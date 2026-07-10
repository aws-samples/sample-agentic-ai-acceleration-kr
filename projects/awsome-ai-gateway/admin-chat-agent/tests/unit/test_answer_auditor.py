# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""L5 answer auditor 게이트·fail-soft 단위 테스트 (§60).

안전이 핵심(L4 critic 와 동일 원칙): AUDITOR_ENABLED=OFF/비-deep/저위험이면 절대
호출 안 함(회귀 0), auditor 실패는 답변 불변(fail-soft), verdict 파싱 실패는 PASS 폴백.
auditor 는 **비파괴** — tool_results/답변 수치를 절대 고치지 않는다.
"""

from __future__ import annotations

import pytest

m = pytest.importorskip("agent.main", reason="agent runtime deps 필요")


def _tool_results(verdict="PASS"):
    return [{"tool": "ask_sql_specialist", "result": {
        "sql": "SELECT 1", "rows": [{"c": 4598}], "row_count": 4598,
        "validation": {"verdict": verdict, "reason": "ok"},
    }}]


def test_disabled_is_noop(monkeypatch):
    monkeypatch.setattr(m, "AUDITOR_ENABLED", False)
    # OFF → 큰 숫자·deep 이어도 미호출(None).
    assert m._run_answer_auditor("q", "총 4598건입니다", _tool_results(), "deep") is None


def test_non_deep_is_noop(monkeypatch):
    monkeypatch.setattr(m, "AUDITOR_ENABLED", True)
    # quick → 즉답성 위해 미호출.
    assert m._run_answer_auditor("q", "총 4598건입니다", _tool_results(), "quick") is None


def test_low_risk_skipped(monkeypatch):
    monkeypatch.setattr(m, "AUDITOR_ENABLED", True)
    # 큰 숫자 없음 + validator PASS → 저위험 미호출.
    assert m._run_answer_auditor("q", "안녕하세요", _tool_results("PASS"), "deep") is None


def test_high_risk_triggers_on_big_number(monkeypatch):
    monkeypatch.setattr(m, "AUDITOR_ENABLED", True)
    called = {}

    def fake_call(payload):
        called["yes"] = True
        return '{"verdict": "PASS", "defects": [], "confidence": 0.9, "reason": "일치"}'

    monkeypatch.setattr(m, "_auditor_call", fake_call)
    out = m._run_answer_auditor("q", "총 4598건입니다", _tool_results("PASS"), "deep")
    assert called.get("yes") is True  # 큰 숫자(4598) → 고위험 → 호출
    assert out["available"] is True
    assert out["verdict"] == "PASS"


def test_high_risk_triggers_on_validator_warn(monkeypatch):
    monkeypatch.setattr(m, "AUDITOR_ENABLED", True)
    monkeypatch.setattr(m, "_auditor_call",
                        lambda p: '{"verdict":"PASS","defects":[],"confidence":0.8,"reason":"ok"}')
    # 큰 숫자 없어도 validator WARN 이면 호출.
    out = m._run_answer_auditor("q", "소수 결과", _tool_results("WARN"), "deep")
    assert out is not None and out["available"] is True


def test_auditor_failure_failsoft(monkeypatch):
    monkeypatch.setattr(m, "AUDITOR_ENABLED", True)

    def boom(payload):
        raise RuntimeError("model down")

    monkeypatch.setattr(m, "_auditor_call", boom)
    out = m._run_answer_auditor("q", "총 4598건입니다", _tool_results("PASS"), "deep")
    # 실패 → available:false, 예외 전파 안 함(답변 차단 X).
    assert out is not None and out["available"] is False


def test_bad_verdict_falls_back_to_pass(monkeypatch):
    monkeypatch.setattr(m, "AUDITOR_ENABLED", True)
    monkeypatch.setattr(m, "_auditor_call",
                        lambda p: '{"verdict":"GARBAGE","defects":[],"confidence":0.5,"reason":"?"}')
    out = m._run_answer_auditor("q", "총 4598건입니다", _tool_results("PASS"), "deep")
    assert out["verdict"] == "PASS"  # 알 수 없는 verdict → 관대 폴백(차단 아님)


def test_retry_verdict_passthrough(monkeypatch):
    monkeypatch.setattr(m, "AUDITOR_ENABLED", True)
    monkeypatch.setattr(
        m, "_auditor_call",
        lambda p: '{"verdict":"RETRY","defects":[{"type":"B","body_value":4600,'
                  '"ground_values":[4598],"suggested_fix":"정확값 인용"}],'
                  '"confidence":0.7,"reason":"4600 vs 4598 drift"}',
    )
    out = m._run_answer_auditor("q", "약 4600건", _tool_results("PASS"), "deep")
    assert out["verdict"] == "RETRY"
    assert out["defects"][0]["type"] == "B"
