# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import asyncio

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.degradation.manager import DegradationManager

logger = structlog.get_logger(__name__)


class HealthChecker:
    """주기적으로 DB/Redis 상태를 확인하고 DegradationManager에 보고한다.

    deepdive Q50 Phase 3: DB·Redis 체크를 **동시(asyncio.gather)** 실행해 한 틱의
    벽시계 시간을 둘 중 느린 쪽으로 줄인다(과거엔 순차 → 최대 6s 직렬). interval 도
    설정 가능(기본 15s 보존). 감지 지연 = interval × FAIL_THRESHOLD 이므로 interval↓
    이 진짜 장애 진입을 앞당긴다(히스테리시스가 플래핑 false-positive 를 막아준다).
    """

    def __init__(
        self,
        redis,
        db_engine: AsyncEngine,
        degradation_manager: DegradationManager,
        interval: int = 15,
        cost_stream_spool=None,
    ) -> None:
        self._redis = redis
        self._db_engine = db_engine
        self._degradation = degradation_manager
        self._interval = interval
        self._task: asyncio.Task | None = None
        # P0-②: drained on each healthy Redis check to re-publish cost:stream
        # payloads that failed XADD while Redis was down.
        self._cost_stream_spool = cost_stream_spool

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run_loop(self) -> None:
        while True:
            # DB·Redis 동시 체크(상호 독립) → 한 틱 = max(두 체크), not 합.
            # return_exceptions=True 로 한쪽 예외가 다른쪽 보고를 막지 않게 한다
            # (각 _check_* 가 내부에서 예외를 잡아 보고하므로 추가 안전망).
            await asyncio.gather(
                self._check_db(),
                self._check_redis(),
                return_exceptions=True,
            )
            await asyncio.sleep(self._interval)

    async def _check_db(self) -> None:
        try:
            async with self._db_engine.connect() as conn:
                await asyncio.wait_for(
                    conn.execute(text("SELECT 1")),
                    timeout=5.0,
                )
            self._degradation.report_db_health(healthy=True)
        except Exception:
            logger.warning("db_health_check_failed")
            self._degradation.report_db_health(healthy=False)

    async def _check_redis(self) -> None:
        try:
            await asyncio.wait_for(
                self._redis.ping(),
                timeout=1.0,
            )
            self._degradation.report_redis_health(healthy=True)
            # P0-②: Redis is up — re-publish any cost:stream payloads that were
            # spooled while it was down (best-effort; failures re-buffer).
            if self._cost_stream_spool is not None and self._cost_stream_spool.size:
                try:
                    await self._cost_stream_spool.drain(self._redis)
                except Exception:
                    logger.warning("cost_stream_spool_drain_failed")
        except Exception:
            logger.warning("redis_health_check_failed")
            self._degradation.report_redis_health(healthy=False)
