# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Forced structured-output envelope 스키마 테스트.

envelopes.py 는 pydantic 만 의존(strands/boto3 무관) → 직접 import 가능.
필드명이 scoring.py 의 이벤트 계약(sql/code/verdict/kind 등)과 일치하는지,
verdict 정규화가 case 06 류 형식 실패를 막는지 검증.
"""

from __future__ import annotations

import pytest

from agent.envelopes import (
    ENVELOPE_MODELS,
    CodeEnvelope,
    ReportEnvelope,
    SqlEnvelope,
    ValidatorEnvelope,
    VizEnvelope,
)


def test_envelope_models_cover_all_subagents():
    assert set(ENVELOPE_MODELS) == {
        "ask_sql_specialist",
        "ask_code_specialist",
        "ask_validator",
        "ask_viz_specialist",
        "ask_report_specialist",
    }


def test_report_envelope_required_fields():
    # §49 최종 아키텍처: 커스텀 인터프리터에서 샌드박스가 직접 S3 업로드 → URI 반환.
    # 필수 = report_s3_uri/file_name/format/summary. invoke() 가 report 이벤트로 발행.
    env = ReportEnvelope(
        report_s3_uri="s3://bkt/reports/abc123/cost-report-2026-06.pdf",
        file_name="cost-report-2026-06.pdf",
        format="pdf",
        summary="6월 총비용 $84.4, 전월 대비 +12%.",
    )
    d = env.model_dump()
    assert d["report_s3_uri"].startswith("s3://")
    assert "/reports/" in d["report_s3_uri"]
    assert d["file_name"].endswith(".pdf")
    assert d["format"] == "pdf"
    assert d["page_count"] is None


def test_report_envelope_requires_uri():
    with pytest.raises(Exception):
        ReportEnvelope(file_name="x.pdf", format="pdf", summary="s")  # report_s3_uri 누락


def test_sql_envelope_dump_matches_event_contract():
    env = SqlEnvelope(sql="SELECT 1", row_count=3)
    d = env.model_dump()
    # scoring.extract_sql 가 result["sql"] 을 읽는다.
    assert d["sql"] == "SELECT 1"
    assert d["row_count"] == 3
    assert d["rows"] == []  # default


def test_sql_envelope_requires_sql():
    with pytest.raises(Exception):
        SqlEnvelope()  # sql 필수


def test_code_envelope_requires_code_and_summary():
    env = CodeEnvelope(result_summary="요약", code="import pandas")
    d = env.model_dump()
    assert d["code"] == "import pandas"  # scoring.extract_code
    assert d["result_summary"] == "요약"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("PASS", "PASS"),
        ("pass", "PASS"),
        ("PASS - looks correct", "PASS"),
        ("WARN: small sample", "WARN"),
        ("verdict is FAIL", "FAIL"),
        ("Fail", "FAIL"),
    ],
)
def test_validator_verdict_normalized(raw, expected):
    """case 06 타깃: 모델이 산문을 섞어도 표준 토큰으로 정규화."""
    env = ValidatorEnvelope(verdict=raw, reason="x")
    assert env.verdict == expected


def test_validator_fail_priority_over_pass():
    # 'PASS' 와 'FAIL' 이 둘 다 들어가면 FAIL 우선(안전 측).
    env = ValidatorEnvelope(verdict="not PASS, actually FAIL", reason="x")
    assert env.verdict == "FAIL"


def test_validator_confidence_bounds():
    with pytest.raises(Exception):
        ValidatorEnvelope(verdict="PASS", reason="x", confidence=1.5)


def test_viz_envelope_kind_required():
    env = VizEnvelope(kind="bar", x="email", y="cost_usd")
    d = env.model_dump()
    assert d["kind"] == "bar"  # scoring.extract_chart_kinds


def test_viz_envelope_y_can_be_list():
    env = VizEnvelope(kind="line", x="day", y=["cost", "tokens"])
    assert env.model_dump()["y"] == ["cost", "tokens"]


def test_extra_fields_preserved():
    # extra="allow" — 모델이 추가 필드 넣어도 보존(ok/explain_cost 등).
    env = SqlEnvelope(sql="SELECT 1", ok=True, explain_cost=42)
    d = env.model_dump()
    assert d.get("ok") is True
    assert d.get("explain_cost") == 42
