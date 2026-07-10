# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""TaskSupervisor 단위 테스트."""
from __future__ import annotations

import asyncio

import pytest

from worker.worker import TaskSupervisor


async def test_start_all_runs_registered_tasks() -> None:
    results: list[str] = []
    stop_event = asyncio.Event()

    async def task_a():
        results.append("a")
        await stop_event.wait()

    supervisor = TaskSupervisor()
    supervisor.register("task_a", task_a)
    await supervisor.start_all()

    # 태스크가 시작되어 실행 중인지 확인
    await asyncio.sleep(0.01)
    assert "a" in results

    stop_event.set()
    await supervisor.stop_all(grace_period=1.0)


async def test_stop_all_cancels_running_tasks() -> None:
    stop_called = asyncio.Event()

    async def infinite_task():
        try:
            await asyncio.sleep(9999)
        except asyncio.CancelledError:
            stop_called.set()
            raise

    supervisor = TaskSupervisor()
    supervisor.register("infinite", infinite_task)
    await supervisor.start_all()

    await asyncio.sleep(0.01)
    await supervisor.stop_all(grace_period=1.0)

    assert stop_called.is_set()


async def test_supervisor_restarts_failed_task() -> None:
    """예외로 종료된 태스크는 backoff 후 재시작된다."""
    run_count = 0
    done_event = asyncio.Event()

    async def flaky_task():
        nonlocal run_count
        run_count += 1
        if run_count < 2:
            raise RuntimeError("first failure")
        done_event.set()
        await asyncio.sleep(9999)

    supervisor = TaskSupervisor(max_backoff=0.01)  # backoff 최소화
    supervisor.register("flaky", flaky_task)
    await supervisor.start_all()

    await asyncio.wait_for(done_event.wait(), timeout=2.0)
    await supervisor.stop_all(grace_period=0.1)

    assert run_count >= 2
