"""AG-UI 요청 페이로드 파싱 (순수 로직).

AG-UI RunAgentInput 페이로드에서 질문/스레드/실행 ID 및 세션·액터 식별자를 추출한다.
CopilotKit 프록시가 전달하는 형태를 수용한다:
  {threadId, runId, messages:[{id,role,content}], state:{}, forwardedProps:{}, context:[]}

actorId/sessionId 규격:
- sessionId: AG-UI `threadId` (대화 스레드 = STM 세션 단위)
- actorId: forwardedProps.actorId 또는 state.actorId, 없으면 "anonymous"
  (M3 에서 Cognito JWT sub 로 대체 예정)

M4 additive: forwardedProps.userAccessToken (Cognito AccessToken) 이 오면 gateway 모드에서
사용자 위임(On-Behalf-Of) Bearer 로 쓰인다. **토큰 값은 절대 로깅하지 않는다.**
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT_ACTOR_ID = "anonymous"


@dataclass(frozen=True)
class ParsedRequest:
    """정규화된 요청.

    clarification_response 가 있으면 이번 요청은 앞선 clarification interrupt 에 대한 재개다.
    형태: {"interruptId": str, "values": {<field name>: <값>, ...}}.

    user_access_token (M4 additive) 은 호출자가 전달한 Cognito AccessToken 이다.
    gateway 모드에서 M2M 서비스 토큰 대신 이 토큰으로 Gateway MCP 를 호출(OBO)한다.
    민감값이므로 로그·이벤트·LLM 컨텍스트에 노출하지 않는다.
    """

    question: str
    thread_id: str
    run_id: str
    session_id: str
    actor_id: str
    clarification_response: dict[str, Any] | None = None
    user_access_token: str | None = None


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
    clarification_response = _parse_clarification_response(forwarded, state)
    user_access_token = _parse_user_access_token(forwarded, state)
    return ParsedRequest(
        question=question,
        thread_id=thread_id,
        run_id=run_id,
        session_id=session_id,
        actor_id=actor_id,
        clarification_response=clarification_response,
        user_access_token=user_access_token,
    )


def _parse_user_access_token(forwarded: Any, state: Any) -> str | None:
    """사용자 위임(OBO) Cognito AccessToken 을 추출 (M4 additive).

    `forwardedProps.userAccessToken` 우선, snake_case(`user_access_token`)와
    `state` 경유도 수용한다. 문자열이 아니거나 공백이면 None(= 기존 서비스 계정 위임 유지).
    **토큰 값은 로깅 금지** — 여기서도 값에 대한 어떤 로그도 남기지 않는다.
    """
    candidates: list[Any] = []
    for source in (forwarded, state):
        if isinstance(source, dict):
            candidates.append(source.get("userAccessToken"))
            candidates.append(source.get("user_access_token"))
    return _first_str(*candidates)


def _parse_clarification_response(forwarded: Any, state: Any) -> dict[str, Any] | None:
    """clarification interrupt 재개 payload 를 추출·정규화.

    forwardedProps.clarificationResponse 를 우선하고 state.clarificationResponse 도 수용한다.
    유효 조건: interruptId(문자열)와 values(dict)가 모두 있어야 한다.
    """
    raw = None
    if isinstance(forwarded, dict):
        raw = forwarded.get("clarificationResponse")
    if raw is None and isinstance(state, dict):
        raw = state.get("clarificationResponse")
    if not isinstance(raw, dict):
        return None
    interrupt_id = _first_str(raw.get("interruptId"), raw.get("interrupt_id"))
    values = raw.get("values")
    if not interrupt_id or not isinstance(values, dict):
        return None
    return {"interruptId": interrupt_id, "values": values}


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
