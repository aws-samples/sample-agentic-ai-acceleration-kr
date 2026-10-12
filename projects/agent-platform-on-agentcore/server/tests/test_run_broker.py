"""
A run must outlive the response that started it.

The SSE generator used to be iterated by the HTTP response itself, so a closed
tab closed the generator, which closed the runtime stream, which cancelled the
agent. The broker puts a background task between the two: the task drains the
generator to the end whatever the browser does, and a response is merely one
subscriber among any number.
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.run_broker import (  # noqa: E402
    HEARTBEAT_FRAME,
    RunAlreadyActive,
    RunBroker,
)


async def frames(*items, gap=0.0, hang=False):
    for item in items:
        if gap:
            await asyncio.sleep(gap)
        yield item
    if hang:
        await asyncio.sleep(3600)


async def drain(agen):
    return [f async for f in agen]


def test_a_subscriber_sees_frames_from_before_and_after_it_joined():
    async def scenario():
        broker = RunBroker()
        run = broker.start("t", frames("a", "b", "c", gap=0.02))
        await asyncio.sleep(0.03)  # "a" is already out
        return await drain(run.subscribe(heartbeat_interval=10))

    assert asyncio.run(scenario()) == ["a", "b", "c"]


def test_dropping_a_subscriber_does_not_stop_the_run():
    seen = []

    async def source():
        for i in range(4):
            await asyncio.sleep(0.02)
            seen.append(i)
            yield str(i)

    async def scenario():
        broker = RunBroker()
        run = broker.start("t", source())
        sub = run.subscribe(heartbeat_interval=10).__aiter__()
        await sub.__anext__()
        await sub.aclose()  # what a closed tab does to the response
        await asyncio.sleep(0.15)
        return run.done, broker.get("t")

    done, registered = asyncio.run(scenario())
    assert seen == [0, 1, 2, 3], "the source stopped when its subscriber left"
    assert done is True
    assert registered is None, "a finished run must leave the registry"


def test_cancel_interrupts_the_source_and_ends_subscribers():
    cancelled = []

    async def source():
        try:
            yield "a"
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    async def scenario():
        broker = RunBroker()
        run = broker.start("t", source())
        task = asyncio.ensure_future(drain(run.subscribe(heartbeat_interval=10)))
        await asyncio.sleep(0.02)
        assert broker.cancel("t") is True
        got = await asyncio.wait_for(task, 1)
        return got, broker.cancel("t")

    got, second = asyncio.run(scenario())
    assert got == ["a"]
    assert cancelled == [True]
    assert second is False, "cancelling a run that is gone must say so"


def test_source_heartbeats_are_not_buffered_and_idle_subscribers_get_their_own():
    async def scenario():
        broker = RunBroker()
        run = broker.start(
            "t", frames("a", HEARTBEAT_FRAME, HEARTBEAT_FRAME, "b", gap=0.01, hang=True)
        )
        await asyncio.sleep(0.1)  # everything but the hang is out
        sub = run.subscribe(heartbeat_interval=0.05).__aiter__()
        got = [await sub.__anext__() for _ in range(3)]
        await sub.aclose()
        broker.cancel("t")
        await asyncio.sleep(0)
        return run.frames, got

    buffered, got = asyncio.run(scenario())
    assert buffered == ["a", "b"]
    assert got[:2] == ["a", "b"]
    assert got[2] == HEARTBEAT_FRAME, "a silent tail must still put bytes on the wire"


def test_a_second_run_on_the_same_thread_is_refused():
    async def scenario():
        broker = RunBroker()
        broker.start("t", frames("a", hang=True))
        with pytest.raises(RunAlreadyActive):
            broker.start("t", frames("b"))
        broker.cancel("t")
        await asyncio.sleep(0)

    asyncio.run(scenario())


def test_shutdown_cancels_every_run():
    cancelled = []

    async def source(name):
        try:
            yield name
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.append(name)
            raise

    async def scenario():
        broker = RunBroker()
        broker.start("t1", source("t1"))
        broker.start("t2", source("t2"))
        await asyncio.sleep(0.02)
        await broker.shutdown()
        return broker.get("t1"), broker.get("t2")

    assert asyncio.run(scenario()) == (None, None)
    assert sorted(cancelled) == ["t1", "t2"]


def test_a_source_that_raises_still_ends_its_subscribers():
    async def source():
        yield "a"
        raise RuntimeError("boom")

    async def scenario():
        broker = RunBroker()
        run = broker.start("t", source())
        return await asyncio.wait_for(drain(run.subscribe(heartbeat_interval=10)), 1)

    assert asyncio.run(scenario()) == ["a"]


def test_cancel_right_after_start_still_clears_the_registry():
    """A task cancelled before its first step never enters `_pump`, so a
    `finally` there never runs. The thread would answer 409 forever."""

    async def scenario():
        broker = RunBroker()
        run = broker.start("t", frames("a", hang=True))
        assert broker.cancel("t") is True
        await asyncio.sleep(0.01)
        return run.done, broker.get("t")

    assert asyncio.run(scenario()) == (True, None)


def test_cancel_from_a_worker_thread_reaches_the_loop():
    """Routes run in FastAPI's threadpool; a Task is not thread-safe."""
    cancelled = []

    async def source():
        try:
            yield "a"
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    async def scenario():
        broker = RunBroker()
        broker.start("t", source())
        await asyncio.sleep(0.02)
        result = await asyncio.to_thread(broker.cancel, "t")
        await asyncio.sleep(0.05)
        return result, broker.get("t")

    assert asyncio.run(scenario()) == (True, None)
    assert cancelled == [True]


