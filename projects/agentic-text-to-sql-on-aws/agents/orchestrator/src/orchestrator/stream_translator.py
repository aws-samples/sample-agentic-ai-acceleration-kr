"""Strands stream_async 이벤트 → AG-UI 이벤트 변환기 (순수 로직).

Strands Agent 및 Graph 의 스트리밍 이벤트를 AG-UI 프로토콜 이벤트로 변환한다.
ID 생성기를 주입받으므로 부수효과가 없어 단위 테스트로 완전 커버한다.

지원하는 입력 이벤트 형태:
- 단일 Agent (agent.stream_async):
    {"data": "<텍스트 델타>"}                         → TEXT_MESSAGE_CONTENT
    {"current_tool_use": {"toolUseId","name","input"}} → TOOL_CALL_START/ARGS
    {"result": AgentResult}                            → interrupt 시 CUSTOM(clarification_request)
- Graph (graph.stream_async):
    {"type": "multiagent_node_start", "node_id": ...}  → STEP_STARTED
    {"type": "multiagent_node_stream", "event": {...}} → 내부 Agent 이벤트로 재귀 처리
    {"type": "multiagent_node_stop", "node_id": ...}   → STEP_FINISHED
    {"type": "multiagent_node_interrupt", "node_id", "interrupts":[Interrupt]}
                                                       → CUSTOM(clarification_request)
    {"type": "multiagent_result", ...}                 → (무시, 상위에서 RUN_FINISHED)

TEXT_MESSAGE_START/END 는 텍스트 델타 시퀀스의 시작/종료를 감지해 자동 삽입한다.
clarification interrupt 는 CUSTOM 이벤트로 표면화하고, 상위(app.py)가 RUN_FINISHED 로 닫는다.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

from . import agui_events
from .clarification import CLARIFICATION_INTERRUPT_NAME, clarification_value


class StreamTranslator:
    """Strands 스트림 이벤트를 AG-UI 이벤트로 변환하는 상태 기계.

    id_factory: 호출마다 고유 ID 문자열을 반환하는 콜러블(메시지/툴콜 ID 용).
    """

    def __init__(self, id_factory: Callable[[str], str]) -> None:
        self._id = id_factory
        self._active_message_id: str | None = None
        self._active_tool_ids: dict[str, str] = {}  # strands toolUseId -> agui toolCallId
        self._emitted_tool_args: set[str] = set()
        # clarification interrupt 가 표면화되면 True. 상위(app.py)가 이를 보고
        # 정상 RUN_FINISHED 로 스트림을 닫고 재개는 새 요청으로 처리한다.
        self.clarification_pending: bool = False

    def translate(self, event: dict[str, Any]) -> Iterator[dict[str, Any]]:
        """단일 Strands 이벤트를 0개 이상의 AG-UI 이벤트로 변환."""
        etype = event.get("type")
        if etype == "multiagent_node_start":
            yield from self._flush_text()
            yield agui_events.step_started(str(event.get("node_id", "")))
            return
        if etype == "multiagent_node_stop":
            yield from self._flush_text()
            yield agui_events.step_finished(str(event.get("node_id", "")))
            return
        if etype == "multiagent_node_interrupt":
            yield from self._handle_interrupts(event.get("interrupts"))
            return
        if etype == "multiagent_node_stream":
            inner = event.get("event")
            if isinstance(inner, dict):
                yield from self.translate(inner)
            return
        if etype == "multiagent_result":
            yield from self._flush_text()
            return

        # 단일 Agent 이벤트 형태.
        tool_use = event.get("current_tool_use")
        if isinstance(tool_use, dict) and tool_use.get("toolUseId"):
            yield from self._handle_tool_use(tool_use)
            return

        data = event.get("data")
        if isinstance(data, str) and data:
            yield from self._handle_text_delta(data)
            return

        # 단일 Agent 최종 결과 이벤트: stop_reason=="interrupt" 이면 interrupts 표면화.
        result = event.get("result")
        if result is not None and self._result_interrupted(result):
            yield from self._handle_interrupts(getattr(result, "interrupts", None))
            return

    def finalize(self) -> Iterator[dict[str, Any]]:
        """스트림 종료 시 열린 텍스트 메시지를 닫는다."""
        yield from self._flush_text()

    # --- 내부 헬퍼 ---------------------------------------------------------

    def _handle_interrupts(self, interrupts: Any) -> Iterator[dict[str, Any]]:
        """interrupt(들)를 CUSTOM(clarification_request) 이벤트로 방출.

        clarification 이름이 아닌 interrupt 는 방어적으로 무시한다(현재 clarification 만 사용).
        """
        # 열린 텍스트/도구 스트림을 먼저 닫아 이벤트 순서를 보존.
        yield from self._flush_text()
        if not isinstance(interrupts, (list, tuple)):
            return
        for interrupt in interrupts:
            name = self._interrupt_name(interrupt)
            if name != CLARIFICATION_INTERRUPT_NAME:
                continue
            self.clarification_pending = True
            yield agui_events.custom("clarification_request", clarification_value(interrupt))

    @staticmethod
    def _interrupt_name(interrupt: Any) -> str | None:
        if isinstance(interrupt, dict):
            return interrupt.get("name")
        return getattr(interrupt, "name", None)

    @staticmethod
    def _result_interrupted(result: Any) -> bool:
        """단일 Agent 최종 결과가 interrupt 로 정지했는지 판단(방어적)."""
        if getattr(result, "stop_reason", None) == "interrupt":
            return True
        # stop_reason 이 없어도 interrupts 가 채워져 있으면 interrupt 로 간주.
        return bool(getattr(result, "interrupts", None))

    def _handle_text_delta(self, delta: str) -> Iterator[dict[str, Any]]:
        if self._active_message_id is None:
            # 새 텍스트가 시작되면 열린 도구 호출의 인자 스트림을 종료한다.
            yield from self._close_tools()
            self._active_message_id = self._id("msg")
            yield agui_events.text_message_start(self._active_message_id)
        yield agui_events.text_message_content(self._active_message_id, delta)

    def _handle_tool_use(self, tool_use: dict[str, Any]) -> Iterator[dict[str, Any]]:
        # 텍스트 메시지가 열려 있으면 먼저 닫는다(도구 호출 경계).
        yield from self._close_text()
        strands_id = str(tool_use["toolUseId"])
        name = str(tool_use.get("name", ""))
        if strands_id not in self._active_tool_ids:
            call_id = self._id("tool")
            self._active_tool_ids[strands_id] = call_id
            yield agui_events.tool_call_start(call_id, name)
        call_id = self._active_tool_ids[strands_id]
        # 인자는 누적 스트리밍되므로, 값이 있을 때 스냅샷을 한 번 전달.
        raw_input = tool_use.get("input")
        if raw_input:
            snapshot = raw_input if isinstance(raw_input, str) else str(raw_input)
            key = f"{strands_id}:{snapshot}"
            if key not in self._emitted_tool_args:
                self._emitted_tool_args.add(key)
                yield agui_events.tool_call_args(call_id, snapshot)

    def _flush_text(self) -> Iterator[dict[str, Any]]:
        """열린 텍스트 메시지와 도구 호출을 모두 종료(노드 경계/스트림 종료)."""
        yield from self._close_text()
        yield from self._close_tools()

    def _close_text(self) -> Iterator[dict[str, Any]]:
        if self._active_message_id is not None:
            yield agui_events.text_message_end(self._active_message_id)
            self._active_message_id = None

    def _close_tools(self) -> Iterator[dict[str, Any]]:
        for call_id in self._active_tool_ids.values():
            yield agui_events.tool_call_end(call_id)
        self._active_tool_ids.clear()
