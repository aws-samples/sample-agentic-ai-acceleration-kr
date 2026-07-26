"""SQL 검증기 유닛 테스트 — allow-list, 우회 시도, LIMIT 주입."""

from __future__ import annotations

import pytest

from sql_execution_mcp.validation import (
    DEFAULT_ROW_LIMIT,
    MAX_ROW_LIMIT,
    SqlValidationPipeline,
)


@pytest.fixture
def pipeline() -> SqlValidationPipeline:
    return SqlValidationPipeline()


# ── 통과 케이스 ──────────────────────────────────────────────────────────


def test_simple_select_passes(pipeline: SqlValidationPipeline) -> None:
    result = pipeline.validate("SELECT id, name FROM customers WHERE active = true")
    assert result.ok
    assert result.sql is not None
    assert "LIMIT" in result.sql.upper()  # 자동 LIMIT 주입


def test_cte_passes(pipeline: SqlValidationPipeline) -> None:
    result = pipeline.validate(
        "WITH recent AS (SELECT * FROM orders WHERE created_at > '2026-01-01') "
        "SELECT count(*) FROM recent"
    )
    assert result.ok, result.reason


def test_union_passes(pipeline: SqlValidationPipeline) -> None:
    result = pipeline.validate("SELECT id FROM a UNION SELECT id FROM b")
    assert result.ok, result.reason


def test_join_and_aggregate_pass(pipeline: SqlValidationPipeline) -> None:
    result = pipeline.validate(
        "SELECT r.region, sum(o.amount) FROM orders o "
        "JOIN regions r ON o.region_id = r.id GROUP BY r.region"
    )
    assert result.ok, result.reason


# ── 쓰기/DDL 거부 ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO t (a) VALUES (1)",
        "UPDATE t SET a = 1 WHERE id = 2",
        "DELETE FROM t WHERE id = 1",
        "DROP TABLE t",
        "CREATE TABLE t (a int)",
        "ALTER TABLE t ADD COLUMN c int",
        "TRUNCATE TABLE t",
        "GRANT SELECT ON t TO someone",
        "MERGE INTO t USING s ON t.i = s.i WHEN MATCHED THEN DELETE",
        "COPY t FROM '/etc/passwd'",
        "SET search_path TO public",
    ],
)
def test_write_and_ddl_rejected(pipeline: SqlValidationPipeline, sql: str) -> None:
    result = pipeline.validate(sql)
    assert not result.ok
    assert result.rule in {"statement_type", "forbidden_node"}


def test_vacuum_command_rejected(pipeline: SqlValidationPipeline) -> None:
    # sqlglot이 Command로 폴백하는 문장(VACUUM/EXPLAIN 등)도 거부.
    result = pipeline.validate("VACUUM FULL")
    assert not result.ok


# ── 다중 statement 거부 ──────────────────────────────────────────────────


def test_multi_statement_rejected(pipeline: SqlValidationPipeline) -> None:
    result = pipeline.validate("SELECT 1; SELECT 2")
    assert not result.ok
    assert result.rule == "single_statement"


def test_stacked_write_rejected(pipeline: SqlValidationPipeline) -> None:
    result = pipeline.validate("SELECT * FROM t; DROP TABLE t")
    assert not result.ok
    assert result.rule == "single_statement"


def test_trailing_semicolon_ok(pipeline: SqlValidationPipeline) -> None:
    # 후행 세미콜론 하나는 단일 문장으로 취급.
    result = pipeline.validate("SELECT 1;")
    assert result.ok, result.reason


# ── 시스템 카탈로그 차단 ─────────────────────────────────────────────────


def test_pg_catalog_rejected(pipeline: SqlValidationPipeline) -> None:
    result = pipeline.validate("SELECT * FROM pg_catalog.pg_tables")
    assert not result.ok
    assert result.rule == "system_catalog"


def test_information_schema_rejected(pipeline: SqlValidationPipeline) -> None:
    result = pipeline.validate("SELECT table_name FROM information_schema.tables")
    assert not result.ok
    assert result.rule == "system_catalog"


def test_pg_prefix_table_rejected(pipeline: SqlValidationPipeline) -> None:
    result = pipeline.validate("SELECT * FROM pg_stat_activity")
    assert not result.ok
    assert result.rule == "system_catalog"


# ── CTE/서브쿼리 우회 시도 거부 ─────────────────────────────────────────


def test_cte_with_delete_rejected(pipeline: SqlValidationPipeline) -> None:
    # WITH x AS (DELETE ... RETURNING) SELECT — 최상위는 Select지만 내부에 Delete.
    result = pipeline.validate(
        "WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x"
    )
    assert not result.ok
    assert result.rule == "forbidden_node"


def test_cte_with_insert_rejected(pipeline: SqlValidationPipeline) -> None:
    result = pipeline.validate(
        "WITH x AS (INSERT INTO t VALUES (1) RETURNING *) SELECT * FROM x"
    )
    assert not result.ok
    assert result.rule == "forbidden_node"


def test_subquery_update_rejected(pipeline: SqlValidationPipeline) -> None:
    result = pipeline.validate(
        "SELECT * FROM (UPDATE t SET a = 1 RETURNING *) AS s"
    )
    assert not result.ok


def test_select_into_rejected(pipeline: SqlValidationPipeline) -> None:
    result = pipeline.validate("SELECT * INTO backup FROM customers")
    assert not result.ok
    assert result.rule == "forbidden_node"


def test_comment_bypass_rejected(pipeline: SqlValidationPipeline) -> None:
    # 주석 뒤에 숨긴 두 번째 문장은 여전히 다중 statement로 파싱되어 거부.
    result = pipeline.validate("SELECT 1 /* harmless */; DROP TABLE t")
    assert not result.ok
    assert result.rule == "single_statement"


def test_comment_inline_ddl_rejected(pipeline: SqlValidationPipeline) -> None:
    result = pipeline.validate("SELECT * FROM t -- comment\n; DELETE FROM t")
    assert not result.ok


# ── 위험 함수 차단 ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT pg_sleep(10)",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT lo_export(1, '/tmp/x')",
    ],
)
def test_dangerous_functions_rejected(pipeline: SqlValidationPipeline, sql: str) -> None:
    result = pipeline.validate(sql)
    assert not result.ok
    assert result.rule == "forbidden_node"


# ── LIMIT 주입 ───────────────────────────────────────────────────────────


def test_limit_injected_when_absent(pipeline: SqlValidationPipeline) -> None:
    result = pipeline.validate("SELECT * FROM big_table")
    assert result.ok
    assert f"LIMIT {DEFAULT_ROW_LIMIT}" in result.sql.upper().replace("  ", " ")


def test_limit_capped_when_over_max(pipeline: SqlValidationPipeline) -> None:
    result = pipeline.validate("SELECT * FROM big_table LIMIT 999999")
    assert result.ok
    assert str(MAX_ROW_LIMIT) in result.sql
    assert "999999" not in result.sql


def test_limit_preserved_when_under_max(pipeline: SqlValidationPipeline) -> None:
    result = pipeline.validate("SELECT * FROM t LIMIT 10")
    assert result.ok
    assert "10" in result.sql
    assert str(MAX_ROW_LIMIT) not in result.sql or MAX_ROW_LIMIT == 10


# ── 파싱 실패 / 빈 입력 ──────────────────────────────────────────────────


def test_garbage_rejected(pipeline: SqlValidationPipeline) -> None:
    result = pipeline.validate("this is not sql !!!")
    assert not result.ok


def test_empty_rejected(pipeline: SqlValidationPipeline) -> None:
    result = pipeline.validate("   ")
    assert not result.ok
