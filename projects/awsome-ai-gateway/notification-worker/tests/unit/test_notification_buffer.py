# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""NotificationBufferQueue 단위 테스트."""
from __future__ import annotations

import asyncio

import pytest

from worker.schemas.events import EventType, NotificationEvent, ServiceSource
from worker.services.notification_buffer import NotificationBufferQueue


def _make_event(event_id: str = "e1", event_type: EventType = EventType.BUDGET_THRESHOLD) -> NotificationEvent:
    return NotificationEvent(
        event_id=event_id,
        type=event_type,
        timestamp="2026-04-10T12:00:00+00:00",  # type: ignore[arg-type]
        source=ServiceSource.GATEWAY_PROXY,
        payload={"user_id": "u1"},
    )


async def test_enqueue_and_size() -> None:
    buf = NotificationBufferQueue(max_size=10)
    assert buf.size == 0

    event = _make_event()
    result = await buf.enqueue(event)

    assert result is True
    assert buf.size == 1


async def test_enqueue_full_drops_oldest() -> None:
    buf = NotificationBufferQueue(max_size=2)
    e1 = _make_event("e1")
    e2 = _make_event("e2")
    e3 = _make_event("e3")

    await buf.enqueue(e1)
    await buf.enqueue(e2)

    # 가득 찬 상태에서 e3 삽입 → e1이 드랍되어야 함
    result = await buf.enqueue(e3)

    assert result is False
    assert buf.size == 2


async def test_drain_processes_all_events() -> None:
    buf = NotificationBufferQueue(max_size=10)
    processed: list[str] = []

    for i in range(5):
        await buf.enqueue(_make_event(f"e{i}"))

    async def process_fn(event: NotificationEvent) -> None:
        processed.append(event.event_id)

    count = await buf.drain(process_fn)

    assert count == 5
    assert buf.size == 0
    assert len(processed) == 5


async def test_drain_re_enqueues_on_failure() -> None:
    buf = NotificationBufferQueue(max_size=10)
    await buf.enqueue(_make_event("e1"))

    fail_count = 0

    async def failing_process_fn(event: NotificationEvent) -> None:
        nonlocal fail_count
        fail_count += 1
        raise RuntimeError("simulated failure")

    count = await buf.drain(failing_process_fn)

    assert count == 0
    # 실패한 이벤트는 다시 큐에 삽입
    assert buf.size == 1
    assert fail_count == 1


async def test_drain_empty_buffer_returns_zero() -> None:
    buf = NotificationBufferQueue(max_size=10)
    count = await buf.drain(lambda e: asyncio.sleep(0))
    assert count == 0


async def test_set_metrics_called_on_drop() -> None:
    buf = NotificationBufferQueue(max_size=1)

    class FakeCounter:
        calls: list = []

        def add(self, value, attrs=None):
            self.calls.append((value, attrs))

    counter = FakeCounter()
    buf.set_metrics(counter)

    await buf.enqueue(_make_event("e1"))  # 가득 참
    await buf.enqueue(_make_event("e2"))  # 드랍 발생

    assert len(counter.calls) == 1
    assert counter.calls[0][1] == {"error_type": "buffer_overflow"}
