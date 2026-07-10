# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from app.degradation.manager import (
    FAIL_THRESHOLD,
    SUCCESS_THRESHOLD,
    DegradationManager,
)
from app.schemas.domain import DegradationLevel


def test_initial_state_is_healthy():
    dm = DegradationManager()
    assert dm.level == DegradationLevel.HEALTHY


def test_db_degradation_after_fail_threshold():
    dm = DegradationManager()
    # FAIL_THRESHOLD - 1 회 실패 → 아직 HEALTHY
    for _ in range(FAIL_THRESHOLD - 1):
        dm.report_db_health(False)
    assert dm.level == DegradationLevel.HEALTHY
    dm.report_db_health(False)
    assert dm.level == DegradationLevel.DB_DEGRADED


def test_redis_degradation_after_fail_threshold():
    dm = DegradationManager()
    for _ in range(FAIL_THRESHOLD):
        dm.report_redis_health(False)
    assert dm.level == DegradationLevel.REDIS_DEGRADED


def test_both_degraded():
    dm = DegradationManager()
    for _ in range(FAIL_THRESHOLD):
        dm.report_db_health(False)
    assert dm.level == DegradationLevel.DB_DEGRADED
    for _ in range(FAIL_THRESHOLD):
        dm.report_redis_health(False)
    assert dm.level == DegradationLevel.BOTH_DEGRADED


def test_recovery_from_db_degraded():
    dm = DegradationManager()
    for _ in range(FAIL_THRESHOLD):
        dm.report_db_health(False)
    assert dm.level == DegradationLevel.DB_DEGRADED

    # SUCCESS_THRESHOLD - 1 회 성공 → 아직 DB_DEGRADED
    for _ in range(SUCCESS_THRESHOLD - 1):
        dm.report_db_health(True)
    assert dm.level == DegradationLevel.DB_DEGRADED
    dm.report_db_health(True)
    assert dm.level == DegradationLevel.HEALTHY


def test_recovery_both_to_redis_degraded():
    dm = DegradationManager()
    for _ in range(FAIL_THRESHOLD):
        dm.report_db_health(False)
    for _ in range(FAIL_THRESHOLD):
        dm.report_redis_health(False)
    assert dm.level == DegradationLevel.BOTH_DEGRADED

    # DB 복구
    for _ in range(SUCCESS_THRESHOLD):
        dm.report_db_health(True)
    assert dm.level == DegradationLevel.REDIS_DEGRADED


def test_can_serve():
    dm = DegradationManager()
    assert dm.current_state.can_serve() is True

    for _ in range(FAIL_THRESHOLD):
        dm.report_db_health(False)
    assert dm.current_state.can_serve() is True  # DB_DEGRADED는 서비스 가능

    for _ in range(FAIL_THRESHOLD):
        dm.report_redis_health(False)
    assert dm.current_state.can_serve() is False  # BOTH_DEGRADED


# ─── 히스테리시스 (deepdive Q50 Phase 3) ───


def test_flapping_eventually_degrades():
    """간헐 장애(2실패:1성공 — 67% 실패율)도 결국 강등된다.

    과거엔 단발 성공이 fail_count 를 0 으로 리셋해 절대 임계에 못 닿았다(미탐지 버그).
    이제 성공은 1 만 감쇠하므로 실패>성공이면 카운터가 누적돼 강등에 도달한다.
    """
    dm = DegradationManager()
    # 패턴: F F S 반복. 사이클당 순증가 = 2 - 1 = 1.
    for _ in range(20):
        dm.report_redis_health(False)
        dm.report_redis_health(False)
        dm.report_redis_health(True)
        if dm.level == DegradationLevel.REDIS_DEGRADED:
            break
    assert dm.level == DegradationLevel.REDIS_DEGRADED


def test_sparse_failures_stay_healthy():
    """드문 단발 실패(성공이 다수)는 강등되지 않는다 — false-positive 방지.

    각 실패 직후 충분한 성공이 fail_count 를 0 으로 감쇠시켜 누적되지 않는다.
    """
    dm = DegradationManager()
    for _ in range(30):
        dm.report_redis_health(False)
        # 실패 1회당 성공 3회 → fail_count 가 0 으로 다시 내려간다.
        dm.report_redis_health(True)
        dm.report_redis_health(True)
        dm.report_redis_health(True)
    assert dm.level == DegradationLevel.HEALTHY


def test_consecutive_fails_still_degrade_unchanged():
    """순수 연속 실패는 기존과 동일하게 정확히 FAIL_THRESHOLD 에서 강등(감쇠 무관)."""
    dm = DegradationManager()
    for _ in range(FAIL_THRESHOLD - 1):
        dm.report_redis_health(False)
    assert dm.level == DegradationLevel.HEALTHY
    dm.report_redis_health(False)
    assert dm.level == DegradationLevel.REDIS_DEGRADED


def test_recovery_resets_fail_count_cleanly():
    """강등 후 연속 성공 복구 시 fail_count 잔재가 즉시 재강등시키지 않는다."""
    dm = DegradationManager()
    for _ in range(FAIL_THRESHOLD):
        dm.report_redis_health(False)
    assert dm.level == DegradationLevel.REDIS_DEGRADED
    for _ in range(SUCCESS_THRESHOLD):
        dm.report_redis_health(True)
    assert dm.level == DegradationLevel.HEALTHY
    # 복구 직후 단발 실패가 곧장 재강등하지 않아야 한다(fail_count 가 0 에서 시작).
    dm.report_redis_health(False)
    assert dm.level == DegradationLevel.HEALTHY
