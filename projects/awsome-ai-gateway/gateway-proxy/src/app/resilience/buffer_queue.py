# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = structlog.get_logger(__name__)


class UsageBufferQueue:
    """DB 장애 시 UsageRecord를 메모리에 버퍼링하는 큐.

    큐가 가득 찰 경우 oldest 항목을 드랍하고 새 항목을 삽입한다.
    복구 시 drain()으로 일괄 flush한다.
    """

    def __init__(self, max_size: int = 10_000) -> None:
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=max_size)
        self._dropped_count: int = 0
        self._drop_counter = None  # GatewayMetrics.usage_records_dropped_total

    def set_metrics(self, dropped_counter) -> None:
        self._drop_counter = dropped_counter

    @property
    def size(self) -> int:
        return self._queue.qsize()

    async def enqueue(self, record) -> bool:
        try:
            self._queue.put_nowait(record)
            return True
        except asyncio.QueueFull:
            # oldest 항목 드랍
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self._queue.put_nowait(record)
            self._dropped_count += 1
            if self._drop_counter is not None:
                self._drop_counter.add(1)
            logger.warning("usage_record_dropped", queue_size=self._queue.qsize())
            return False

    async def drain(
        self, session_factory: async_sessionmaker[AsyncSession], batch_size: int = 100
    ) -> int:
        """큐 전체를 batch_size 단위로 DB insert. 복구 즉시 호출."""
        from app.models.usage import UsageRecord

        total = 0
        while not self._queue.empty():
            batch = []
            for _ in range(batch_size):
                try:
                    batch.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break

            if not batch:
                break

            try:
                async with session_factory() as session:
                    session.add_all(batch)
                    await session.commit()
                total += len(batch)
                logger.info("usage_buffer_drained", count=len(batch))
            except Exception:
                logger.exception("usage_buffer_drain_failed", batch_size=len(batch))
                # 실패한 배치 다시 큐에 넣기 (best-effort)
                for record in batch:
                    try:
                        self._queue.put_nowait(record)
                    except asyncio.QueueFull:
                        break

        return total
