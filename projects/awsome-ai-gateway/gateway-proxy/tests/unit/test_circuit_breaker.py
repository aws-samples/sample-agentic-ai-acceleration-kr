# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""CircuitBreaker 상태머신 검증 (deepdive Q50). 시간 주입으로 sleep 없이 결정적."""

from __future__ import annotations

from app.degradation.circuit_breaker import CircuitBreaker, CircuitState


class _Clock:
    """주입형 단조 시계."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _cb(clock, **kw):
    return CircuitBreaker(now_fn=clock, **kw)


def test_starts_closed_and_allows():
    cb = _cb(_Clock())
    assert cb.state == CircuitState.CLOSED
    assert cb.allow() is True


def test_trips_open_after_fail_threshold():
    cb = _cb(_Clock(), fail_threshold=3)
    for _ in range(2):
        cb.record_failure()
    assert cb.state == CircuitState.CLOSED  # 아직
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert cb.allow() is False  # 즉시 차단(fast-fail)


def test_open_rejects_until_recovery_then_half_open():
    clock = _Clock()
    cb = _cb(clock, fail_threshold=1, recovery_timeout=5.0)
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert cb.allow() is False  # recovery 미경과
    clock.advance(5.0)
    assert cb.allow() is True  # 경과 → HALF_OPEN 승격, 시험 호출 허용
    assert cb.state == CircuitState.HALF_OPEN


def test_half_open_success_closes():
    clock = _Clock()
    cb = _cb(clock, fail_threshold=1, recovery_timeout=1.0, success_threshold=2)
    cb.record_failure()
    clock.advance(1.0)
    cb.allow()  # → HALF_OPEN
    cb.record_success()
    assert cb.state == CircuitState.HALF_OPEN  # 아직 1/2
    cb.record_success()
    assert cb.state == CircuitState.CLOSED  # 복구


def test_half_open_failure_reopens():
    clock = _Clock()
    cb = _cb(clock, fail_threshold=1, recovery_timeout=1.0)
    cb.record_failure()
    clock.advance(1.0)
    cb.allow()  # → HALF_OPEN
    cb.record_failure()  # 시험 실패 → 즉시 OPEN
    assert cb.state == CircuitState.OPEN
    assert cb.allow() is False


def test_success_decays_fail_count_in_closed():
    """CLOSED 에서 간헐 실패 사이 성공이 fail_count 를 감쇠 → 영원히 안 쌓임."""
    cb = _cb(_Clock(), fail_threshold=3)
    cb.record_failure()
    cb.record_failure()  # 2
    cb.record_success()  # 1
    cb.record_failure()  # 2
    cb.record_success()  # 1
    cb.record_failure()  # 2 — 임계(3) 미달이라 여전히 CLOSED
    assert cb.state == CircuitState.CLOSED


def test_recovery_timeout_zero_immediately_half_open():
    """recovery_timeout=0 이면 OPEN 직후 첫 allow() 가 HALF_OPEN."""
    cb = _cb(_Clock(), fail_threshold=1, recovery_timeout=0.0)
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert cb.allow() is True
    assert cb.state == CircuitState.HALF_OPEN
