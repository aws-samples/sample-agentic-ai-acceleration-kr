"""run_sql 도구 통합 유닛 테스트 — 검증·실행·감사 로깅·datasource 라우팅. Data API mock."""

from __future__ import annotations

import json
import logging

from sql_execution_mcp import server


def _call_run_sql(sql: str, datasource: str | None = None):
    # FastMCP tool 데코레이터는 원함수를 그대로 반환하므로 직접 호출한다.
    if datasource is None:
        return server.run_sql(sql)
    return server.run_sql(sql, datasource)


def test_rejected_query_returns_rejected_and_audits(monkeypatch, caplog) -> None:
    # 실행기가 호출되지 않아야 함(거부는 실행 전에 차단).
    def _boom(datasource: str) -> None:
        raise AssertionError("거부된 쿼리는 실행기에 도달하면 안 됩니다.")

    monkeypatch.setattr(server, "_get_executor", _boom)

    # audit 로거는 propagate=False라 caplog 핸들러를 직접 붙여 캡처.
    audit_logger = logging.getLogger("sql_execution_mcp.audit")
    audit_logger.addHandler(caplog.handler)
    try:
        result = _call_run_sql("DROP TABLE customers")
    finally:
        audit_logger.removeHandler(caplog.handler)
    assert result["status"] == "rejected"
    assert result["rule"] in {"statement_type", "forbidden_node"}

    # 감사 로그가 structured JSON으로 기록되었는지 확인.
    audit_lines = [m for m in caplog.messages if '"event": "sql_rejected"' in m]
    assert audit_lines
    record = json.loads(audit_lines[-1])
    assert record["event"] == "sql_rejected"
    assert "sql_hash" in record
    assert "DROP" not in json.dumps(record)  # 원문 SQL 미노출


def test_ok_query_executes_and_returns_rows(monkeypatch) -> None:
    from sql_execution_mcp.executor import ExecutionResult

    class FakeExecutor:
        def __init__(self) -> None:
            self.last_sql = None

        def execute(self, sql: str) -> ExecutionResult:
            self.last_sql = sql
            return ExecutionResult(
                columns=["id"], rows=[[1], [2]], row_count=2, truncated=False
            )

    fake = FakeExecutor()
    monkeypatch.setattr(server, "_get_executor", lambda datasource: fake)

    result = _call_run_sql("SELECT id FROM customers")
    assert result["status"] == "ok"
    assert result["columns"] == ["id"]
    assert result["row_count"] == 2
    assert result["truncated"] is False
    # LIMIT 주입이 실행 SQL에 반영되었는지 확인.
    assert "LIMIT" in fake.last_sql.upper()


def test_execution_error_returns_error_message(monkeypatch) -> None:
    class FailingExecutor:
        def execute(self, sql: str):
            raise RuntimeError("relation \"customers\" does not exist")

    monkeypatch.setattr(server, "_get_executor", lambda datasource: FailingExecutor())

    result = _call_run_sql("SELECT id FROM customers")
    assert result["status"] == "error"
    assert "does not exist" in result["message"]


# ── datasource 라우팅 ────────────────────────────────────────────────────


class _RecordingExecutor:
    """실행된 SQL을 기록하는 fake 실행기."""

    def __init__(self) -> None:
        self.last_sql: str | None = None

    def execute(self, sql: str):
        from sql_execution_mcp.executor import ExecutionResult

        self.last_sql = sql
        return ExecutionResult(columns=["id"], rows=[[1]], row_count=1, truncated=False)


def test_default_datasource_is_aurora(monkeypatch) -> None:
    # datasource 미지정 시 aurora 파이프라인/실행기로 라우팅.
    seen: dict[str, str] = {}
    fake = _RecordingExecutor()

    def _get(datasource: str):
        seen["datasource"] = datasource
        return fake

    monkeypatch.setattr(server, "_get_executor", _get)
    result = _call_run_sql("SELECT id FROM customers")
    assert result["status"] == "ok"
    assert seen["datasource"] == "aurora"


def test_redshift_datasource_routes_to_redshift(monkeypatch) -> None:
    # redshift env 구성됨 + redshift 실행기로 라우팅.
    monkeypatch.setenv("REDSHIFT_WORKGROUP", "wg")
    monkeypatch.setenv("REDSHIFT_DB", "analytics")
    monkeypatch.setenv("REDSHIFT_SECRET_ARN", "arn:aws:secretsmanager:us-west-2:1:secret:ro")

    seen: dict[str, str] = {}
    fake = _RecordingExecutor()

    def _get(datasource: str):
        seen["datasource"] = datasource
        return fake

    monkeypatch.setattr(server, "_get_executor", _get)
    result = _call_run_sql("SELECT id FROM customers", "redshift")
    assert result["status"] == "ok"
    assert seen["datasource"] == "redshift"


def test_unknown_datasource_rejected(monkeypatch, caplog) -> None:
    # 실행기에 도달하기 전에 rejected 되어야 함.
    def _boom(datasource: str) -> None:
        raise AssertionError("unknown datasource는 실행기에 도달하면 안 됩니다.")

    monkeypatch.setattr(server, "_get_executor", _boom)

    audit_logger = logging.getLogger("sql_execution_mcp.audit")
    audit_logger.addHandler(caplog.handler)
    try:
        result = _call_run_sql("SELECT 1", "mysql")
    finally:
        audit_logger.removeHandler(caplog.handler)

    assert result["status"] == "rejected"
    assert result["rule"] == "unknown_datasource"
    # 감사 로그에 datasource 필드가 포함되는지 확인.
    audit_lines = [m for m in caplog.messages if '"event": "sql_rejected"' in m]
    assert audit_lines
    record = json.loads(audit_lines[-1])
    assert record["rule"] == "unknown_datasource"
    assert record["datasource"] == "mysql"


def test_redshift_not_configured_returns_graceful_error(monkeypatch) -> None:
    # redshift env 미설정 시 크래시 대신 error 응답.
    for name in ("REDSHIFT_WORKGROUP", "REDSHIFT_DB", "REDSHIFT_SECRET_ARN"):
        monkeypatch.delenv(name, raising=False)

    def _boom(datasource: str) -> None:
        raise AssertionError("미구성 상태에서 실행기를 만들면 안 됩니다.")

    monkeypatch.setattr(server, "_get_executor", _boom)
    result = _call_run_sql("SELECT 1", "redshift")
    assert result["status"] == "error"
    assert "Redshift 미구성" in result["message"]


def test_redshift_dialect_pipeline_used(monkeypatch) -> None:
    # redshift datasource는 redshift dialect 파이프라인을 사용해야 함.
    assert server._pipelines["redshift"].dialect == "redshift"
    assert server._pipelines["aurora"].dialect == "postgres"
