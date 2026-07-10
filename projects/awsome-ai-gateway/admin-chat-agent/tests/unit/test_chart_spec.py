# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""_valid_chart_spec + chart 추출 테스트 (DEVLOG §35).

render_chart tool 이 만든 spec 과 텍스트 추출 spec 둘 다 ChartRenderer
계약({kind,data,encoding})을 만족해야 chart 이벤트로 발행된다.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def valid(agent_pure_fns):
    return agent_pure_fns["_valid_chart_spec"]


@pytest.fixture
def extract(agent_pure_fns):
    return agent_pure_fns["_extract_chart_specs"]


# ─── _valid_chart_spec ───
def test_valid_full_spec(valid):
    assert valid({"kind": "bar", "data": [{"x": 1}], "encoding": {"x": "a", "y": "b"}})


def test_invalid_missing_data(valid):
    assert not valid({"kind": "bar", "encoding": {"x": "a"}})


def test_invalid_missing_kind(valid):
    assert not valid({"data": [], "encoding": {}})


def test_invalid_encoding_not_dict(valid):
    assert not valid({"kind": "bar", "data": [], "encoding": "x"})


def test_invalid_non_dict(valid):
    assert not valid("not a dict")
    assert not valid(None)


def test_render_chart_spec_shape_passes(valid):
    """render_chart tool 이 반환하는 spec 형태가 검증 통과해야 (chart 이벤트 발행 조건)."""
    # render_chart: {"kind", "data", "encoding": {"x","y",color?}, "title"}
    spec = {"kind": "line", "data": [{"day": "2026-06-01", "cost": 1.2}],
            "encoding": {"x": "day", "y": "cost"}, "title": "추이"}
    assert valid(spec)


# ─── _extract_chart_specs (텍스트 fallback) ───
def test_extract_fenced_chart(extract):
    text = '결과:\n```chart\n{"kind":"bar","data":[{"a":1}],"encoding":{"x":"a","y":"b"}}\n```'
    specs = extract(text)
    assert len(specs) == 1
    assert specs[0][0]["kind"] == "bar"


def test_extract_bare_json_chart(extract):
    text = '차트: {"kind":"pie","data":[{"m":1}],"encoding":{"x":"m","y":"n"}}'
    specs = extract(text)
    assert len(specs) == 1
    assert specs[0][0]["kind"] == "pie"


def test_extract_ignores_non_chart_json(extract):
    """encoding/data 없는 JSON 은 차트 아님."""
    text = '{"sql": "SELECT 1", "row_count": 5}'
    assert extract(text) == []
