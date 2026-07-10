# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from worker.services.config_cache import ConfigCache
from worker.services.notification_buffer import NotificationBufferQueue

if TYPE_CHECKING:
    from redis.asyncio import Redis
    from redis.asyncio.cluster import RedisCluster

    from worker.observability.metrics import WorkerMetrics
    from worker.schemas.events import NotificationEvent

logger = structlog.get_logger(__name__)


class HealthCheckTask:
    """60초마다 DB와 Redis 상태를 확인하고, DB 복구 시 메모리 버퍼를 드레인한다.

    TaskSupervisor가 ``run()``을 태스크로 실행한다.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        redis_client: Redis | RedisCluster,
        config_cache: ConfigCache,
        notification_buffer: NotificationBufferQueue,
        process_buffered_event: Callable[[NotificationEvent], Awaitable[None]],
        check_interval: int = 60,
        metrics: WorkerMetrics | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._redis_client = redis_client
        self._config_cache = config_cache
        self._notification_buffer = notification_buffer
        self._process_buffered_event = process_buffered_event
        self._check_interval = check_interval
        self._metrics = metrics

        self._db_healthy: bool = True
        self._redis_healthy: bool = True

    async def run(self) -> None:
        logger.info("health_check_task_started")
        while True:
            await self._check_db()
            await self._check_redis()
            await self._poll_config_if_needed()
            await asyncio.sleep(self._check_interval)

    async def _check_db(self) -> None:
        """DB 연결 상태를 확인하고, 복구 시 버퍼를 드레인한다."""
        try:
            async with self._session_factory() as session:
                await asyncio.wait_for(session.execute(text("SELECT 1")), timeout=3.0)

            if not self._db_healthy:
                self._db_healthy = True
                logger.info("db_connection_restored")
                drained = await self._notification_buffer.drain(self._process_buffered_event)
                if drained > 0:
                    logger.info("buffer_drained", count=drained)

        except Exception as exc:
            if self._db_healthy:
                self._db_healthy = False
                logger.error("db_connection_lost", error=str(exc))
                if self._metrics:
                    self._metrics.errors_total.add(1, {"error_type": "db"})

    async def _check_redis(self) -> None:
        """Redis 연결 상태를 확인한다."""
        try:
            await asyncio.wait_for(self._redis_client.ping(), timeout=1.0)

            if not self._redis_healthy:
                self._redis_healthy = True
                logger.info("redis_connection_restored")

        except Exception as exc:
            if self._redis_healthy:
                self._redis_healthy = False
                logger.error("redis_connection_lost", error=str(exc))
                if self._metrics:
                    self._metrics.errors_total.add(1, {"error_type": "redis"})

    async def _poll_config_if_needed(self) -> None:
        """ConfigCache의 폴링 주기가 경과했으면 DB에서 재로드한다."""
        if self._config_cache.needs_poll():
            try:
                await self._config_cache.load()
            except Exception as exc:
                logger.warning("config_poll_failed", error=str(exc))

    @property
    def is_healthy(self) -> bool:
        """DB와 Redis 모두 정상 상태인 경우 True를 반환한다."""
        return self._db_healthy and self._redis_healthy
