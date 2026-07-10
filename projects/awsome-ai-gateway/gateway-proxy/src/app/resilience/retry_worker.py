# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class RetryItem:
    task_fn: Callable[[], Awaitable[None]]
    attempt: int = 0
    max_retries: int = 3
    next_retry_at: float = field(default=0.0)


class RetryWorker:
    """비동기 백그라운드 태스크 실패를 지수 백오프로 재시도하는 워커."""

    def __init__(self, max_retries: int = 3) -> None:
        self._queue: asyncio.Queue[RetryItem] = asyncio.Queue()
        self._max_retries = max_retries
        self._task: asyncio.Task | None = None
        self._error_counter = None  # GatewayMetrics.background_task_errors_total

    def set_metrics(self, error_counter) -> None:
        self._error_counter = error_counter

    async def submit(self, task_fn: Callable[[], Awaitable[None]]) -> None:
        item = RetryItem(task_fn=task_fn, max_retries=self._max_retries)
        await self._queue.put(item)

    async def run(self) -> None:
        """lifespan에서 asyncio.create_task로 실행."""
        while True:
            item = await self._queue.get()

            now = asyncio.get_event_loop().time()
            if item.next_retry_at > now:
                await asyncio.sleep(item.next_retry_at - now)

            try:
                await item.task_fn()
            except Exception:
                item.attempt += 1
                if item.attempt < item.max_retries:
                    delay = 2**item.attempt  # 2s, 4s, 8s
                    item.next_retry_at = asyncio.get_event_loop().time() + delay
                    await self._queue.put(item)
                    logger.warning(
                        "background_task_retry",
                        attempt=item.attempt,
                        delay=delay,
                    )
                else:
                    if self._error_counter is not None:
                        self._error_counter.add(1)
                    logger.error(
                        "background_task_failed_permanently",
                        attempts=item.attempt,
                    )

    async def start(self) -> None:
        self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