def test_a_stop_during_the_reservation_cancels_the_run_when_it_launches():
    """Stop during the seconds of pre-stream setup. The thread is reserved but
    has no task yet; the Stop is kept on the reservation and applied at launch,
    after the generator's first step so its own handler writes `interrupted`."""
    cancelled = []

    async def source():
        try:
            yield "a"
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    async def scenario():
        broker = RunBroker()
        reservation = broker.reserve("t", owner_sub="alice")
        assert broker.cancel("t", owner_sub="alice") is True
        run = broker.launch(reservation, source())
        got = await asyncio.wait_for(drain(run.subscribe(heartbeat_interval=10)), 1)
        return got, broker.get("t")

    got, registered = asyncio.run(scenario())
    assert got == ["a"], "the run must start (and persist its interruption) before dying"
    assert cancelled == [True]
    assert registered is None


def test_a_reservation_blocks_a_second_run_and_feeds_an_early_subscriber():
    async def scenario():
        broker = RunBroker()
        reservation = broker.reserve("t")
        with pytest.raises(RunAlreadyActive):
            broker.start("t", frames("x"))
        assert broker.get("t") is reservation
        early = asyncio.ensure_future(drain(reservation.subscribe(heartbeat_interval=10)))
        await asyncio.sleep(0.01)
        broker.launch(reservation, frames("a", "b"))
        return await asyncio.wait_for(early, 1)

    assert asyncio.run(scenario()) == ["a", "b"]


def test_releasing_a_reservation_frees_the_thread_and_ends_its_subscribers():
    async def scenario():
        broker = RunBroker()
        reservation = broker.reserve("t")
        early = asyncio.ensure_future(drain(reservation.subscribe(heartbeat_interval=10)))
        await asyncio.sleep(0.01)
        broker.release(reservation)  # pre-stream setup failed; no run will come
        got = await asyncio.wait_for(early, 1)
        broker.start("t", frames("again"))  # the thread is free again
        broker.cancel("t")
        await asyncio.sleep(0.01)
        return got

    assert asyncio.run(scenario()) == []


def test_a_stranger_cannot_cancel_a_reservation():
    async def scenario():
        broker = RunBroker()
        reservation = broker.reserve("t", owner_sub="alice")
        assert broker.cancel("t", owner_sub="mallory") is False
        run = broker.launch(reservation, frames("a", hang=True))
        await asyncio.sleep(0.05)
        alive = broker.get("t") is run and not run.done
        broker.cancel("t")
        await asyncio.sleep(0.01)
        return alive

    assert asyncio.run(scenario()) is True


def test_an_overflowing_subscriber_is_dropped_but_the_run_continues(monkeypatch):
    """A listener that never reads must not pin the whole run in its queue."""
    import services.run_broker as module

    monkeypatch.setattr(module, "SUBSCRIBER_QUEUE_LIMIT", 3)

    async def scenario():
        broker = RunBroker()
        run = broker.start("t", frames(*[str(i) for i in range(10)], gap=0.001))
        sub = run.subscribe(heartbeat_interval=10).__aiter__()
        first = await sub.__anext__()
        await asyncio.sleep(0.1)  # the run finishes while nobody reads
        rest = [f async for f in sub]
        return first, rest, run.frames, run.done

    first, rest, buffered, done = asyncio.run(scenario())
    assert first == "0"
    assert done is True
    assert buffered == [str(i) for i in range(10)], "dropping a listener must not drop frames"
    assert len(rest) <= 3, "a dropped listener must end, not drain a backlog"
