"""
The thread is reserved before the pre-stream setup, not after.

Two requests used to be able to pass the "is a run active" check together,
and the second then ran `get_or_create_thread` — a whole-row put — after the
first run had started writing, clobbering its human message. And the check
ran before ownership, so a guessed id on someone else's busy thread learned
that it existed and was running (409 instead of 403).
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.common import StreamRequest  # noqa: E402
from services.run_broker import RunAlreadyActive  # noqa: E402
from services.streaming_service import StreamingService  # noqa: E402
from services.thread_service import ThreadForbidden  # noqa: E402

VALUES = {"messages": [{"id": "u-2", "type": "human", "content": "hello"}]}


class StubThread:
    def __init__(self, owner_sub="alice"):
        self.values = {"messages": [
            {"id": "u-1", "type": "human", "content": "earlier"},
            {"id": "a-1", "type": "ai", "content": "answer"},
        ]}
        self.updated_at = ""
        self.owner_sub = owner_sub


class StubRepo:
    def update(self, thread_id, thread):
        return thread


class StubThreadService:
    def __init__(self, owner_sub="alice", setup_delay=0.0, setup_raises=None):
        self.repository = StubRepo()
        self.owner_sub = owner_sub
        self.setup_delay = setup_delay
        self.setup_raises = setup_raises
        self.setup_calls = 0
        self.statuses = []

    def get_or_create_thread(self, thread_id, owner_sub="", initial_values=None,
                             agent_record_id="", agent_name="", **_kwargs):
        self.setup_calls += 1
        if self.setup_delay:
            import time
            time.sleep(self.setup_delay)
        if self.setup_raises:
            raise self.setup_raises
        return StubThread(self.owner_sub)

    def get_thread(self, thread_id):
        return StubThread(self.owner_sub)

    def update_thread_status(self, thread_id, status):
        self.statuses.append(status)


class HangingClient:
    async def execute_stream(self, thread_id, values, config=None, actor_id=None):
        yield {"event": {"contentBlockDelta": {"delta": {"text": "partial"}}}}
        await asyncio.sleep(3600)


def service_with(threads):
    client = HangingClient()
    service = StreamingService(thread_service=threads, agentcore_client=client)
    service._get_agent_client = lambda config=None: client
    return service


def test_a_stranger_gets_forbidden_before_learning_the_thread_is_busy():
    threads = StubThreadService(owner_sub="bob")
    service = service_with(threads)

    async def scenario():
        service.run_broker.reserve("t-1", owner_sub="bob")
        with pytest.raises(ThreadForbidden):
            await service.stream_thread_execution("t-1", StreamRequest(values=VALUES), owner_sub="alice")
        service.run_broker.release(service.run_broker.get("t-1"))

    asyncio.run(scenario())
    assert threads.setup_calls == 0


def test_the_owner_gets_409_while_a_run_is_active():
    service = service_with(StubThreadService())

    async def scenario():
        await service.stream_thread_execution("t-1", StreamRequest(values=VALUES), owner_sub="alice")
        with pytest.raises(RunAlreadyActive):
            await service.stream_thread_execution("t-1", StreamRequest(values=VALUES), owner_sub="alice")
        service.run_broker.cancel("t-1")
        await asyncio.sleep(0.05)

    asyncio.run(scenario())


def test_a_second_request_during_setup_is_refused_without_touching_the_row():
    threads = StubThreadService(setup_delay=0.2)
    service = service_with(threads)

    async def scenario():
        first = asyncio.ensure_future(
            service.stream_thread_execution("t-1", StreamRequest(values=VALUES), owner_sub="alice")
        )
        await asyncio.sleep(0.05)  # first is inside get_or_create_thread
        with pytest.raises(RunAlreadyActive):
            await service.stream_thread_execution("t-1", StreamRequest(values=VALUES), owner_sub="alice")
        await first
        service.run_broker.cancel("t-1")
        await asyncio.sleep(0.05)

    asyncio.run(scenario())
    assert threads.setup_calls == 1


def test_a_failed_setup_releases_the_reservation():
    threads = StubThreadService(setup_raises=ThreadForbidden("nope"))
    service = service_with(threads)

    async def scenario():
        with pytest.raises(ThreadForbidden):
            await service.stream_thread_execution("t-1", StreamRequest(values=VALUES), owner_sub="alice")
        return service.run_broker.get("t-1")

    assert asyncio.run(scenario()) is None


def test_the_run_records_the_transcript_it_started_from():
    """What a reattaching client keeps: the stored messages from before this
    run, plus this run's own question (saved by the generator, so not in the
    stored list at reservation time but not rebuilt by the replay either)."""
    service = service_with(StubThreadService())

    async def scenario():
        await service.stream_thread_execution("t-1", StreamRequest(values=VALUES), owner_sub="alice")
        run = service.run_broker.get("t-1")
        ids = list(run.baseline_message_ids)
        service.run_broker.cancel("t-1")
        await asyncio.sleep(0.05)
        return ids

    assert asyncio.run(scenario()) == ["u-1", "a-1", "u-2"]
