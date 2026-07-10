# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.audit import AuditLog

logger = structlog.get_logger()


class AuditLogger:
    """Audit logger with in-process queue + long-lived batch consumer.

    Behavior:
      - log(): non-blocking put_nowait into asyncio.Queue. Returns None.
        If queue is full, drop entry and emit a warning (응답 지연보다 우선).
      - Background consumer drains the queue every `flush_interval` seconds
        or when `batch_size` items accumulate, then bulk-INSERTs.
      - shutdown() sets stop_event and waits for the consumer to drain remaining
        items (graceful). Pod kill -9 may lose in-flight queue items.

    NFR-3.8 implication: audit log is now eventually-consistent with < 500ms
    typical lag. See spec.
    """

    def __init__(
        self,
        *,
        batch_size: int = 100,
        flush_interval: float = 0.5,
        max_queue_size: int = 10_000,
    ) -> None:
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=max_queue_size)
        self._consumer_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._session_factory: async_sessionmaker[AsyncSession] | None = None
        self._dropped = 0

    async def log(
        self,
        session: AsyncSession | None,  # 시그니처 호환 (호출부 변경 0). 미사용.
        *,
        actor_user_id: uuid.UUID,
        actor_role: str,
        action: str,
        resource_type: str,
        resource_id: str,
        changes: dict | None = None,
        result: str = "SUCCESS",
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> None:
        entry = {
            "id": uuid.uuid4(),
            "timestamp": datetime.now(timezone.utc),
            "actor_user_id": actor_user_id,
            "actor_role": actor_role,
            "action": action,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "changes": changes or {},
            "result": result,
            "ip_address": ip_address,
            "request_id": request_id,
        }
        # NOTE: log() does not check stop_event. If called during/after shutdown,
        # the entry can be enqueued but never drained (consumer already exited).
        # Trade-off accepted: we don't want callers to block or fail at request time.
        try:
            self._queue.put_nowait(entry)
        except asyncio.QueueFull:
            self._dropped += 1
            logger.warning(
                "audit.queue_full_drop",
                action=action,
                resource_type=resource_type,
                dropped_total=self._dropped,
            )

    async def start(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        if self._consumer_task is not None:
            return
        self._session_factory = session_factory
        self._stop_event.clear()
        self._consumer_task = asyncio.create_task(self._consume(), name="audit_consumer")

    async def shutdown(self, timeout: float = 10.0) -> None:
        if self._consumer_task is None:
            return
        # NOTE: consumer wakes up at most after `flush_interval` seconds (default 0.5s)
        # because _drain_batch awaits queue.get() with that timeout. Acceptable at
        # current defaults; raise concern if flush_interval is ever raised significantly.
        self._stop_event.set()
        try:
            await asyncio.wait_for(self._consumer_task, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("audit.shutdown_timeout", remaining=self._queue.qsize())
            self._consumer_task.cancel()
        self._consumer_task = None

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    @property
    def dropped_count(self) -> int:
        return self._dropped

    async def _consume(self) -> None:
        assert self._session_factory is not None
        while True:
            batch = await self._drain_batch()
            if batch:
                await self._flush_batch(batch)
            if self._stop_event.is_set() and self._queue.empty():
                return

    async def _drain_batch(self) -> list[dict[str, Any]]:
        """Wait for first item (or flush_interval timeout), then drain up to batch_size."""
        batch: list[dict[str, Any]] = []
        try:
            first = await asyncio.wait_for(self._queue.get(), timeout=self._flush_interval)
            batch.append(first)
        except asyncio.TimeoutError:
            return batch
        # 큐에 더 있으면 batch_size 까지 즉시 drain (block 안 함)
        while len(batch) < self._batch_size:
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return batch

    async def _flush_batch(self, batch: list[dict[str, Any]]) -> None:
        assert self._session_factory is not None
        try:
            async with self._session_factory() as session:
                session.add_all([AuditLog(**entry) for entry in batch])
                await session.commit()
        except Exception as exc:
            logger.error("audit.flush_failed", count=len(batch), error=str(exc), exc_info=True)


# Module-level singleton (호환). main.py lifespan 에서 start()/shutdown() 호출.
audit_logger = AuditLogger()
