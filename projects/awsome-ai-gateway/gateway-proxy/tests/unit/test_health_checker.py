# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""HealthChecker 동시 체크 검증 (deepdive Q50 Phase 3).

DB·Redis 체크를 asyncio.gather 로 동시 실행하도록 바꿨다(과거 순차 → 최대 6s 직렬).
한 틱이 둘 다 보고하는지, 한쪽 예외가 다른쪽 보고를 막지 않는지 잠근다.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.degradation.manager import DegradationManager
from app.services.health_checker import HealthChecker


def _fake_db_engine(ok: bool):
    """SELECT 1 이 성공/실패하는 가짜 AsyncEngine.connect() 컨텍스트."""
    engine = MagicMock()

    @asynccontextmanager
    async def _connect():
        conn = MagicMock()
        if ok:
            conn.execute = AsyncMock(return_value=None)
        else:
            conn.execute = AsyncMock(side_effect=Exception("db down"))
        yield conn

    engine.connect = _connect
    return engine


@pytest.mark.asyncio
async def test_single_tick_reports_both_db_and_redis():
    dm = DegradationManager()
    dm.report_db_health = MagicMock()
    dm.report_redis_health = MagicMock()

    redis = MagicMock()
    redis.ping = AsyncMock(return_value=True)
    hc = HealthChecker(redis, _fake_db_engine(ok=True), dm)

    # 한 틱만 수동 실행(루프 없이 _check_* 동시 호출).
    import asyncio

    await asyncio.gather(hc._check_db(), hc._check_redis())

    dm.report_db_health.assert_called_once_with(healthy=True)
    dm.report_redis_health.assert_called_once_with(healthy=True)


@pytest.mark.asyncio
async def test_redis_failure_does_not_block_db_report():
    dm = DegradationManager()
    dm.report_db_health = MagicMock()
    dm.report_redis_health = MagicMock()

    redis = MagicMock()
    redis.ping = AsyncMock(side_effect=Exception("redis down"))
    hc = HealthChecker(redis, _fake_db_engine(ok=True), dm)

    import asyncio

    await asyncio.gather(hc._check_db(), hc._check_redis(), return_exceptions=True)

    # DB 는 정상 보고, Redis 는 실패 보고 — 한쪽 예외가 다른쪽을 막지 않는다.
    dm.report_db_health.assert_called_once_with(healthy=True)
    dm.report_redis_health.assert_called_once_with(healthy=False)
