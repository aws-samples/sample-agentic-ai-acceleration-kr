# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""_parse_agent_json 견고화 테스트 (DEVLOG §34).

sub-agent 가 envelope JSON 대신 산문/마크다운, 또는 여러 JSON 조각을 섞어
답하는 경우에도 기대 키(sql/code/verdict)를 가진 envelope 를 우선 추출하는지.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def parse(agent_pure_fns):
    return agent_pure_fns["_parse_agent_json"]


def test_clean_json_fence(parse):
    text = '결과입니다:\n```json\n{"sql": "SELECT 1", "row_count": 5}\n```'
    r = parse(text, ("sql", "row_count"))
    assert r["sql"] == "SELECT 1"


def test_bare_json(parse):
    text = '{"sql": "SELECT 1", "rows": [], "row_count": 0}'
    r = parse(text, ("sql",))
    assert r["sql"] == "SELECT 1"


def test_picks_envelope_among_multiple_json(parse):
    """산문에 IAM statement 같은 엉뚱한 JSON 이 섞여도 sql envelope 우선."""
    text = (
        '권한 오류 예시: {"Effect": "Allow", "Action": "lambda:Invoke"}\n'
        '실제 결과: {"sql": "SELECT email FROM auth.users", "row_count": 2, "rows": []}'
    )
    r = parse(text, ("sql", "row_count"))
    assert r is not None
    assert "sql" in r, f"sql envelope 를 골라야 하는데: {r}"
    assert r["sql"].startswith("SELECT")


def test_picks_most_key_matches(parse):
    text = (
        '{"sql": "SELECT 1"}\n'
        '{"sql": "SELECT 2", "rows": [], "row_count": 3, "columns": []}'
    )
    r = parse(text, ("sql", "rows", "row_count"))
    # 키 매치 더 많은 두 번째
    assert r["sql"] == "SELECT 2"


def test_code_envelope_keys(parse):
    text = '{"result_summary": "STL 분해 완료", "code": "from statsmodels...", "data": null}'
    r = parse(text, ("code", "result_summary", "data"))
    assert "code" in r


def test_no_json_returns_none(parse):
    """순수 마크다운 표(JSON 없음) → None (호출측 prose fallback)."""
    text = "## 결과\n| email | role |\n|---|---|\n| a@b.com | ADMIN |"
    assert parse(text, ("sql",)) is None


def test_no_expect_keys_returns_first(parse):
    text = '{"foo": 1}\n{"bar": 2}'
    r = parse(text)
    assert r == {"foo": 1}


def test_nested_json_balanced_scan(parse):
    text = '응답: {"verdict": "PASS", "reason": "ok", "meta": {"nested": true}}'
    r = parse(text, ("verdict", "reason"))
    assert r["verdict"] == "PASS"
    assert r["meta"]["nested"] is True
