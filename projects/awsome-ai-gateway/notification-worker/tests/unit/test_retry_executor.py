# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""RetryExecutor 단위 테스트."""
from __future__ import annotations

import pytest

from worker.senders.base import EmailSendError
from worker.services.retry_executor import RetryExecutor


async def test_execute_success_first_attempt() -> None:
    executor = RetryExecutor()
    call_count = 0

    async def fn():
        nonlocal call_count
        call_count += 1
        return "ok"

    result = await executor.execute(fn, event_type="budget_threshold")
    assert result == "ok"
    assert call_count == 1


async def test_execute_retries_on_transient_error(monkeypatch) -> None:
    """일시적 오류 시 최대 3회 재시도."""
    import worker.services.retry_executor as module

    monkeypatch.setattr(module, "_BACKOFF_DELAYS", (0, 0, 0))  # 지연 제거

    executor = RetryExecutor()
    call_count = 0

    async def fn():
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise EmailSendError("transient", retryable=True)
        return "ok"

    result = await executor.execute(fn)
    assert result == "ok"
    assert call_count == 3


async def test_execute_raises_on_permanent_error() -> None:
    """retryable=False 오류는 즉시 re-raise."""
    executor = RetryExecutor()
    call_count = 0

    async def fn():
        nonlocal call_count
        call_count += 1
        raise EmailSendError("invalid_address", retryable=False)

    with pytest.raises(EmailSendError, match="invalid_address"):
        await executor.execute(fn)

    assert call_count == 1  # 재시도 없음


async def test_execute_raises_after_all_attempts_exhausted(monkeypatch) -> None:
    import worker.services.retry_executor as module

    monkeypatch.setattr(module, "_BACKOFF_DELAYS", (0, 0, 0))

    executor = RetryExecutor()
    call_count = 0

    async def fn():
        nonlocal call_count
        call_count += 1
        raise EmailSendError("timeout", retryable=True)

    with pytest.raises(EmailSendError):
        await executor.execute(fn, event_type="key_expiring")

    assert call_count == 3  # _MAX_ATTEMPTS


async def test_execute_increments_retry_counter(monkeypatch) -> None:
    import worker.services.retry_executor as module

    monkeypatch.setattr(module, "_BACKOFF_DELAYS", (0, 0, 0))

    class FakeCounter:
        calls: list = []

        def add(self, value, attrs=None):
            self.calls.append((value, attrs))

    counter = FakeCounter()
    executor = RetryExecutor(retry_counter=counter)
    call_count = 0

    async def fn():
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise EmailSendError("retry_me", retryable=True)
        return "ok"

    await executor.execute(fn, event_type="key_revoked")

    # 첫 번째 실패 시 retry_counter.add 호출
    assert len(counter.calls) == 1
    assert counter.calls[0][1] == {"event_type": "key_revoked"}


async def test_execute_retries_generic_exception(monkeypatch) -> None:
    """EmailSendError 외 일반 예외도 retryable로 처리."""
    import worker.services.retry_executor as module

    monkeypatch.setattr(module, "_BACKOFF_DELAYS", (0, 0, 0))

    executor = RetryExecutor()
    call_count = 0

    async def fn():
        nonlocal call_count
        call_count += 1
        raise ConnectionError("network error")

    with pytest.raises(ConnectionError):
        await executor.execute(fn)

    assert call_count == 3
