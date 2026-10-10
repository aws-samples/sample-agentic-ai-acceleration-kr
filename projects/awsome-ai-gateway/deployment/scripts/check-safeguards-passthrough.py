#!/usr/bin/env python3
"""게이트웨이에 Auto mode 서버 분류기 패치가 적용됐는지 확인한다.

Claude Code 와 같은 모양의 요청(같은 User-Agent, 같은 anthropic-beta)을
게이트웨이에 보내고 다섯 가지를 본다. 모델이 도구를 부르더라도 실행하지 않는다.
  1) 턴별 effort  — 대화 중간 메시지의 output_config 가 400 없이 통과하는가
  2) 판정(비스트리밍) — 응답 최상위에 safeguard_results 가 오는가
  3) 판정(스트리밍)   — message_delta.delta.safeguard_results 가 오는가
  4) 도구 추가 블록   — system 메시지의 tool_addition 이 400 없이 통과하는가
                        (advisor 를 가리키는 블록은 게이트웨이가 지워야 한다)
  5) thinking 표시    — thinking.display "updates" 가 400 없이 통과하는가
4)·5) 는 2026-10-09 에 더한 beta(inline-tools · thinking-display-updates)를 본다.
모두 통과하면 종료 코드 0, 아니면 1.

사용법:
  read -rs GATEWAY_KEY && export GATEWAY_KEY   # 게이트웨이 키(VK), 화면에 안 보임
  python3 check-safeguards-passthrough.py https://<게이트웨이 주소>
  python3 check-safeguards-passthrough.py https://<게이트웨이 주소> \
      --model claude-sonnet-5
--model 은 이 게이트웨이에 등록된 Auto mode 지원 모델(Claude 4.6 이상)의 별칭이다. 1) 은
대화 중간 메시지별 effort 를 받는 5.5 세대(Opus·Sonnet·Haiku 5.5)에서만 판정된다 — 다른 모델에서는
4)·5) 로 beta 전달이 확인되면 1) 을 SKIP 으로 둔다(Bedrock 오류 문구가 beta 유무와 같다).
4) 도 모델이 도구 추가 블록을 받지 않으면(Sonnet 5) SKIP 이다. 모델 때문인 SKIP 은 결과를
바꾸지 않는다. 2)·3) 의 SKIP 은 모델이 도구를 부르지 않은 것이라 다시 실행한다.

English: checks whether the Auto-mode server-classifier patch is live on a gateway.
Sends Claude Code-shaped requests (same User-Agent, same anthropic-beta values)
and checks 1) per-turn effort passes without a 400 (only the 5.5 generation — Opus, Sonnet, Haiku 5.5 — takes it;
on other models it is SKIP when 4) or 5) shows the betas get through — Bedrock's error is
the same with or without the beta; 4) is SKIP on a model without mid-conversation
tool changes, e.g. Sonnet 5), 2) non-streaming verdicts
(top-level safeguard_results), 3) streaming verdicts
(message_delta.delta.safeguard_results), 4) a system-message tool_addition passes
(and one pointing at the advisor is removed by the gateway), 5) thinking.display
"updates" passes. 4) and 5) cover the betas added on 2026-10-09 (inline-tools,
thinking-display-updates). No tool is ever executed. Exit code 0 when
all pass, 1 otherwise. Use a 5.5-generation model (Opus, Sonnet or Haiku 5.5) for the full check.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import unicodedata
import urllib.error
import urllib.request

BETAS = ("claude-code-20250219,interleaved-thinking-2025-05-14,"
         "thinking-token-count-2026-05-13,context-management-2025-06-27,"
         "prompt-caching-scope-2026-01-05,mid-conversation-system-2026-04-07,"
         "per-turn-control-2026-07-01,mid-conversation-tool-changes-2026-07-01,"
         "effort-2025-11-24,dangerous-tool-use-2026-09-03,afk-mode-2026-01-31")
#: 계정 기능 플래그가 켜진 Claude Code 가 더 보내는 beta (2026-10-09).
#: English: extra betas sent by Claude Code with account feature flags on.
FLAGGED_BETAS = (BETAS + ",inline-tools-2026-09-15,advisor-tool-2026-03-01,"
                 "thinking-display-updates-2026-08-18")
BASH = {"name": "Bash", "description": "Run a shell command.",
        "input_schema": {"type": "object",
                         "properties": {"command": {"type": "string"}},
                         "required": ["command"]}}
#: 판단 재료는 최소한만 — 사용자 경로·신원은 보내지 않는다.
#: English: a minimal classifier context — no user paths or identity.
SAFEGUARDS = [{"type": "dangerous_tool_use",
               "classifier_context": {"v": 1, "permission_mode": "auto"}}]
ASK = "Use the Bash tool to run exactly: ls -la"
#: Anthropic API 전용 도구 — 게이트웨이가 tools 에서 지운다.
#: English: an Anthropic-only tool the gateway removes from tools.
ADVISOR = {"type": "advisor_20260301", "name": "advisor", "model": "claude-fable-5-1"}


def post(base: str, key: str, body: dict, betas: str = BETAS) -> tuple[int, str]:
    req = urllib.request.Request(
        base.rstrip("/") + "/v1/messages", data=json.dumps(body).encode(),
        method="POST", headers={
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": betas,
            "authorization": f"Bearer {key}",
            "user-agent": "claude-cli/2.1.289 (external, sdk-cli)",
            "x-app": "cli"})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def sse_events(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.split("\n\n"):
        event, data = None, None
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                try:
                    data = json.loads(line[5:].strip())
                except ValueError:
                    data = None
        if event and isinstance(data, dict):
            out.append((event, data))
    return out


def verdict_for(results, tool_id: str) -> str | None:
    """safeguard_results 에서 tool_id 의 판정(outcome)을 꺼낸다. 없으면 None."""
    for r in results or []:
        if isinstance(r, dict) and r.get("type") == "dangerous_tool_use":
            v = ((r.get("status") or {}).get("tool_uses") or {}).get(tool_id)
            if isinstance(v, dict):
                return v.get("outcome") or v.get("type")
    return None


def check_effort(base, key, model):
    body = {"model": model, "max_tokens": 50, "messages": [
        {"role": "user", "content": "Say OK."},
        {"role": "system", "content": [{"type": "text", "text": "One word."}],
         "output_config": {"effort": "low"}}]}
    st, text = post(base, key, body)
    if st == 200:
        return "PASS", "200"
    if "output_config" in text:
        return "FAIL", f"{st} per-turn-control 이 Bedrock 까지 가지 않음"
    if "Unexpected value" in text:
        return "FAIL", f"{st} 모르는 beta 까지 넘김: {text[:120]}"
    return "ERROR", f"{st} {text[:160]}"


def check_verdict(base, key, model, stream: bool):
    body = {"model": model, "max_tokens": 400, "tools": [BASH],
            "safeguards": SAFEGUARDS, "stream": stream,
            "messages": [{"role": "user", "content": ASK}]}
    for _ in range(3):                       # 모델이 도구를 안 부르면 다시
        st, text = post(base, key, body)
        if st != 200:
            return "FAIL", f"{st} {text[:160]}"
        if stream:
            evs = sse_events(text)
            err = [d for e, d in evs if e == "error"]
            if err:
                return "FAIL", f"스트림 오류: {json.dumps(err[0])[:160]}"
            ids = [d["content_block"]["id"] for e, d in evs
                   if e == "content_block_start"
                   and d.get("content_block", {}).get("type") == "tool_use"]
            deltas = [d.get("delta") or {} for e, d in evs if e == "message_delta"]
            results = deltas[-1].get("safeguard_results") if deltas else None
        else:
            msg = json.loads(text)
            ids = [b["id"] for b in msg.get("content", [])
                   if b.get("type") == "tool_use"]
            results = msg.get("safeguard_results")
        if not ids:
            continue
        outcome = verdict_for(results, ids[0])
        if outcome:
            return "PASS", f"{ids[0]} -> {outcome}"
        return "FAIL", "판정 없음 (safeguards 또는 판정이 중간에 사라짐)"
    return "SKIP", "모델이 도구를 부르지 않아 판정을 볼 수 없음 (다시 실행)"


def check_tool_addition(base, key, model):
    body = {"model": model, "max_tokens": 50, "tools": [BASH, ADVISOR],
            "messages": [
                {"role": "user", "content": "Say OK."},
                {"role": "system", "content": [
                    {"type": "text", "text": "One word."},
                    {"type": "tool_addition",
                     "tool": {"type": "tool_reference", "name": "Bash"}},
                    {"type": "tool_addition",
                     "tool": {"type": "tool_reference", "name": "advisor"}}]}]}
    st, text = post(base, key, body, FLAGGED_BETAS)
    if st == 200:
        return "PASS", "200"
    if "not supported on this model" in text:      # the beta got through (e.g. Sonnet 5)
        return "SKIP", f"{st} 이 모델은 대화 중간 도구 추가를 받지 않음 (beta 는 전달됨)"
    if "advisor_" in text:                         # the tool itself reached Bedrock
        return "FAIL", f"{st} advisor 도구가 그대로 Bedrock 까지 감"
    if "unknown tool" in text:
        return "FAIL", f"{st} advisor 를 가리키는 블록이 남음"
    if "tool_addition" in text:
        return "FAIL", f"{st} inline-tools 가 Bedrock 까지 가지 않음"
    return "ERROR", f"{st} {text[:160]}"


def check_thinking_display(base, key, model):
    body = {"model": model, "max_tokens": 1000,
            "thinking": {"type": "adaptive", "display": "updates"},
            "messages": [{"role": "user", "content": "Say OK."}]}
    st, text = post(base, key, body, FLAGGED_BETAS)
    if st == 200:
        return "PASS", "200"
    if "display" in text:
        return "FAIL", f"{st} thinking-display-updates 가 Bedrock 까지 가지 않음"
    return "ERROR", f"{st} {text[:160]}"


def _pad(s: str, width: int) -> str:
    """한글을 2칸으로 세어 오른쪽을 채운다. English: pad by display width."""
    w = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)
    return s + " " * max(width - w, 0)


def main() -> int:
    ap = argparse.ArgumentParser(description="Auto mode 서버 분류기 패치 확인")
    ap.add_argument("url", help="게이트웨이 주소, 예: https://gateway.example.com")
    ap.add_argument("--model", default="claude-sonnet-5-5")
    args = ap.parse_args()
    key = os.environ.get("GATEWAY_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if not key:
        print("GATEWAY_KEY(게이트웨이 키)를 먼저 설정하세요.\n"
              "  예: read -rs GATEWAY_KEY && export GATEWAY_KEY")
        return 2
    url, model = args.url, args.model
    rows = [("1) 턴별 effort", check_effort(url, key, model)),
            ("2) 판정 (비스트리밍)", check_verdict(url, key, model, False)),
            ("3) 판정 (스트리밍)", check_verdict(url, key, model, True)),
            ("4) 도구 추가 블록", check_tool_addition(url, key, model)),
            ("5) thinking 표시", check_thinking_display(url, key, model))]
    # 1) 의 400 은 beta 가 안 넘어온 것과 모델이 메시지별 effort 를 안 받는 것을 구분하지
    # 못한다. 같은 경로로 넘어가는 4)·5) 의 beta 가 통과하면 모델 쪽 이유로 본다.
    # English: 1)'s 400 cannot tell a missing beta from a model that does not take per-message
    # effort; when 4)/5)'s betas get through the same path, it is the model.
    if rows[0][1][0] == "FAIL" and "per-turn-control" in rows[0][1][1] and \
            "PASS" in (rows[3][1][0], rows[4][1][0]):
        rows[0] = (rows[0][0], ("SKIP", "이 모델은 메시지별 effort 를 받지 않음 — "
                                        "5.5 세대 모델로 판정 (--model)"))
    print(f"게이트웨이: {url}   모델: {model}")
    for name, (res, detail) in rows:
        print(f"  {res:5s}  {_pad(name, 20)} {detail}")
    passed = [r for _, (r, _) in rows]
    # 1)·4) 의 SKIP 은 모델이 그 기능을 받지 않는다는 뜻이라 결과를 바꾸지 않는다.
    # English: a SKIP in 1) or 4) means the model lacks the feature, not the gateway.
    model_skips = [i for i in (0, 3) if passed[i] == "SKIP"]
    if all(p == "PASS" or i in model_skips for i, p in enumerate(passed)):
        note = " (이 모델에서 판정 불가: " + ", ".join(f"{i + 1})" for i in model_skips) + ")" \
            if model_skips else ""
        print("결과: 패치 적용됨" + note)
        return 0
    if passed[:3] == ["PASS"] * 3 and "PASS" not in passed[3:]:
        print("결과: 서버 판정은 적용됨, 2026-10-09 beta 2개는 미적용 — 이미지 버전을 확인하세요")
    elif passed[1] == "PASS" and passed[2] == "FAIL":
        print("결과: 요청 쪽만 적용됨 — 웹 검색 응답의 마지막 message_delta 를 확인하세요")
    elif all(p == "FAIL" for p in passed):
        print("결과: 패치 미적용 (이전 동작)")
    else:
        print("결과: 일부만 확인됨 — 위 항목을 확인하세요")
    return 1


if __name__ == "__main__":
    sys.exit(main())
