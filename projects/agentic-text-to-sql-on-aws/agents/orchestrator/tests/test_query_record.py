"""t2sql_query_record 구조화 로그 + 빌더 bundle 오버라이드 테스트."""

import asyncio
import json

from orchestrator import app as app_module
from orchestrator.agent_builder import OrchestratorBuilder
from orchestrator.app import QUERY_RECORD_MARKER, RunnerSession, _drive, log_query_record
from orchestrator.bundle_config import DEFAULT_BUNDLE_LABEL, BundleOverride
from orchestrator.config import Settings
from orchestrator.ids import SequentialIdFactory
from orchestrator.prompts import SYSTEM_PROMPT
from orchestrator.request import ParsedRequest
from orchestrator.session_cache import SessionCache
from orchestrator.stream_translator import StreamTranslator


def _drain(agen):
    async def _run():
        out = []
        async for item in agen:
            out.append(item)
        return out

    return asyncio.run(_run())


class FakeRunner:
    def __init__(self, events):
        self._events = events

    async def stream_async(self, task):
        self.task = task
        for e in self._events:
            yield e


def _req(session_id="s1", question="지역별 매출 상위 5개 지역을 알려줘"):
    return ParsedRequest(
        question=question,
        thread_id=session_id,
        run_id="r1",
        session_id=session_id,
        actor_id="anonymous",
    )


def _records(caplog):
    """캡처된 로그에서 query record dict 목록을 파싱."""
    out = []
    for record in caplog.records:
        message = record.getMessage()
        if QUERY_RECORD_MARKER in message:
            payload = message.split(QUERY_RECORD_MARKER, 1)[1].strip()
            out.append(json.loads(payload))
    return out


# --- log_query_record 직렬화 -------------------------------------------------


def test_log_query_record_shape(caplog):
    with caplog.at_level("INFO", logger="orchestrator"):
        returned = log_query_record(
            question="월별 주문 수",
            sql="SELECT 1",
            status="ok",
            session_id="s" * 40,
            bundle_label="b-1@v-2",
            app_version="sha-abc",
        )
    records = _records(caplog)
    assert len(records) == 1
    assert records[0] == returned
    assert records[0] == {
        "question": "월별 주문 수",
        "sql": "SELECT 1",
        "status": "ok",
        "session_id": "s" * 40,
        "version": {"bundle": "b-1@v-2", "agent": "sha-abc"},
    }


def test_log_query_record_keeps_korean_unescaped(caplog):
    with caplog.at_level("INFO", logger="orchestrator"):
        log_query_record(
            question="지역별 매출",
            sql=None,
            status="error",
            session_id="s1",
            bundle_label=DEFAULT_BUNDLE_LABEL,
            app_version="dev",
        )
    message = [r.getMessage() for r in caplog.records if QUERY_RECORD_MARKER in r.getMessage()][0]
    assert "지역별 매출" in message  # ensure_ascii=False
    assert _records(caplog)[0]["sql"] is None


# --- _drive 종료 지점 로깅 ----------------------------------------------------


def test_drive_logs_ok_record_with_last_sql(monkeypatch, caplog):
    monkeypatch.setattr(app_module, "SESSION_CACHE", SessionCache())
    events = [
        {
            "current_tool_use": {
                "toolUseId": "t1",
                "name": "sql-execution-mcp___run_sql",
                "input": json.dumps({"sql": "SELECT region FROM customers"}),
            }
        },
        {"data": "결과 요약"},
    ]
    session = RunnerSession(
        runner=FakeRunner(events),
        clients=[],
        session_manager=None,
        bundle_label="b-1@v-1",
        question="지역별 매출 상위 5개 지역을 알려줘",
    )
    translator = StreamTranslator(SequentialIdFactory("r1"))
    with caplog.at_level("INFO", logger="orchestrator"):
        _drain(_drive(session, _req(), translator, task="질문", app_version="sha-1"))

    records = _records(caplog)
    assert len(records) == 1
    assert records[0]["status"] == "ok"
    assert records[0]["sql"] == "SELECT region FROM customers"
    assert records[0]["version"] == {"bundle": "b-1@v-1", "agent": "sha-1"}
    assert records[0]["question"] == "지역별 매출 상위 5개 지역을 알려줘"


