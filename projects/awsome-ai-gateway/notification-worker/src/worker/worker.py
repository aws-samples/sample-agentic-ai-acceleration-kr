# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import structlog

logger = structlog.get_logger(__name__)


class TaskSupervisor:
    """asyncio 태스크 생명주기 관리자.

    등록된 태스크가 예외로 종료되면 지수 백오프(1s → 2s → 4s → ... → 최대 60s)로 재시작한다 (RP-01).
    stop_all() 호출 시 모든 태스크를 취소하고 grace_period 초 대기 후 강제 종료한다.
    """

    def __init__(self, max_backoff: float = 60.0) -> None:
        self.max_backoff = max_backoff
        self._registry: dict[str, Callable[[], Awaitable[None]]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def register(self, name: str, coro_fn: Callable[[], Awaitable[None]]) -> None:
        """태스크를 등록한다.

        Args:
            name: 로그/추적용 식별자.
            coro_fn: ``await listener.run()`` 처럼 호출 가능한 코루틴 팩토리.
        """
        self._registry[name] = coro_fn

    async def start_all(self) -> None:
        """등록된 모든 태스크를 시작한다."""
        for name, coro_fn in self._registry.items():
            task: asyncio.Task[None] = asyncio.create_task(
                self._supervised_run(name, coro_fn),
                name=name,
            )
            self._tasks[name] = task

        logger.info("supervisor_started", task_count=len(self._registry))

    async def _supervised_run(
        self,
        name: str,
        coro_fn: Callable[[], Awaitable[None]],
    ) -> None:
        """지수 백오프로 태스크를 감독·재시작한다."""
        backoff: float = 1.0

        while True:
            try:
                await coro_fn()
                # 정상 종료 (일반적으로 발생하지 않음)
                logger.warning("task_exited_normally", name=name)
                break  # 정상 종료 시 재시작하지 않음
            except asyncio.CancelledError:
                logger.info("task_cancelled", name=name)
                raise  # 취소는 전파
            except Exception as exc:
                logger.error(
                    "task_failed_restarting",
                    name=name,
                    error=str(exc),
                    backoff=backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.max_backoff)

    async def stop_all(self, grace_period: float = 30.0) -> None:
        """모든 태스크를 취소하고 grace_period 초 안에 완료를 기다린다."""
        # 1. 모든 태스크 취소
        for name, task in self._tasks.items():
            if not task.done():
                task.cancel()
                logger.info("task_cancellation_requested", name=name)

        # 2. grace_period 내 완료 대기
        if self._tasks:
            done, pending = await asyncio.wait(
                list(self._tasks.values()),
                timeout=grace_period,
            )
            if pending:
                logger.warning("tasks_not_cancelled_in_time", count=len(pending))

            # 완료된 태스크의 예외를 수거하여 "Task exception was never retrieved" 경고 방지
            for task in done:
                if not task.cancelled():
                    exc = task.exception()
                    if exc is not None:
                        logger.error("task_exited_with_exception", error=str(exc))

        logger.info("supervisor_stopped")
