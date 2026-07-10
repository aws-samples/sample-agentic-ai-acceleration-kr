# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""자동 검증(§58 결함⑨ 근본 해소) 단위 테스트.

validator 는 orchestrator(LLM)가 부를지 말지 선택하는 게 아니라, SQL 이 생성되면
ask_sql_specialist/ask_sql_verified 안에서 **코드로 항상** 실행돼야 한다(비결정성 0).
이 테스트가 그 보장을 고정한다.
"""

from __future__ import annotations

import pytest

m = pytest.importorskip("agent.main", reason="agent runtime deps 필요")


def _patch_validator(monkeypatch):
    """_run_validator 를 가짜로 — 호출 여부/인자만 관측. 실제 LLM 안 탐."""
    calls = []

    def fake_run(user_question, generated_sql, sample_rows, schema_used,
                 row_count, accuracy_warnings=None, *, stash=True):
        calls.append({"sql": generated_sql, "warnings": accuracy_warnings, "rows": row_count})
        return {"verdict": "PASS", "reason": "fake", "confidence": 0.9}

    monkeypatch.setattr(m, "_run_validator", fake_run)
    return calls


def test_auto_validate_runs_on_structured_envelope_without_ok(monkeypatch):
    """⚠️ 회귀 가드: structured output(SqlEnvelope)엔 `ok` 필드가 없다. ok 에
    의존하면 validator 가 라이브에서 한 번도 안 도는 버그(§58 결함⑨ 라이브)."""
    calls = _patch_validator(monkeypatch)
    # 실제 structured 엔벨로프 모양 — ok 없음!
    env = {"sql": "SELECT SUM(cost_usd) FROM usage.usage_logs", "rows": [{"sum": 5}],
           "row_count": 1, "columns": [{"name": "sum"}], "s3_uri": None, "note": None}
    m._auto_validate("이번 달 비용", env)
    assert len(calls) == 1, "ok 없는 structured envelope 에도 validator 가 돌아야 함"
    assert env["validation"]["verdict"] == "PASS"


def test_auto_validate_passes_accuracy_warnings(monkeypatch):
    calls = _patch_validator(monkeypatch)
    env = {"sql": "SELECT 1", "rows": [], "row_count": 0, "columns": [],
           "accuracy_warnings": ["fan-out 위험"]}
    m._auto_validate("q", env)
    assert calls[0]["warnings"] == ["fan-out 위험"]


def test_auto_validate_skips_when_no_sql(monkeypatch):
    calls = _patch_validator(monkeypatch)
    m._auto_validate("q", {"sql": "", "rows": []})           # SQL 빈 문자열
    m._auto_validate("q", {"ok": False, "error": "boom"})    # 명시적 에러
    m._auto_validate("q", {"sql": "SELECT 1", "error": "x"}) # 에러 동반
    m._auto_validate("q", {"response": "not an envelope"})   # SQL 키 없음
    assert len(calls) == 0


def test_auto_validate_no_double_validation(monkeypatch):
    calls = _patch_validator(monkeypatch)
    env = {"sql": "SELECT 1", "rows": [], "row_count": 0, "columns": []}
    m._auto_validate("q", env)
    m._auto_validate("q", env)  # 이미 validation 있으면 재실행 안 함
    assert len(calls) == 1


def test_auto_validate_graceful_on_validator_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("validator down")
    monkeypatch.setattr(m, "_run_validator", boom)
    env = {"sql": "SELECT 1", "rows": [], "row_count": 0, "columns": []}
    m._auto_validate("q", env)
    # 검증 호출 실패해도 본 경로 안 막고 WARN 으로 표시.
    assert env["validation"]["verdict"] == "WARN"
