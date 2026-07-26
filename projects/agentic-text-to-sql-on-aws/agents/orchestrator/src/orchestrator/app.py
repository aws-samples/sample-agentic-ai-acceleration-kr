"""AG-UI 엔트리포인트 (AgentCore Runtime, 포트 8080).

`BedrockAgentCoreApp` 로 `POST /invocations`(SSE) + `GET /ping` 을 노출하고,
Strands Graph(기본) 또는 단일 Agent(폴백)의 stream_async 를 AG-UI 프로토콜 이벤트로
변환해 SSE 로 스트리밍한다.

## AG-UI 통합 선택 근거 (보고서와 동일)
`AGUIApp`/`ag_ui_strands.StrandsAgent` 에 **Graph** 를 직접 결합하는 경로는 2026-07 기준
문서로 검증되지 않았다(단일 Agent 만 확인). 이벤트 흐름을 확실히 보장하기 위해
`graph.stream_async`(및 agent.stream_async)를 직접 구동하고 AG-UI wire-format 이벤트를
우리가 방출한다. 이 방식은 CopilotKit 이 그대로 소비하는 표준 AG-UI 이벤트를 생성한다.

## clarification(재요청) 재개 전략 (M2)
질의가 모호하면 intent 단계가 `request_clarification` 도구를 호출해 Strands interrupt 를
발생시킨다. 이때 CUSTOM(clarification_request) 이벤트를 방출한 뒤 정상 RUN_FINISHED 로
스트림을 닫는다(연결을 열어둔 채 대기하지 않음). 사용자의 폼 응답은 새 HTTP 요청으로
`forwardedProps.clarificationResponse` 를 통해 들어온다.

재개는 **같은 microVM 내 모듈 레벨 캐시**(session_id → 실행 세션)로 처리한다. AgentCore
Runtime 은 runtimeSessionId 로 같은 microVM 라우팅을 지향하지만 프로세스 상태 복원을
보장하지 않으므로, interrupt 발생 시 Graph/Agent 인스턴스와 열린 MCP 클라이언트를 캐시에
살려둔다. 캐시 미스(=microVM 교체)로 재개할 수 없으면 RUN_ERROR 대신 사용자에게 질문을
다시 하도록 안내한다(CLARIFICATION_EXPIRED).

> 참고: `AgentCoreMemorySessionManager` 는 Graph(multiagent) 세션 영속화를 지원하지 않는다
> (create/read/update_multi_agent 미구현 → 상위 SessionRepository 가 NotImplementedError).
> 따라서 Graph 에는 세션 매니저를 연결하지 않고 캐시 전략만 사용한다. 단일 Agent 는 STM 용으로
> 세션 매니저를 연결하되, interrupt 재개 자체는 같은 인스턴스 재사용(캐시)에 의존한다.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator
from typing import Any

from . import agui_events
from .agent_builder import OrchestratorBuilder
from .clarification import build_resume_task
from .config import Settings
from .ids import SequentialIdFactory
from .mcp_client import create_tool_clients
from .memory import create_session_manager
from .request import ParsedRequest, parse_run_input
from .session_cache import SessionCache
from .stream_translator import StreamTranslator

logger = logging.getLogger("orchestrator")

SETTINGS = Settings.from_env()

# session_id → 진행 중(interrupt 대기) 실행 세션. clarification 재개용.
SESSION_CACHE: SessionCache = SessionCache()


class RunnerSession:
    """한 세션의 실행 상태 묶음(재개 시 재사용).

    runner(Graph/Agent) 와 열린 MCP 클라이언트·세션 매니저를 함께 보관해,
    interrupt 로 정지한 실행을 같은 인스턴스로 재개할 수 있게 한다.
    """

    def __init__(
        self,
        runner: Any,
        clients: list[Any],
        session_manager: Any | None,
    ) -> None:
        self.runner = runner
        self._clients = clients
        self._session_manager = session_manager

    def close(self) -> None:
        """MCP 클라이언트와 세션 매니저 자원을 정리한다(예외는 삼킨다)."""
        for client in self._clients:
            # MCPClient.stop 은 __exit__ 시그니처(exc_type, exc_val, exc_tb)를 따른다.
            _safe_close(client, "stop", None, None, None)
        if self._session_manager is not None:
            _safe_close(self._session_manager, "close")


def _safe_close(obj: Any, method: str, *args: Any) -> None:
    fn = getattr(obj, method, None)
    if fn is None:
        return
    try:
        fn(*args)
    except TypeError:
        # 시그니처 차이(무인자 stop/close 구현) 방어 — 인자 없이 재시도.
        try:
            fn()
        except Exception:  # noqa: BLE001
            logger.warning("자원 정리 실패(%s.%s)", type(obj).__name__, method, exc_info=True)
    except Exception:  # noqa: BLE001 — 정리 실패는 로깅만, 요청 흐름을 막지 않음
        logger.warning("자원 정리 실패(%s.%s)", type(obj).__name__, method, exc_info=True)


def _build_app() -> Any:
    from bedrock_agentcore.runtime import BedrockAgentCoreApp

    app = BedrockAgentCoreApp()

    @app.entrypoint
    async def invoke(payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        async for event in run_orchestration(payload, SETTINGS):
            # BedrockAgentCoreApp 은 async generator 의 yield 값을 SSE data 로 직렬화한다.
            yield event

    return app


async def run_orchestration(
    payload: dict[str, Any], settings: Settings
) -> AsyncIterator[dict[str, Any]]:
    """요청 1건을 처리하며 AG-UI 이벤트를 순차 방출."""
    req = parse_run_input(payload)
    yield agui_events.run_started(req.thread_id, req.run_id)

    try:
        settings.require_mcp_arns()
    except ValueError as exc:
        logger.error("구성 오류: %s", exc)
        yield agui_events.run_error(str(exc), code="CONFIG_ERROR")
        yield agui_events.run_finished(req.thread_id, req.run_id)
        return

    # clarification 재개 요청 여부에 따라 경로를 분기.
    if req.clarification_response is not None:
        async for event in _resume_orchestration(req, settings):
            yield event
        return

    if not req.question:
        yield agui_events.run_error("질문이 비어 있습니다.", code="EMPTY_QUESTION")
        yield agui_events.run_finished(req.thread_id, req.run_id)
        return

    async for event in _fresh_orchestration(req, settings):
        yield event


async def _fresh_orchestration(
    req: ParsedRequest, settings: Settings
) -> AsyncIterator[dict[str, Any]]:
    """신규 질의 처리. interrupt 발생 시 실행 세션을 캐시에 살려둔다."""
    translator = StreamTranslator(SequentialIdFactory(req.run_id))
    # 도구 평면 모드(direct/gateway)를 추상화한 클라이언트 묶음.
    tool_clients = create_tool_clients(settings)
    session_manager = create_session_manager(
        settings.memory_id, req.actor_id, req.session_id, settings.region
    )

    # interrupt 재개를 위해 클라이언트를 컨텍스트 매니저가 아닌 명시적 start/stop 으로 관리.
    session = RunnerSession(
        runner=None, clients=tool_clients.clients, session_manager=session_manager
    )
    try:
        tool_clients.start()

        sql_tools = tool_clients.sql_tools()
        semantic_tools = tool_clients.semantic_tools()
        builder = OrchestratorBuilder(
            settings=settings,
            sql_tools=sql_tools,
            semantic_tools=semantic_tools,
            session_manager=session_manager,
        )
        session.runner = _build_runner(builder, settings.mode)
        async for event in _drive(session, req, translator, task=req.question):
            yield event
    except Exception as exc:  # noqa: BLE001 — 어떤 실패든 AG-UI 오류로 사용자에 전달
        logger.exception("오케스트레이션 실패")
        for agui_event in translator.finalize():
            yield agui_event
        session.close()
        SESSION_CACHE.pop(req.session_id)
        yield agui_events.run_error(f"처리 중 오류가 발생했습니다: {exc}", code="RUNTIME_ERROR")

    yield agui_events.run_finished(req.thread_id, req.run_id)


async def _resume_orchestration(
    req: ParsedRequest, settings: Settings
) -> AsyncIterator[dict[str, Any]]:
    """clarification 응답으로 정지 지점부터 재개. 캐시 미스면 CLARIFICATION_EXPIRED."""
    session = SESSION_CACHE.pop(req.session_id)
    if session is None:
        # microVM 교체 등으로 interrupt state 소실 → 사용자에게 재질의 안내.
        logger.info("CLARIFICATION_EXPIRED: session_id=%s 캐시 미스로 재개 불가", req.session_id)
        for event in _clarification_expired_events(SequentialIdFactory(req.run_id)):
            yield event
        yield agui_events.run_finished(req.thread_id, req.run_id)
        return

    translator = StreamTranslator(SequentialIdFactory(req.run_id))
    task = build_resume_task(req.clarification_response or {})
    try:
        async for event in _drive(session, req, translator, task=task):
            yield event
    except Exception as exc:  # noqa: BLE001
        logger.exception("clarification 재개 실패")
        for agui_event in translator.finalize():
            yield agui_event
        session.close()
        yield agui_events.run_error(f"처리 중 오류가 발생했습니다: {exc}", code="RUNTIME_ERROR")

    yield agui_events.run_finished(req.thread_id, req.run_id)


async def _drive(
    session: RunnerSession,
    req: ParsedRequest,
    translator: StreamTranslator,
    task: Any,
) -> AsyncIterator[dict[str, Any]]:
    """runner 를 구동해 이벤트를 방출하고, 완료 후 세션 수명을 결정한다.

    interrupt(clarification) 로 끝나면 세션을 캐시에 유지(재개 대비), 정상 종료면 자원 정리.
    """
    async for strands_event in session.runner.stream_async(task):
        for agui_event in translator.translate(strands_event):
            yield agui_event
    for agui_event in translator.finalize():
        yield agui_event

    if translator.clarification_pending:
        # 재개를 위해 실행 세션(클라이언트 포함)을 살려둔다. 상한 초과분은 정리.
        for evicted in SESSION_CACHE.put(req.session_id, session):
            if isinstance(evicted, RunnerSession):
                evicted.close()
    else:
        session.close()


def _clarification_expired_events(id_factory: SequentialIdFactory) -> Iterator[dict[str, Any]]:
    """재개 불가(만료) 시 사용자에게 재질의를 안내하는 TEXT_MESSAGE 시퀀스."""
    message_id = id_factory("msg")
    text = (
        "요청하신 추가 정보를 이어서 처리할 세션이 만료되었습니다. "
        "번거로우시겠지만 질문을 처음부터 다시 입력해 주세요."
    )
    yield agui_events.text_message_start(message_id)
    yield agui_events.text_message_content(message_id, text)
    yield agui_events.text_message_end(message_id)


def _build_runner(builder: OrchestratorBuilder, mode: str) -> Any:
    if mode == "agent":
        logger.info("단일 Agent 모드로 실행")
        return builder.build_single_agent()
    logger.info("Strands Graph 모드로 실행")
    return builder.build_graph()


app = _build_app()


def main() -> None:
    app.run()


if __name__ == "__main__":
    main()
