"""Slack text in, Slack messages out.

In: Slack escapes `&`, `<` and `>` in message text; the agent should see what
the user typed. Out: answers are Markdown, sent as `markdown_text` (standard
Markdown, up to 12,000 characters a message). A workspace that refuses it gets
the same answer as mrkdwn `text` instead, in smaller pieces.
"""
import re
from typing import List, Optional, Tuple

# Per message. Under Slack's limits with room for the fence a split closes.
MARKDOWN_CHUNK = 11_000
# `text` is recommended under 4,000; chat.update refuses more (`msg_too_long`).
MRKDWN_CHUNK = 3_800

_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")


def unescape(text: str) -> str:
    """Undo Slack's escaping. `&amp;` last, so `&amp;lt;` stays `&lt;`."""
    return (text or "").replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _fence_marker(line: str) -> Optional[str]:
    match = _FENCE.match(line)
    return match.group(1) if match else None


def _cut_line(line: str, room: int) -> List[str]:
    """A line longer than a message, cut at a space where one is near the end."""
    pieces: List[str] = []
    while len(line) > room:
        cut = line.rfind(" ", room // 2, room)
        if cut <= 0:
            cut = room
        pieces.append(line[:cut])
        line = line[cut:].lstrip(" ") if line[cut:cut + 1] == " " else line[cut:]
    pieces.append(line)
    return pieces


def split_markdown(text: str, limit: int = MARKDOWN_CHUNK) -> List[str]:
    """Split an answer into messages of at most `limit` characters.

    Prefers a blank line (a paragraph boundary) in the second half of a full
    message, then a line boundary, then a hard cut. A message that ends inside
    a code block closes the fence, and the next one reopens it with the same
    opening line (language included), so each message renders on its own.
    """
    text = (text or "").strip("\n")
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: List[str] = []
    # (line, opener of the fence open *after* this line, or None)
    lines: List[Tuple[str, Optional[str]]] = []
    opener: Optional[str] = None  # the fence line currently open
    size = 0

    def body_of(entries: List[Tuple[str, Optional[str]]]) -> str:
        return "\n".join(line for line, _ in entries)

    def emit(entries: List[Tuple[str, Optional[str]]], open_after: Optional[str]) -> None:
        body = body_of(entries)
        if open_after:
            body += "\n" + _fence_marker(open_after)
        if body.strip():
            chunks.append(body)

    def flush() -> None:
        nonlocal lines, size
        # A paragraph break outside any fence, in the back half of the message.
        cut = None
        for i in range(len(lines) - 1, 0, -1):
            line, open_after = lines[i]
            if not line.strip() and open_after is None and len(body_of(lines[:i])) >= limit // 2:
                cut = i
                break
        if cut is not None:
            emit(lines[:cut], None)
            carried = lines[cut + 1 :]
        else:
            emit(lines, lines[-1][1] if lines else None)
            carried = []
            if opener:
                carried = [(opener, opener)]
        lines = carried
        size = len(body_of(lines))

    for raw in text.split("\n"):
        # Room for "\n" + a closing fence on this message.
        reserve = 1 + len(_fence_marker(opener) or "") if opener else 0
        pieces = [raw]
        room = limit - reserve - (len(opener) + 1 if opener else 0) - 1
        if len(raw) > room > 0:
            pieces = _cut_line(raw, room)
        for piece in pieces:
            marker = _fence_marker(piece)
            next_opener = opener
            if marker:
                if opener is None:
                    next_opener = piece
                elif marker.startswith(_fence_marker(opener)[0]) and len(marker) >= len(_fence_marker(opener)):
                    next_opener = None
            closing = 1 + len(_fence_marker(next_opener)) if next_opener else 0
            # Twice at most: a paragraph cut can carry lines that still leave
            # no room, and the second flush then sends everything held.
            for _ in range(2):
                added = len(piece) + (1 if lines else 0)
                if not lines or size + added + closing <= limit:
                    break
                if opener and lines == [(opener, opener)]:
                    break  # only the reopened fence is held; nothing to send
                flush()
            lines.append((piece, next_opener))
            size += added
            opener = next_opener
    if lines:
        emit(lines, None)
    return chunks


_BOLD = re.compile(r"\*\*(.+?)\*\*")
_HEADING = re.compile(r"^#{1,6}\s+(.*)$")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def to_mrkdwn(markdown: str) -> str:
    """A best-effort Markdown -> mrkdwn pass for the fallback path.

    Escapes `&`, `<`, `>` (mrkdwn control characters), then rewrites bold,
    headings and links outside code blocks. Lists and code blocks already read
    the same in both.
    """
    out: List[str] = []
    in_code = False
    for line in (markdown or "").split("\n"):
        if _fence_marker(line):
            in_code = not in_code
            out.append("```")
            continue
        line = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if not in_code:
            heading = _HEADING.match(line)
            if heading:
                line = f"*{heading.group(1).strip()}*"
            line = _BOLD.sub(r"*\1*", line)
            line = _LINK.sub(r"<\2|\1>", line)
        out.append(line)
    return "\n".join(out)


# ── Fixed user-facing phrases ─────────────────────────────────────────────────
# Never carry internal detail (exception text, ARNs, ids): a DM is outside the
# admin's view, and the user cannot act on it anyway.

PROCESSING = "처리 중…"
EMPTY_ANSWER = "_(응답 내용이 없습니다.)_"
FILE_SHARE_UNSUPPORTED = "첨부파일은 아직 지원하지 않습니다. 내용을 텍스트로 보내 주세요."
EXTERNAL_USER = "이 워크스페이스의 구성원만 이 봇을 사용할 수 있습니다."
REFUSALS = {
    "unmapped": "이 Slack 계정의 이메일과 일치하는 플랫폼 계정이 없습니다. 관리자에게 문의해 주세요.",
    "ambiguous": "이 Slack 이메일과 일치하는 플랫폼 계정이 여러 개입니다. 관리자에게 문의해 주세요.",
    "inactive": "플랫폼 계정이 비활성 상태이거나 이메일 확인이 끝나지 않았습니다. 관리자에게 문의해 주세요.",
    "not_allowed": "이 에이전트를 사용할 권한이 없습니다.",
    "lookup_failed": "계정을 확인하지 못했습니다. 잠시 후 다시 시도해 주세요.",
}
BUSY = "이 대화에서 이전 질문을 아직 처리하고 있습니다. 답변이 끝난 뒤 다시 보내 주세요."
FOREIGN_THREAD = "다른 계정의 대화에는 이어서 질문할 수 없습니다. 스레드 밖에서 새 대화를 시작해 주세요."
AGENT_UNAVAILABLE = "지금은 이 에이전트를 사용할 수 없습니다. 관리자에게 문의해 주세요."
NEW_THREAD_NEEDED = "이 대화는 더 이어갈 수 없습니다. 스레드 밖에서 새 대화를 시작해 주세요."
NOT_STARTED = "요청을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요."

FAILED_NOTE = "⚠️ 답변 중 오류가 발생했습니다."
INTERRUPTED_NOTE = "⚠️ 답변이 중간에 끊겼습니다. 다시 질문해 주세요."
TIMEOUT_NOTE = "⚠️ 답변 시간이 너무 길어 중단했습니다."
LOCK_LOST_NOTE = "⚠️ 처리 상태를 유지하지 못해 답변을 중단했습니다. 다시 질문해 주세요."


def artifacts_note(count: int) -> str:
    return f"📎 파일 {count}개가 만들어졌습니다. 웹에서 확인해 주세요."


def web_link(origin: str, thread_id: str) -> str:
    return f"[웹에서 이어 보기]({origin}/?threadId={thread_id})"