def test_drive_logs_clarification_status(monkeypatch, caplog):
    monkeypatch.setattr(app_module, "SESSION_CACHE", SessionCache())
    events = [
        {
            "type": "multiagent_node_interrupt",
            "node_id": "intent",
            "interrupts": [
                {"id": "i-1", "name": "clarification", "reason": {"question": "기간은?"}}
            ],
        }
    ]
    session = RunnerSession(runner=FakeRunner(events), clients=[], session_manager=None)
    translator = StreamTranslator(SequentialIdFactory("r1"))
    with caplog.at_level("INFO", logger="orchestrator"):
        _drain(_drive(session, _req(), translator, task="질문"))

    records = _records(caplog)
    assert records[0]["status"] == "clarification"
    assert records[0]["sql"] is None
    assert records[0]["version"]["bundle"] == DEFAULT_BUNDLE_LABEL
    assert records[0]["version"]["agent"] == "dev"


def test_fresh_orchestration_logs_error_record(monkeypatch, caplog):
    monkeypatch.setattr(app_module, "SESSION_CACHE", SessionCache())
    monkeypatch.setattr(app_module, "load_bundle_override", lambda *a, **k: None)

    class Boom:
        clients: list = []

        def start(self):
            raise RuntimeError("MCP 연결 실패")

        def sql_tools(self):
            return []

        def semantic_tools(self):
            return []

    monkeypatch.setattr(app_module, "create_tool_clients", lambda *a, **k: Boom())
    monkeypatch.setattr(app_module, "create_session_manager", lambda *a, **k: None)

    settings = Settings.from_env({"SQL_MCP_ARN": "a", "SEMANTIC_MCP_ARN": "b"})
    with caplog.at_level("INFO", logger="orchestrator"):
        out = _drain(app_module._fresh_orchestration(_req(), settings))

    assert any(e["type"] == "RUN_ERROR" for e in out)
    assert out[-1]["type"] == "RUN_FINISHED"
    records = _records(caplog)
    assert records[0]["status"] == "error"
    assert records[0]["version"] == {"bundle": DEFAULT_BUNDLE_LABEL, "agent": "dev"}


# --- StreamTranslator last_sql 추적 ------------------------------------------


def test_translator_tracks_last_run_sql_only():
    translator = StreamTranslator(SequentialIdFactory("r1"))
    list(
        translator.translate(
            {
                "current_tool_use": {
                    "toolUseId": "t0",
                    "name": "semantic___search_schema",
                    "input": json.dumps({"query": "매출"}),
                }
            }
        )
    )
    assert translator.last_sql is None

    for sql in ("SELECT 1", "SELECT 2"):
        list(
            translator.translate(
                {
                    "current_tool_use": {
                        "toolUseId": f"t-{sql}",
                        "name": "run_sql",
                        "input": {"sql": sql},
                    }
                }
            )
        )
    assert translator.last_sql == "SELECT 2"


def test_translator_ignores_partial_tool_input():
    translator = StreamTranslator(SequentialIdFactory("r1"))
    list(
        translator.translate(
            {
                "current_tool_use": {
                    "toolUseId": "t1",
                    "name": "run_sql",
                    "input": '{"sql": "SELE',
                }
            }
        )
    )
    assert translator.last_sql is None


# --- 빌더 bundle 오버라이드 ---------------------------------------------------


def _builder(bundle_override=None):
    return OrchestratorBuilder(
        settings=Settings.from_env({"MODEL_ID": "code-default-model"}),
        sql_tools=[],
        semantic_tools=[],
        bundle_override=bundle_override,
    )


def test_builder_defaults_without_override():
    builder = _builder()
    assert builder.system_prompt == SYSTEM_PROMPT
    assert builder.model_id == "code-default-model"
    assert builder.bundle_label == DEFAULT_BUNDLE_LABEL


def test_builder_applies_bundle_override():
    override = BundleOverride(
        system_prompt="번들 프롬프트", model_id="bundle-model", bundle_label="b@v"
    )
    builder = _builder(override)
    assert builder.system_prompt == "번들 프롬프트"
    assert builder.model_id == "bundle-model"
    assert builder.bundle_label == "b@v"


def test_builder_partial_override_keeps_code_default():
    override = BundleOverride(model_id="bundle-model", bundle_label="b@v")
    builder = _builder(override)
    assert builder.system_prompt == SYSTEM_PROMPT
    assert builder.model_id == "bundle-model"


# --- config additive ---------------------------------------------------------


def test_settings_bundle_and_app_version_defaults():
    s = Settings.from_env({})
    assert s.config_bundle_param == ""
    assert s.app_version == "dev"


def test_settings_reads_bundle_and_app_version():
    s = Settings.from_env(
        {"CONFIG_BUNDLE_PARAM": " /agentic-t2sql/active-bundle ", "APP_VERSION": " sha-1 "}
    )
    assert s.config_bundle_param == "/agentic-t2sql/active-bundle"
    assert s.app_version == "sha-1"
