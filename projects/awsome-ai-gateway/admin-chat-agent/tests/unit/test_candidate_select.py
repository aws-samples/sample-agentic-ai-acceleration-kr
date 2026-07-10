# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""candidate_select 실행기반 후보선택 단위 테스트 (DEVLOG §58, L3).

순수 함수(LLM/DB 불요)라 결정적으로 검증. 핵심: 다른 SQL 이 같은 결과셋이면
같은 클러스터, 틀린 SQL(fan-out N배 등)은 소수파로 탈락, 동률은 tie 로 표시.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent.candidate_select import (  # noqa: E402
    Candidate,
    normalize_resultset,
    resultset_hash,
    select_by_execution,
)


def _env(rows, ok=True, warnings=None, error=None):
    return {"ok": ok, "rows": rows, "row_count": len(rows or []),
            "accuracy_warnings": warnings or [], "error": error}


class TestNormalization:
    def test_column_order_invariant(self):
        a = [{"email": "x", "cost": 1.0}]
        b = [{"cost": 1.0, "email": "x"}]
        assert resultset_hash(a) == resultset_hash(b)

    def test_row_order_invariant_by_default(self):
        a = [{"m": "x", "c": 1}, {"m": "y", "c": 2}]
        b = [{"m": "y", "c": 2}, {"m": "x", "c": 1}]
        assert resultset_hash(a) == resultset_hash(b)

    def test_float_noise_collapsed(self):
        a = [{"c": 1.0000001}]
        b = [{"c": 1.0000002}]
        assert resultset_hash(a) == resultset_hash(b)

    def test_different_values_differ(self):
        a = [{"c": 1.0}]
        b = [{"c": 2.0}]
        assert resultset_hash(a) != resultset_hash(b)

    def test_order_sensitive_distinguishes_ranking(self):
        a = [{"m": "x", "c": 2}, {"m": "y", "c": 1}]
        b = [{"m": "y", "c": 1}, {"m": "x", "c": 2}]
        assert resultset_hash(a, order_sensitive=True) != resultset_hash(b, order_sensitive=True)


class TestSelection:
    def test_majority_wins_fanout_minority_dropped(self):
        # 2 후보 정답(합계 100), 1 후보 fan-out(합계 300) → 정답 다수파
        correct = _env([{"cost": 100.0}])
        correct2 = _env([{"cost": 100.0}])
        fanout = _env([{"cost": 300.0}])
        r = select_by_execution([Candidate("s1", correct), Candidate("s2", fanout), Candidate("s3", correct2)])
        assert r["winner_index"] in (0, 2)  # 정답 클러스터 대표
        assert r["agreement"] == round(2 / 3, 3)
        assert r["n_clusters"] == 2
        assert not r["tie"]

    def test_failed_candidates_excluded(self):
        ok1 = _env([{"c": 5}])
        failed = _env([], ok=False, error="UndefinedColumn")
        ok2 = _env([{"c": 5}])
        r = select_by_execution([Candidate("a", ok1), Candidate("b", failed), Candidate("c", ok2)])
        assert r["n_valid"] == 2
        assert r["agreement"] == 1.0  # 2/2 valid 합의
        assert r["winner_index"] in (0, 2)

    def test_tie_flagged(self):
        a = _env([{"c": 1}])
        b = _env([{"c": 2}])
        r = select_by_execution([Candidate("a", a), Candidate("b", b)])
        assert r["tie"] is True
        assert len(r["tie_indices"]) == 2

    def test_warning_penalty_breaks_tie(self):
        # 동률이지만 한쪽은 accuracy_warnings 있음 → 경고 없는 쪽 우선
        clean = _env([{"c": 1}])
        warned = _env([{"c": 2}], warnings=["fan-out 위험"])
        r = select_by_execution([Candidate("warned", warned), Candidate("clean", clean)])
        assert r["tie"] is False
        assert r["winner_index"] == 1  # clean

    def test_single_candidate_fallback(self):
        only = _env([{"c": 42}])
        r = select_by_execution([Candidate("only", only)])
        assert r["winner_index"] == 0
        assert r["agreement"] == 1.0
        assert not r["tie"]

    def test_all_failed_returns_none(self):
        f1 = _env([], ok=False, error="x")
        f2 = _env([], ok=False, error="y")
        r = select_by_execution([Candidate("a", f1), Candidate("b", f2)])
        assert r["winner_index"] is None
        assert r["n_valid"] == 0

    def test_empty(self):
        r = select_by_execution([])
        assert r["winner_index"] is None
        assert r["n_candidates"] == 0


class TestStructuredEnvelopeOk:
    """§60 회귀 가드: 구조화 SqlEnvelope 엔 `ok` 필드가 없다. ok 부재를 실행실패로
    오판하면 모든 후보가 무효(0/k·합의 0%)가 돼 L3 가 무력화된다(라이브 버그)."""

    def test_structured_envelope_without_ok_is_valid(self):
        # 실제 SqlEnvelope 모양 — ok 없음, sql+rows+row_count 만.
        env = {"sql": "SELECT 1", "rows": [{"c": 5}], "row_count": 1, "columns": []}
        c = Candidate("SELECT 1", env)
        assert c.ok is True  # ok 부재 ≠ 실패(sql 있고 명시적 에러 없음)

    def test_two_structured_candidates_reach_consensus(self):
        # ok 필드 없는 후보 2개가 같은 결과 → 합의 1.0(예전엔 0/2·0%였음).
        e1 = {"sql": "SELECT sum(cost) c FROM t", "rows": [{"c": 100.0}], "row_count": 1}
        e2 = {"sql": "SELECT sum(t.cost) c FROM t", "rows": [{"c": 100.0}], "row_count": 1}
        r = select_by_execution([Candidate("a", e1), Candidate("b", e2)])
        assert r["n_valid"] == 2
        assert r["agreement"] == 1.0
        assert r["winner_index"] in (0, 1)

    def test_explicit_error_still_excluded_without_ok(self):
        # ok 없어도 error 있으면 실패로 본다(자유텍스트 폴백 에러 envelope).
        bad = {"sql": "", "error": "UndefinedColumn", "rows": []}
        good = {"sql": "SELECT 1", "rows": [{"c": 5}], "row_count": 1}
        r = select_by_execution([Candidate("", bad), Candidate("SELECT 1", good)])
        assert r["n_valid"] == 1
        assert r["winner_index"] == 1

    def test_parse_error_excluded(self):
        # _agent_call 자유텍스트 폴백 실패 → {"response":..., "parse_error":True}.
        pe = {"response": "...", "parse_error": True}
        c = Candidate("", pe)
        assert c.ok is False
