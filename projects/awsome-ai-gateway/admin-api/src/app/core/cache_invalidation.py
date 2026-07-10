# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import redis.asyncio as aioredis
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import CacheInvalidationFailure

logger = structlog.get_logger()


class CacheInvalidationManager:
    """Best-effort Redis DEL with failure logging and manual retry support."""

    def __init__(self, redis: aioredis.Redis) -> None:
        self._redis = redis

    async def invalidate(
        self,
        keys: list[str],
        *,
        session: AsyncSession | None = None,
    ) -> None:
        """Delete Redis cache keys. On failure, log to cache_invalidation_failures table."""
        if not keys:
            return

        for key in keys:
            try:
                await self._redis.delete(key)
                logger.debug("cache.invalidated", key=key)
            except Exception:
                logger.warning("cache.invalidation_failed", key=key, exc_info=True)
                if session:
                    await self._record_failure(session, key)

    async def invalidate_pattern(
        self,
        pattern: str,
        *,
        session: AsyncSession | None = None,
    ) -> int:
        """Delete all Redis keys matching ``pattern`` via SCAN+DEL. Returns deleted count.

        Use for fan-out invalidation when a single source-of-truth update should
        invalidate many cached keys (e.g. one rate-limit policy change → all
        per-model cache entries for that user/team). On failure, logs the
        pattern as a single failure entry; partial deletes are not rolled back.
        """
        try:
            deleted = 0
            async for key in self._redis.scan_iter(match=pattern, count=200):
                await self._redis.delete(key)
                deleted += 1
            logger.debug("cache.invalidated_pattern", pattern=pattern, deleted=deleted)
            return deleted
        except Exception:
            logger.warning("cache.invalidation_pattern_failed", pattern=pattern, exc_info=True)
            if session is not None:
                await self._record_failure(session, f"pattern:{pattern}")
            return 0

    async def retry_failed(self, session: AsyncSession) -> int:
        """Retry all unresolved cache invalidation failures. Returns count of resolved."""
        from sqlalchemy import select

        stmt = select(CacheInvalidationFailure).where(
            CacheInvalidationFailure.resolved_at.is_(None)
        )
        result = await session.execute(stmt)
        failures = result.scalars().all()

        resolved_count = 0
        for failure in failures:
            try:
                await self._redis.delete(failure.cache_key)
                failure.resolved_at = datetime.now(timezone.utc)
                resolved_count += 1
                logger.info("cache.retry_resolved", key=failure.cache_key)
            except Exception:
                failure.retry_count += 1
                failure.last_retry_at = datetime.now(timezone.utc)
                logger.warning("cache.retry_failed", key=failure.cache_key, retry_count=failure.retry_count)

        return resolved_count

    async def swap_reverse_index_membership(
        self,
        *,
        old_key: str,
        new_key: str,
        members: list[str],
        session: AsyncSession | None = None,
    ) -> None:
        """SREM old_key + SADD new_key in one pipeline. Failure → audit + raise."""
        if not members:
            return
        try:
            pipe = self._redis.pipeline(transaction=False)
            pipe.srem(old_key, *members)
            pipe.sadd(new_key, *members)
            await pipe.execute()
        except Exception:
            logger.warning(
                "cache.swap_reverse_index_failed",
                old_key=old_key,
                new_key=new_key,
                member_count=len(members),
                exc_info=True,
            )
            if session is not None:
                await self._record_failure(session, f"swap:{old_key}->{new_key}")
            raise

    async def _record_failure(self, session: AsyncSession, cache_key: str) -> None:
        entry = CacheInvalidationFailure(
            id=uuid.uuid4(),
            cache_key=cache_key,
            failed_at=datetime.now(timezone.utc),
            retry_count=0,
            context={"source": "admin-api"},
        )
        session.add(entry)
