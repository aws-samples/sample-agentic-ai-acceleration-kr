"""RDS Data API 실행기 유닛 테스트 — Data API는 mock."""

from __future__ import annotations

from unittest.mock import MagicMock

from sql_execution_mcp.executor import SqlExecutor


def _make_executor(client: MagicMock, max_rows: int = 500) -> SqlExecutor:
    return SqlExecutor(
        cluster_arn="arn:aws:rds:us-west-2:123456789012:cluster:test",
        secret_arn="arn:aws:secretsmanager:us-west-2:123456789012:secret:agent_ro",
        db_name="appdb",
        region="us-west-2",
        max_rows=max_rows,
        client=client,
    )


def test_execute_normalizes_columns_and_rows() -> None:
    client = MagicMock()
    client.execute_statement.return_value = {
        "columnMetadata": [{"label": "id"}, {"label": "name"}, {"label": "active"}],
        "records": [
            [{"longValue": 1}, {"stringValue": "Alice"}, {"booleanValue": True}],
            [{"longValue": 2}, {"stringValue": "Bob"}, {"isNull": True}],
        ],
    }
    result = _make_executor(client).execute("SELECT id, name, active FROM t LIMIT 200")

    assert result.columns == ["id", "name", "active"]
    assert result.rows == [[1, "Alice", True], [2, "Bob", None]]
    assert result.row_count == 2
    assert result.truncated is False


def test_execute_forbids_continue_after_timeout() -> None:
    client = MagicMock()
    client.execute_statement.return_value = {"columnMetadata": [], "records": []}
    _make_executor(client).execute("SELECT 1")

    kwargs = client.execute_statement.call_args.kwargs
    assert kwargs["continueAfterTimeout"] is False
    assert kwargs["includeResultMetadata"] is True
    assert kwargs["secretArn"].endswith("agent_ro")


def test_execute_truncates_over_max_rows() -> None:
    client = MagicMock()
    client.execute_statement.return_value = {
        "columnMetadata": [{"label": "n"}],
        "records": [[{"longValue": i}] for i in range(10)],
    }
    result = _make_executor(client, max_rows=3).execute("SELECT n FROM t")

    assert result.row_count == 3
    assert result.truncated is True
    assert result.rows == [[0], [1], [2]]


def test_execute_handles_array_values() -> None:
    client = MagicMock()
    client.execute_statement.return_value = {
        "columnMetadata": [{"label": "tags"}],
        "records": [[{"arrayValue": {"stringValues": ["a", "b"]}}]],
    }
    result = _make_executor(client).execute("SELECT tags FROM t")
    assert result.rows == [[["a", "b"]]]


def test_execute_column_fallback_to_name() -> None:
    client = MagicMock()
    client.execute_statement.return_value = {
        "columnMetadata": [{"name": "cnt"}],
        "records": [[{"longValue": 42}]],
    }
    result = _make_executor(client).execute("SELECT count(*) AS cnt FROM t")
    assert result.columns == ["cnt"]
