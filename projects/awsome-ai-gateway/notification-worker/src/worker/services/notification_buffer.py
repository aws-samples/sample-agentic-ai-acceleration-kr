# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from worker.schemas.events import NotificationEvent

logger = structlog.get_logger(__name__)


class NotificationBufferQueue:
    """DB 장애 시 NotificationEvent를 메모리에 버퍼링하는 큐 (RP-02).

    큐가 가득 찰 경우 oldest 항목을 드랍하고 새 항목을 삽입한다.
    DB 복구 시 HealthCheckTask가 drain()을 호출하여 전체 처리 파이프라인을 재실행한다.

    U1 UsageBufferQueue와의 차이:
    - max_size: 1000 (U1의 10_000 대비 알림 볼륨에 맞게 축소)
    - drain(): session_factory 대신 process_fn 콜백 수신
      (단순 DB INSERT가 아닌 수신자 결정 → 템플릿 렌더링 → 이메일 전송 → DB 로깅 전체 재실행)
    """

    def __init__(self, max_size: int = 1_000) -> None:
        self._queue: asyncio.Queue[NotificationEvent] = asyncio.Queue(maxsize=max_size)
        self._drop_counter = None  # WorkerMetrics.errors_total

    def set_metrics(self, drop_counter) -> None:
        self._drop_counter = drop_counter

    @property
    def size(self) -> int:
        return self._queue.qsize()

    async def enqueue(self, event: NotificationEvent) -> bool:
        """이벤트를 버퍼에 추가한다. 가득 찬 경우 oldest 항목을 드랍하고 삽입한다."""
        try:
            self._queue.put_nowait(event)
            return True
        except asyncio.QueueFull:
            # oldest 드랍
            try:
                dropped = self._queue.get_nowait()
                logger.warning(
                    "buffer_event_dropped",
                    dropped_event_id=dropped.event_id,
                    queue_size=self._queue.qsize(),
                )
            except asyncio.QueueEmpty:
                pass

            self._queue.put_nowait(event)

            if self._drop_counter is not None:
                self._drop_counter.add(1, {"error_type": "buffer_overflow"})
            return False

    async def drain(
        self,
        process_fn: Callable[[NotificationEvent], Awaitable[None]],
        batch_size: int = 50,
    ) -> int:
        """DB 복구 시 큐 전체를 batch_size 단위로 처리한다.

        process_fn: 이벤트 1건의 전체 처리 파이프라인 (수신자 결정 → 이메일 전송 → DB 로깅)
        처리 실패 시 해당 이벤트를 다시 큐에 넣고 나머지는 다음 drain 주기로 미룬다.
        """
        total = 0
        while not self._queue.empty():
            batch: list[NotificationEvent] = []
            for _ in range(batch_size):
                try:
                    batch.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break

            if not batch:
                break

            for event in batch:
                try:
                    await process_fn(event)
                    total += 1
                except Exception:
                    logger.exception("drain_event_failed", event_id=event.event_id)
                    await self.enqueue(event)
                    # 복구 불안정 상태일 수 있으므로 이번 drain은 여기서 중단
                    return total

        if total > 0:
            logger.info("buffer_drained", count=total)
        return total


# Module singleton
_buffer: NotificationBufferQueue | None = None


def init_notification_buffer(max_size: int = 1_000) -> None:
    global _buffer
    _buffer = NotificationBufferQueue(max_size=max_size)


def get_notification_buffer() -> NotificationBufferQueue:
    assert _buffer is not None, "NotificationBufferQueue not initialised. Call init_notification_buffer() first."
    return _buffer
