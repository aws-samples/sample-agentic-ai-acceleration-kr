# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
import pytest
from unittest.mock import AsyncMock, MagicMock
from app.services.circuit_breaker import CircuitBreakerService


@pytest.mark.asyncio
async def test_opens_after_min_calls_failures(fake_redis):
    cb = CircuitBreakerService(window_sec=30, min_calls=5, error_rate=0.5, open_sec=30, halfopen_ttl_ms=8000)
    pmid = "global.anthropic.claude-opus-4-8"
    for _ in range(4):
        await cb.record_failure(fake_redis, pmid)
    assert await cb.is_open(fake_redis, pmid) is False
    await cb.record_failure(fake_redis, pmid)
    assert await cb.is_open(fake_redis, pmid) is True


@pytest.mark.asyncio
async def test_fail_open_when_redis_none():
    cb = CircuitBreakerService()
    assert await cb.is_open(None, "x") is False        # Redis down => closed
    assert await cb.record_failure(None, "x") is False  # no-op, no raise
    assert await cb.record_success(None, "x") is False  # no-op, no raise
    assert await cb.try_acquire_halfopen_probe(None, "x") is True  # let request through


@pytest.mark.asyncio
async def test_fail_open_on_redis_exception():
    cb = CircuitBreakerService()
    redis = MagicMock()
    redis.exists = AsyncMock(side_effect=ConnectionError("boom"))
    redis.eval = AsyncMock(side_effect=ConnectionError("boom"))
    redis.time = AsyncMock(side_effect=ConnectionError("boom"))
    redis.set = AsyncMock(side_effect=ConnectionError("boom"))
    redis.delete = AsyncMock(side_effect=ConnectionError("boom"))
    assert await cb.is_open(redis, "x") is False
    assert await cb.record_failure(redis, "x") is False
    assert await cb.record_success(redis, "x") is False  # must not raise even though delete raises
    assert await cb.try_acquire_halfopen_probe(redis, "x") is True


@pytest.mark.asyncio
async def test_halfopen_single_flight(fake_redis):
    cb = CircuitBreakerService(halfopen_ttl_ms=8000)
    pmid = "m"
    first = await cb.try_acquire_halfopen_probe(fake_redis, pmid)
    second = await cb.try_acquire_halfopen_probe(fake_redis, pmid)
    assert first is True and second is False


@pytest.mark.asyncio
async def test_record_success_clears_open(fake_redis):
    cb = CircuitBreakerService()
    pmid = "m"
    await fake_redis.set(f"cb:{pmid}:open", "1")
    result = await cb.record_success(fake_redis, pmid)
    assert result is True  # Lua short-circuits returning 1 when open flag present
    assert await fake_redis.exists(f"cb:{pmid}:open") == 0
