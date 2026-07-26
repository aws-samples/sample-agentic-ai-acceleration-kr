"""RedshiftDataApiExecutor 유닛 테스트 — Redshift Data API는 fake(비동기 폴링 모사).

실제 AWS 호출·대기 없이 sleep 을 주입하고 클라이언트를 fake로 대체한다.
"""

from __future__ import annotations

from typing import Any

import pytest

from sql_execution_mcp.executor import (
    ExecutorError,
    RedshiftDataApiExecutor,
)


class FakeRedshiftClient:
    """redshift-data 클라이언트 fake.

    ``statuses`` 는 describe_statement가 순서대로 반환할 Status 시퀀스.
    마지막 값은 계속 반복된다(폴링 여러 번 대응).
    """

    def __init__(
        self,
        statuses: list[str],
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        self._statuses = statuses
        self._result = result or {"ColumnMetadata": [], "Records": []}
        self._error = error
        self._describe_calls = 0
        self.cancelled = False
        self.executed_sql: str | None = None
        self.execute_kwargs: dict[str, Any] = {}

    def execute_statement(self, **kwargs: Any) -> dict[str, Any]:
        self.executed_sql = kwargs.get("Sql")
        self.execute_kwargs = kwargs
        return {"Id": "stmt-1"}

    def describe_statement(self, Id: str) -> dict[str, Any]:  # noqa: N803 — boto3 규격
        idx = min(self._describe_calls, len(self._statuses) - 1)
        status = self._statuses[idx]
        self._describe_calls += 1
        description: dict[str, Any] = {"Status": status}
        if status in {"FAILED", "ABORTED"} and self._error:
            description["Error"] = self._error
        return description

    def get_statement_result(self, Id: str) -> dict[str, Any]:  # noqa: N803
        return self._result

    def cancel_statement(self, Id: str) -> dict[str, Any]:  # noqa: N803
        self.cancelled = True
        return {"Status": True}


def _make_executor(client: FakeRedshiftClient, **kwargs: Any) -> RedshiftDataApiExecutor:
    calls: list[float] = []
    return RedshiftDataApiExecutor(
        workgroup="wg",
        db_name="analytics",
        secret_arn="arn:aws:secretsmanager:us-west-2:1:secret:agent_ro",
        region="us-west-2",
        client=client,
        poll_interval=0.5,
        timeout=kwargs.pop("timeout", 60.0),
        sleep=lambda s: calls.append(s),
        **kwargs,
    )


# ── 정상 실행/폴링 ───────────────────────────────────────────────────────


def test_execute_polls_until_finished_and_normalizes() -> None:
    client = FakeRedshiftClient(
        statuses=["SUBMITTED", "STARTED", "FINISHED"],
        result={
            "ColumnMetadata": [{"label": "id"}, {"label": "name"}, {"label": "active"}],
            "Records": [
                [{"longValue": 1}, {"stringValue": "Alice"}, {"booleanValue": True}],
                [{"longValue": 2}, {"stringValue": "Bob"}, {"isNull": True}],
            ],
        },
    )
    result = _make_executor(client).execute("SELECT id, name, active FROM t")

    assert result.columns == ["id", "name", "active"]
    assert result.rows == [[1, "Alice", True], [2, "Bob", None]]
    assert result.row_count == 2
    assert result.truncated is False
    assert client.executed_sql == "SELECT id, name, active FROM t"


def test_execute_passes_workgroup_db_secret() -> None:
    client = FakeRedshiftClient(statuses=["FINISHED"])
    _make_executor(client).execute("SELECT 1")
    kwargs = client.execute_kwargs
    assert kwargs["WorkgroupName"] == "wg"
    assert kwargs["Database"] == "analytics"
    assert kwargs["SecretArn"].endswith("agent_ro")


def test_execute_normalizes_double_value() -> None:
    client = FakeRedshiftClient(
        statuses=["FINISHED"],
        result={
            "ColumnMetadata": [{"label": "amt"}],
            "Records": [[{"doubleValue": 3.5}]],
        },
    )
    result = _make_executor(client).execute("SELECT amt FROM t")
    assert result.rows == [[3.5]]


def test_execute_column_fallback_to_name() -> None:
    client = FakeRedshiftClient(
        statuses=["FINISHED"],
        result={"ColumnMetadata": [{"name": "cnt"}], "Records": [[{"longValue": 42}]]},
    )
    result = _make_executor(client).execute("SELECT count(*) AS cnt FROM t")
    assert result.columns == ["cnt"]


# ── truncation 계약(Aurora와 동일) ───────────────────────────────────────


def test_execute_truncates_over_max_rows() -> None:
    client = FakeRedshiftClient(
        statuses=["FINISHED"],
        result={
            "ColumnMetadata": [{"label": "n"}],
            "Records": [[{"longValue": i}] for i in range(10)],
        },
    )
    result = _make_executor(client, max_rows=3).execute("SELECT n FROM t")
    assert result.row_count == 3
    assert result.truncated is True
    assert result.rows == [[0], [1], [2]]


# ── 실패/취소/타임아웃 ───────────────────────────────────────────────────


def test_execute_raises_on_failed_status() -> None:
    client = FakeRedshiftClient(
        statuses=["STARTED", "FAILED"],
        error="syntax error at or near \"slect\"",
    )
    with pytest.raises(ExecutorError) as exc:
        _make_executor(client).execute("SLECT 1")
    assert "FAILED" in str(exc.value)
    assert "syntax error" in str(exc.value)


def test_execute_raises_on_aborted_status() -> None:
    client = FakeRedshiftClient(statuses=["ABORTED"], error="aborted by admin")
    with pytest.raises(ExecutorError) as exc:
        _make_executor(client).execute("SELECT 1")
    assert "ABORTED" in str(exc.value)


def test_execute_times_out_and_cancels() -> None:
    # 절대 FINISHED가 안 되는 상태 → 타임아웃 → cancel_statement 시도.
    client = FakeRedshiftClient(statuses=["STARTED"])
    # timeout=1.0, poll_interval=0.5 → 2회 폴링 후 타임아웃.
    with pytest.raises(ExecutorError) as exc:
        _make_executor(client, timeout=1.0).execute("SELECT pg_sleep(999)")
    assert "타임아웃" in str(exc.value)
    assert client.cancelled is True


def test_timeout_cancel_failure_is_ignored() -> None:
    # cancel_statement가 던져도 타임아웃 ExecutorError로 정규화되어야 함.
    client = FakeRedshiftClient(statuses=["STARTED"])

    def _raise(Id: str):  # noqa: N803
        raise RuntimeError("cancel failed")

    client.cancel_statement = _raise  # type: ignore[method-assign]
    with pytest.raises(ExecutorError) as exc:
        _make_executor(client, timeout=1.0).execute("SELECT 1")
    assert "타임아웃" in str(exc.value)


def test_finished_immediately_does_not_sleep() -> None:
    # 첫 describe에서 FINISHED면 sleep 없이 결과 반환.
    slept: list[float] = []
    client = FakeRedshiftClient(statuses=["FINISHED"])
    executor = RedshiftDataApiExecutor(
        workgroup="wg",
        db_name="analytics",
        secret_arn="arn:secret",
        region="us-west-2",
        client=client,
        sleep=lambda s: slept.append(s),
    )
    executor.execute("SELECT 1")
    assert slept == []
