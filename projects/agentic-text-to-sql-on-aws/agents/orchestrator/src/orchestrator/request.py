"""AG-UI 요청 페이로드 파싱 (순수 로직).

AG-UI RunAgentInput 페이로드에서 질문/스레드/실행 ID 및 세션·액터 식별자를 추출한다.
CopilotKit 프록시가 전달하는 형태를 수용한다:
  {threadId, runId, messages:[{id,role,content}], state:{}, forwardedProps:{}, context:[]}

actorId/sessionId 규격:
- sessionId: AG-UI `threadId` (대화 스레드 = STM 세션 단위)
- actorId: forwardedProps.actorId 또는 state.actorId, 없으면 "anonymous"
  (M3 에서 Cognito JWT sub 로 대체 예정)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT_ACTOR_ID = "anonymous"


@dataclass(frozen=True)
class ParsedRequest:
    """정규화된 요청."""

    question: str
    thread_id: str
    run_id: str
    session_id: str
    actor_id: str


def parse_run_input(payload: dict[str, Any]) -> ParsedRequest:
    """RunAgentInput dict 를 ParsedRequest 로 파싱."""
    thread_id = _first_str(payload.get("threadId"), payload.get("thread_id")) or "default-thread"
    run_id = _first_str(payload.get("runId"), payload.get("run_id")) or "default-run"
    question = _latest_user_message(payload.get("messages"))

    forwarded = payload.get("forwardedProps") or payload.get("forwarded_props") or {}
    state = payload.get("state") or {}
    actor_id = (
        _first_str(
            forwarded.get("actorId") if isinstance(forwarded, dict) else None,
            state.get("actorId") if isinstance(state, dict) else None,
        )
        or DEFAULT_ACTOR_ID
    )
    # sessionId 우선순위: 명시적 지정 > threadId.
    session_id = (
        _first_str(
            forwarded.get("sessionId") if isinstance(forwarded, dict) else None,
            state.get("sessionId") if isinstance(state, dict) else None,
        )
        or thread_id
    )
    return ParsedRequest(
        question=question,
        thread_id=thread_id,
        run_id=run_id,
        session_id=session_id,
        actor_id=actor_id,
    )


def _latest_user_message(messages: Any) -> str:
    """messages 배열에서 가장 최근 user 메시지 텍스트를 추출."""
    if not isinstance(messages, list):
        return ""
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        if str(msg.get("role", "")).lower() != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content.strip()
        # content 가 파트 배열인 경우 텍스트 파트를 이어붙임.
        if isinstance(content, list):
            texts = [
                str(p.get("text", ""))
                for p in content
                if isinstance(p, dict) and p.get("type") in (None, "text")
            ]
            joined = " ".join(t for t in texts if t).strip()
            if joined:
                return joined
    return ""


def _first_str(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None
