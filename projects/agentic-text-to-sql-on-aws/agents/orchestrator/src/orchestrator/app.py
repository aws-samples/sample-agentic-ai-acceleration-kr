"""AG-UI 엔트리포인트 (AgentCore Runtime, 포트 8080).

`BedrockAgentCoreApp` 로 `POST /invocations`(SSE) + `GET /ping` 을 노출하고,
Strands Graph(기본) 또는 단일 Agent(폴백)의 stream_async 를 AG-UI 프로토콜 이벤트로
변환해 SSE 로 스트리밍한다.

## AG-UI 통합 선택 근거 (보고서와 동일)
`AGUIApp`/`ag_ui_strands.StrandsAgent` 에 **Graph** 를 직접 결합하는 경로는 2026-07 기준
문서로 검증되지 않았다(단일 Agent 만 확인). 이벤트 흐름을 확실히 보장하기 위해
`graph.stream_async`(및 agent.stream_async)를 직접 구동하고 AG-UI wire-format 이벤트를
우리가 방출한다. 이 방식은 CopilotKit 이 그대로 소비하는 표준 AG-UI 이벤트를 생성한다.

각 요청은 세션 매니저·MCP 도구를 새로 바인딩해 실행한다(마이크로VM 세션 격리 전제).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from . import agui_events
from .agent_builder import OrchestratorBuilder
from .config import Settings
from .ids import SequentialIdFactory
from .mcp_client import create_mcp_client
from .memory import create_session_manager
from .request import parse_run_input
from .stream_translator import StreamTranslator

logger = logging.getLogger("orchestrator")

SETTINGS = Settings.from_env()


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

    if not req.question:
        yield agui_events.run_error("질문이 비어 있습니다.", code="EMPTY_QUESTION")
        yield agui_events.run_finished(req.thread_id, req.run_id)
        return

    translator = StreamTranslator(SequentialIdFactory(req.run_id))
    sql_client = create_mcp_client(settings.sql_mcp_arn, settings.region)
    semantic_client = create_mcp_client(settings.semantic_mcp_arn, settings.region)
    session_manager = create_session_manager(
        settings.memory_id, req.actor_id, req.session_id, settings.region
    )

    try:
        with sql_client, semantic_client:
            sql_tools = sql_client.list_tools_sync()
            semantic_tools = semantic_client.list_tools_sync()
            builder = OrchestratorBuilder(
                settings=settings,
                sql_tools=sql_tools,
                semantic_tools=semantic_tools,
                session_manager=session_manager,
            )
            runner = _build_runner(builder, settings.mode)
            async for strands_event in runner.stream_async(req.question):
                for agui_event in translator.translate(strands_event):
                    yield agui_event
            for agui_event in translator.finalize():
                yield agui_event
    except Exception as exc:  # noqa: BLE001 — 어떤 실패든 AG-UI 오류로 사용자에 전달
        logger.exception("오케스트레이션 실패")
        for agui_event in translator.finalize():
            yield agui_event
        yield agui_events.run_error(f"처리 중 오류가 발생했습니다: {exc}", code="RUNTIME_ERROR")

    yield agui_events.run_finished(req.thread_id, req.run_id)


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
