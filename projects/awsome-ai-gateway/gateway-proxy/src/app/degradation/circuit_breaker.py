# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Per-call Circuit Breaker (deepdive Q50 Phase 3/4).

목적: Redis 가 느리거나 죽었을 때, 매 요청이 socket_timeout(예 2s)을 다 기다린 뒤
실패하는 대신, **연속 실패가 임계를 넘으면 회로를 열어(OPEN) 즉시 fast-fail** 시킨다.
이렇게 하면 ~75초 걸리는 전역 degradation 상태머신을 기다리지 않고 호출 단위에서
1초 미만으로 fallback 경로로 빠질 수 있다(브레인스토밍 "per-request circuit breaker").

상태:
  CLOSED   : 정상. 호출 허용. 실패 누적이 fail_threshold 도달 → OPEN.
  OPEN     : 차단. recovery_timeout 동안 호출 즉시 거부(CircuitOpenError).
             타임아웃 경과 후 첫 호출은 HALF_OPEN 으로 승격해 시험 허용.
  HALF_OPEN: 시험 통과(성공 success_threshold)→ CLOSED 복구, 실패 1회 → 다시 OPEN.

시간 의존(monotonic)은 주입 가능(now_fn)해 테스트에서 sleep 없이 결정적으로 검증.
fail-soft: 이 모듈은 enforcement 를 대체하지 않는다 — 회로가 열리면 호출자는 기존
fallback(DB / in-memory)으로 우회한다(전역 degradation 과 협력).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from enum import Enum


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    """회로가 OPEN 이라 호출이 즉시 거부됨(빠른 실패)."""


class CircuitBreaker:
    """연속 실패 기반 회로 차단기. 동기 상태 전이 + 시간 주입 가능."""

    def __init__(
        self,
        *,
        fail_threshold: int = 5,
        recovery_timeout: float = 5.0,
        success_threshold: int = 2,
        now_fn: Callable[[], float] | None = None,
        name: str = "redis",
    ) -> None:
        self._fail_threshold = max(1, fail_threshold)
        self._recovery_timeout = max(0.0, recovery_timeout)
        self._success_threshold = max(1, success_threshold)
        self._now = now_fn or time.monotonic
        self._name = name

        self._state = CircuitState.CLOSED
        self._fail_count = 0
        self._success_count = 0
        self._opened_at = 0.0

    @property
    def state(self) -> CircuitState:
        return self._state

    def allow(self) -> bool:
        """이번 호출을 허용할지. OPEN 이고 recovery 미경과면 False(즉시 거부).

        OPEN 인데 recovery_timeout 경과 시 HALF_OPEN 으로 올려 시험 호출 1건 허용.
        """
        if self._state == CircuitState.OPEN:
            if self._now() - self._opened_at >= self._recovery_timeout:
                self._state = CircuitState.HALF_OPEN
                self._success_count = 0
                return True
            return False
        return True

    def record_success(self) -> None:
        if self._state == CircuitState.HALF_OPEN:
            self._success_count += 1
            if self._success_count >= self._success_threshold:
                self._reset_closed()
        else:
            # CLOSED 정상 성공 — 누적 실패 카운터 점진 감소(간헐 실패가 영원히
            # 안 쌓이게; degradation manager 의 히스테리시스와 동일 철학).
            if self._fail_count > 0:
                self._fail_count -= 1

    def record_failure(self) -> None:
        if self._state == CircuitState.HALF_OPEN:
            # 시험 실패 → 즉시 다시 OPEN.
            self._trip()
            return
        self._fail_count += 1
        if self._fail_count >= self._fail_threshold:
            self._trip()

    def _trip(self) -> None:
        self._state = CircuitState.OPEN
        self._opened_at = self._now()
        self._success_count = 0

    def _reset_closed(self) -> None:
        self._state = CircuitState.CLOSED
        self._fail_count = 0
        self._success_count = 0
