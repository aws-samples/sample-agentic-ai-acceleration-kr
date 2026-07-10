# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from worker.models.notification import NotificationConfig

if TYPE_CHECKING:
    pass

logger = structlog.get_logger(__name__)

_POLL_INTERVAL = 300  # 5분 (PP-02)


class ConfigCache:
    """NotificationConfig 인메모리 캐시 (PP-02).

    이중 갱신 전략:
    1. Pub/Sub 즉시 갱신 (notifications:config_reload 수신 시 reload() 호출)
    2. 5분 주기 DB 폴링 (HealthCheckTask의 60초 루프에서 needs_poll() 확인)

    둘 다 실패 시 메모리의 기존 데이터로 계속 동작 (graceful degradation).
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._configs: dict[str, NotificationConfig] = {}
        self._last_loaded: float = 0.0

    async def load(self) -> None:
        """DB에서 전체 NotificationConfig를 로드하여 캐시를 갱신한다."""
        async with self._session_factory() as session:
            result = await session.execute(select(NotificationConfig))
            configs = result.scalars().all()

        self._configs = {c.event_type: c for c in configs}
        self._last_loaded = time.monotonic()
        logger.info("config_cache_loaded", count=len(self._configs))

    async def reload(self) -> None:
        """Pub/Sub config_reload 이벤트 수신 시 호출된다."""
        await self.load()
        logger.info("config_cache_reloaded")

    def get(self, event_type: str) -> NotificationConfig | None:
        """이벤트 유형에 해당하는 NotificationConfig를 반환한다."""
        return self._configs.get(event_type)

    def needs_poll(self) -> bool:
        """마지막 로드로부터 POLL_INTERVAL(5분)이 경과했는지 확인한다."""
        return (time.monotonic() - self._last_loaded) > _POLL_INTERVAL


# Module singleton
_cache: ConfigCache | None = None


async def init_config_cache(session_factory: async_sessionmaker[AsyncSession]) -> None:
    global _cache
    _cache = ConfigCache(session_factory)
    await _cache.load()


def get_config_cache() -> ConfigCache:
    assert _cache is not None, "ConfigCache not initialised. Call init_config_cache() first."
    return _cache
