"""AG-UI 프로토콜 이벤트 빌더 (순수 로직).

Strands Graph/Agent 의 stream_async 이벤트를 AG-UI 프로토콜 wire-format(camelCase)
이벤트 dict 로 변환한다. 이 dict 를 SSE 로 그대로 흘려보내면 CopilotKit 이 소비한다.

UI 담당(Task #5)이 그대로 사용할 수 있도록 이벤트 타입/필드를 여기서 단일 정의한다.
AG-UI 표준 이벤트 타입:
  RUN_STARTED / RUN_FINISHED / RUN_ERROR
  TEXT_MESSAGE_START / TEXT_MESSAGE_CONTENT / TEXT_MESSAGE_END
  TOOL_CALL_START / TOOL_CALL_ARGS / TOOL_CALL_END / TOOL_CALL_RESULT
  STEP_STARTED / STEP_FINISHED  (Graph 노드 전이 가시화용)

부수효과가 없어(ID 는 외부 주입) 단위 테스트로 완전 커버한다.
"""

from __future__ import annotations

from typing import Any


def run_started(thread_id: str, run_id: str) -> dict[str, Any]:
    return {"type": "RUN_STARTED", "threadId": thread_id, "runId": run_id}


def run_finished(thread_id: str, run_id: str) -> dict[str, Any]:
    return {"type": "RUN_FINISHED", "threadId": thread_id, "runId": run_id}


def run_error(message: str, code: str | None = None) -> dict[str, Any]:
    event: dict[str, Any] = {"type": "RUN_ERROR", "message": message}
    if code is not None:
        event["code"] = code
    return event


def step_started(step_name: str) -> dict[str, Any]:
    """Graph 노드 진입 (예: schema_linking, sql_generation)."""
    return {"type": "STEP_STARTED", "stepName": step_name}


def step_finished(step_name: str) -> dict[str, Any]:
    return {"type": "STEP_FINISHED", "stepName": step_name}


def text_message_start(message_id: str, role: str = "assistant") -> dict[str, Any]:
    return {"type": "TEXT_MESSAGE_START", "messageId": message_id, "role": role}


def text_message_content(message_id: str, delta: str) -> dict[str, Any]:
    return {"type": "TEXT_MESSAGE_CONTENT", "messageId": message_id, "delta": delta}


def text_message_end(message_id: str) -> dict[str, Any]:
    return {"type": "TEXT_MESSAGE_END", "messageId": message_id}


def tool_call_start(
    tool_call_id: str, tool_call_name: str, parent_message_id: str | None = None
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": "TOOL_CALL_START",
        "toolCallId": tool_call_id,
        "toolCallName": tool_call_name,
    }
    if parent_message_id is not None:
        event["parentMessageId"] = parent_message_id
    return event


def tool_call_args(tool_call_id: str, delta: str) -> dict[str, Any]:
    """도구 인자 델타(JSON 조각 문자열)."""
    return {"type": "TOOL_CALL_ARGS", "toolCallId": tool_call_id, "delta": delta}


def tool_call_end(tool_call_id: str) -> dict[str, Any]:
    return {"type": "TOOL_CALL_END", "toolCallId": tool_call_id}


def tool_call_result(
    message_id: str, tool_call_id: str, content: str, role: str = "tool"
) -> dict[str, Any]:
    return {
        "type": "TOOL_CALL_RESULT",
        "messageId": message_id,
        "toolCallId": tool_call_id,
        "content": content,
        "role": role,
    }
