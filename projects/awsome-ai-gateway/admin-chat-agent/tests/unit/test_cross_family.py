# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""L4 cross-family critic 게이트·fail-soft 단위 테스트 (§59).

안전이 핵심: CRITIC_ENABLED=OFF/비-deep/저위험이면 절대 호출 안 함(회귀 0),
critic 실패는 본 검증 verdict 불변(fail-open), 의견 불일치는 WARN 격상만(차단 X).
"""

from __future__ import annotations

import pytest

m = pytest.importorskip("agent.main", reason="agent runtime deps 필요")


def _env(verdict="PASS", **kw):
    e = {"sql": "SELECT 1", "rows": [], "row_count": 0, "columns": [],
         "validation": {"verdict": verdict, "reason": "ok"}}
    e.update(kw)
    return e


def test_high_risk_signals():
    assert m._is_high_risk(_env("WARN")) is True
    assert m._is_high_risk(_env("FAIL")) is True
    assert m._is_high_risk(_env("PASS", verification={"tie": True})) is True
    assert m._is_high_risk(_env("PASS", accuracy_warnings=["fan-out"])) is True
    assert m._is_high_risk(_env("PASS")) is False  # 저위험 — 미호출 대상


def test_disabled_is_noop(monkeypatch):
    monkeypatch.setattr(m, "CRITIC_ENABLED", False)
    env = _env("WARN")
    m._cross_family_check("q", env, "deep")
    assert "cross_family" not in env["validation"]  # OFF → 손도 안 댐


def test_non_deep_is_noop(monkeypatch):
    monkeypatch.setattr(m, "CRITIC_ENABLED", True)
    env = _env("WARN")
    m._cross_family_check("q", env, "quick")  # quick → 미호출(즉답성)
    assert "cross_family" not in env["validation"]


def test_low_risk_skipped(monkeypatch):
    monkeypatch.setattr(m, "CRITIC_ENABLED", True)
    env = _env("PASS")  # 저위험
    m._cross_family_check("q", env, "deep")
    assert "cross_family" not in env["validation"]


def test_critic_unavailable_failsoft(monkeypatch):
    # critic 호출 실패(미설치/미인증) → cross_family={available:false}, verdict 불변.
    monkeypatch.setattr(m, "CRITIC_ENABLED", True)

    def _boom(_payload):
        raise ImportError("openai 미설치")

    monkeypatch.setattr(m, "_get_critic_call", lambda: _boom)
    env = _env("WARN")
    m._cross_family_check("q", env, "deep")
    assert env["validation"]["cross_family"]["available"] is False
    assert env["validation"]["verdict"] == "WARN"  # 불변(fail-open)


def test_disagreement_escalates_pass_to_warn(monkeypatch):
    # Claude PASS 인데 cross-family 가 의미 우려(FAIL) → PASS→WARN 격상(차단 아님).
    monkeypatch.setattr(m, "CRITIC_ENABLED", True)
    monkeypatch.setattr(
        m, "_get_critic_call",
        lambda: (lambda _p: '{"verdict":"FAIL","restated_intent":"전체 합산","reason":"성공필터 누락"}'),
    )
    env = _env("PASS", accuracy_warnings=["dashboard 정합"])  # 고위험으로
    m._cross_family_check("q", env, "deep")
    cf = env["validation"]["cross_family"]
    assert cf["available"] is True and cf["verdict"] == "FAIL"
    assert env["validation"]["verdict"] == "WARN"       # PASS→WARN 격상
    assert env["validation"]["disagreement"] is True


def test_agreement_keeps_verdict(monkeypatch):
    # cross-family 도 PASS → 격상 없음.
    monkeypatch.setattr(m, "CRITIC_ENABLED", True)
    monkeypatch.setattr(
        m, "_get_critic_call",
        lambda: (lambda _p: '{"verdict":"PASS","restated_intent":"성공 호출 합산","reason":"일치"}'),
    )
    env = _env("PASS", accuracy_warnings=["x"])
    m._cross_family_check("q", env, "deep")
    assert env["validation"]["verdict"] == "PASS"  # 둘 다 PASS → 불변
    assert "disagreement" not in env["validation"]
