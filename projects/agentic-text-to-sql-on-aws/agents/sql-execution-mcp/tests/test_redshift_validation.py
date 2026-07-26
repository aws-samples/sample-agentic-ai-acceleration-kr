"""Redshift dialect 검증기 유닛 테스트 — UNLOAD/COPY 등 Redshift 전용 위험 구문 차단 확인.

동일 규칙(default_rules)을 dialect="redshift"로 파싱해도 allow-list가 유지되는지 검증한다.
"""

from __future__ import annotations

import pytest

from sql_execution_mcp.validation import SqlValidationPipeline


@pytest.fixture
def pipeline() -> SqlValidationPipeline:
    return SqlValidationPipeline(dialect="redshift")


# ── 통과 케이스 ──────────────────────────────────────────────────────────


def test_simple_select_passes(pipeline: SqlValidationPipeline) -> None:
    result = pipeline.validate("SELECT id, name FROM customers WHERE active = true")
    assert result.ok, result.reason
    assert "LIMIT" in result.sql.upper()  # 자동 LIMIT 주입


def test_cte_and_aggregate_pass(pipeline: SqlValidationPipeline) -> None:
    result = pipeline.validate(
        "WITH recent AS (SELECT * FROM orders WHERE created_at > '2026-01-01') "
        "SELECT count(*) FROM recent"
    )
    assert result.ok, result.reason


# ── Redshift 전용 위험 구문 차단 ─────────────────────────────────────────


def test_unload_rejected(pipeline: SqlValidationPipeline) -> None:
    # UNLOAD는 sqlglot이 Command로 폴백 → statement_type/forbidden_node에서 거부.
    result = pipeline.validate(
        "UNLOAD ('select * from customers') TO 's3://bucket/dump' IAM_ROLE 'arn:aws:iam::1:role/r'"
    )
    assert not result.ok
    assert result.rule in {"statement_type", "forbidden_node"}


def test_copy_rejected(pipeline: SqlValidationPipeline) -> None:
    # COPY는 exp.Copy로 파싱 → 최상위가 Query가 아니므로 거부.
    result = pipeline.validate(
        "COPY customers FROM 's3://bucket/data' IAM_ROLE 'arn:aws:iam::1:role/r'"
    )
    assert not result.ok
    assert result.rule in {"statement_type", "forbidden_node"}


# ── 쓰기/DDL/다중문장 거부(dialect 무관 유지) ────────────────────────────


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO t (a) VALUES (1)",
        "UPDATE t SET a = 1 WHERE id = 2",
        "DELETE FROM t WHERE id = 1",
        "DROP TABLE t",
        "CREATE TABLE t (a int)",
        "GRANT SELECT ON t TO someone",
    ],
)
def test_write_and_ddl_rejected(pipeline: SqlValidationPipeline, sql: str) -> None:
    result = pipeline.validate(sql)
    assert not result.ok
    assert result.rule in {"statement_type", "forbidden_node"}


def test_multi_statement_rejected(pipeline: SqlValidationPipeline) -> None:
    result = pipeline.validate("SELECT 1; SELECT 2")
    assert not result.ok
    assert result.rule == "single_statement"


def test_stacked_unload_rejected(pipeline: SqlValidationPipeline) -> None:
    result = pipeline.validate("SELECT * FROM t; UNLOAD ('select 1') TO 's3://b'")
    assert not result.ok
    assert result.rule == "single_statement"
