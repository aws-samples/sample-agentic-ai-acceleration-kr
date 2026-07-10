# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import structlog

from app.degradation.states import (
    BothDegradedState,
    DBDegradedState,
    DegradationState,
    HealthyState,
    RedisDegradedState,
)
from app.schemas.domain import DegradationLevel

logger = structlog.get_logger(__name__)

# T2 v3 에서 3회 연속 실패 기준은 pool 포화 시 false-positive 유발 확인.
# pool 대기 타임아웃은 일시적이므로 5회 연속 실패로 완화 (진짜 장애에는 15~25s 내 진입).
FAIL_THRESHOLD = 5
SUCCESS_THRESHOLD = 3

# 히스테리시스(deepdive Q50 Phase 3): 미강등 상태에서 성공이 들어오면 fail_count 를
# 0 으로 **리셋하지 않고 1 만 감쇠**한다. 과거엔 단발 성공이 카운터를 0 으로 밀어
# 간헐 장애(bad,good,bad,good — 50%+ 실패율 플래핑)가 절대 임계에 못 닿아 미탐지됐다.
# decay<증가(1<1 의 비대칭) 라 "대부분 실패 + 가끔 성공"이 누적돼 결국 강등된다.
# 순수 연속-실패/연속-성공 시퀀스에선 감쇠가 트리거 안 돼 기존 동작 보존.
FAIL_DECAY_ON_SUCCESS = 1


class DegradationManager:
    """Degradation 상태 머신. Health Checker가 보고하는 성공/실패를 집계하여 상태를 전이한다."""

    def __init__(self) -> None:
        self._state: DegradationState = HealthyState()
        self._db_fail_count: int = 0
        self._redis_fail_count: int = 0
        self._db_success_count: int = 0
        self._redis_success_count: int = 0
        self._metrics_degradation_level = None  # GatewayMetrics.degradation_level, 외부에서 주입

    @property
    def current_state(self) -> DegradationState:
        return self._state

    @property
    def level(self) -> DegradationLevel:
        return self._state.level

    def set_metrics(self, degradation_level_counter) -> None:
        self._metrics_degradation_level = degradation_level_counter

    def report_db_health(self, healthy: bool) -> None:
        db_degraded = self.level in (DegradationLevel.DB_DEGRADED, DegradationLevel.BOTH_DEGRADED)
        if healthy:
            self._db_success_count += 1
            if db_degraded:
                # 복구 진행 중 — 연속 성공이 임계 도달 시 healthy 로. fail 은 리셋.
                self._db_fail_count = 0
                if self._db_success_count >= SUCCESS_THRESHOLD:
                    self._recover_db()
            else:
                # 미강등 — fail_count 를 0 으로 밀지 말고 1 만 감쇠(히스테리시스).
                self._db_fail_count = max(0, self._db_fail_count - FAIL_DECAY_ON_SUCCESS)
        else:
            self._db_success_count = 0
            self._db_fail_count += 1
            if self._db_fail_count >= FAIL_THRESHOLD:
                self._degrade_db()

    def report_redis_health(self, healthy: bool) -> None:
        redis_degraded = self.level in (
            DegradationLevel.REDIS_DEGRADED,
            DegradationLevel.BOTH_DEGRADED,
        )
        if healthy:
            self._redis_success_count += 1
            if redis_degraded:
                self._redis_fail_count = 0
                if self._redis_success_count >= SUCCESS_THRESHOLD:
                    self._recover_redis()
            else:
                self._redis_fail_count = max(0, self._redis_fail_count - FAIL_DECAY_ON_SUCCESS)
        else:
            self._redis_success_count = 0
            self._redis_fail_count += 1
            if self._redis_fail_count >= FAIL_THRESHOLD:
                self._degrade_redis()

    def _degrade_db(self) -> None:
        current = self._state.level
        if current == DegradationLevel.HEALTHY:
            self._transition_to(DBDegradedState())
        elif current == DegradationLevel.REDIS_DEGRADED:
            self._transition_to(BothDegradedState())

    def _degrade_redis(self) -> None:
        current = self._state.level
        if current == DegradationLevel.HEALTHY:
            self._transition_to(RedisDegradedState())
        elif current == DegradationLevel.DB_DEGRADED:
            self._transition_to(BothDegradedState())

    def _recover_db(self) -> None:
        current = self._state.level
        if current == DegradationLevel.DB_DEGRADED:
            self._transition_to(HealthyState())
        elif current == DegradationLevel.BOTH_DEGRADED:
            self._transition_to(RedisDegradedState())

    def _recover_redis(self) -> None:
        current = self._state.level
        if current == DegradationLevel.REDIS_DEGRADED:
            self._transition_to(HealthyState())
        elif current == DegradationLevel.BOTH_DEGRADED:
            self._transition_to(DBDegradedState())

    def _transition_to(self, new_state: DegradationState) -> None:
        old_level = self._state.level
        new_level = new_state.level

        if old_level == new_level:
            return

        self._state = new_state
        logger.warning(
            "degradation_state_transition",
            from_level=old_level.value,
            to_level=new_level.value,
        )

        # OTel gauge 업데이트
        if self._metrics_degradation_level is not None:
            level_map = {
                DegradationLevel.HEALTHY: 0,
                DegradationLevel.DB_DEGRADED: 1,
                DegradationLevel.REDIS_DEGRADED: 2,
                DegradationLevel.BOTH_DEGRADED: 3,
            }
            old_val = level_map.get(old_level, 0)
            new_val = level_map.get(new_level, 0)
            self._metrics_degradation_level.add(new_val - old_val)
