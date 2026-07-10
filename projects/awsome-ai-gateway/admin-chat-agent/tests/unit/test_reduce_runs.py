# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""N-run 다수결 집계(scoring.reduce_runs) 테스트.

⑤ 측정 안정화: 케이스를 N회 실행해 과반 통과를 대표 판정으로, 변동성(flaky)을
분리 보고하는지 검증.
"""

from __future__ import annotations

from tests.eval import scoring


def _run(passed: bool, rate: float, cid="01", tier="A") -> dict:
    return {
        "case_id": cid,
        "tier": tier,
        "name": "t",
        "checks": {"a": passed},
        "details": {},
        "passed": passed,
        "pass_rate": rate,
        "error": None,
    }


def test_all_pass_is_stable():
    out = scoring.reduce_runs([_run(True, 1.0), _run(True, 1.0), _run(True, 1.0)])
    assert out["passed"] is True
    assert out["majority"]["pass_count"] == 3
    assert out["majority"]["stable"] is True


def test_all_fail_is_stable():
    out = scoring.reduce_runs([_run(False, 0.5), _run(False, 0.4)])
    assert out["passed"] is False
    assert out["majority"]["stable"] is True


def test_majority_pass_wins():
    # 2/3 통과 → 대표 PASS
    out = scoring.reduce_runs([_run(True, 1.0), _run(True, 1.0), _run(False, 0.6)])
    assert out["passed"] is True
    assert out["majority"]["pass_count"] == 2
    assert out["majority"]["stable"] is False  # 흔들림


def test_minority_pass_loses():
    # 1/3 통과 → 대표 FAIL
    out = scoring.reduce_runs([_run(True, 1.0), _run(False, 0.6), _run(False, 0.5)])
    assert out["passed"] is False
    assert out["majority"]["pass_count"] == 1


def test_tie_goes_to_pass():
    # 1/2 (동률) → 과반 기준 통과 측(pass_count*2 >= n)
    out = scoring.reduce_runs([_run(True, 1.0), _run(False, 0.5)])
    assert out["passed"] is True


def test_check_rate_avg_and_range():
    out = scoring.reduce_runs([_run(True, 1.0), _run(False, 0.5), _run(False, 0.0)])
    assert out["pass_rate"] == round((1.0 + 0.5 + 0.0) / 3, 3)
    assert out["majority"]["check_rate_min"] == 0.0
    assert out["majority"]["check_rate_max"] == 1.0


def test_representative_run_keeps_details():
    # 대표 run 은 통과 run 우선 — details/checks 보존
    passing = _run(True, 1.0)
    passing["details"] = {"keep": "me"}
    out = scoring.reduce_runs([_run(False, 0.5), passing])
    assert out["details"].get("keep") == "me"


def test_empty_runs():
    out = scoring.reduce_runs([])
    assert out["passed"] is False


def test_aggregate_consumes_reduced_results():
    """reduce_runs 출력이 aggregate 와 호환되는지(파이프라인 정합)."""
    reduced = [
        scoring.reduce_runs([_run(True, 1.0, "01", "A"), _run(True, 1.0, "01", "A")]),
        scoring.reduce_runs([_run(False, 0.5, "09", "B"), _run(True, 1.0, "09", "B")]),
    ]
    summary = scoring.aggregate(reduced)
    assert summary["total"] == 2
    assert summary["passed"] == 2  # 둘 다 과반 통과(09 는 동률→통과)
    assert "A" in summary["by_tier"] and "B" in summary["by_tier"]
