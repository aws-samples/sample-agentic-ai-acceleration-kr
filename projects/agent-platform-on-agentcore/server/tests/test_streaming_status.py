"""
Thread status when a stream does not finish normally.

Stopping a run, closing the tab, or navigating away aborts the HTTP request,
which cancels the generator. Cancellation raises asyncio.CancelledError — a
BaseException, so `except Exception` never sees it. Without a finally the thread
keeps the `busy` it was given at the start of the run, forever: every Stop click
and every closed tab leaves a ghost that the busy filter then accumulates.

Writing `interrupted` on cancellation clears the ghosts and gives that status its
only writer, which is what makes the sidebar's warning dot and its status filter
mean anything.
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.common import StreamRequest, ThreadStatus  # noqa: E402
from services.streaming_service import StreamingService  # noqa: E402

VALUES = {"messages": [{"id": "1", "type": "human", "content": "hello"}]}


class StubThread:
    def __init__(self):
        self.values = dict(VALUES)
        self.updated_at = ""


class StubRepo:
    def update(self, thread_id, thread):
        return thread


class StubThreadService:
    """Records every status written, so the test can assert the final one."""

    def __init__(self):
        self.repository = StubRepo()
        self.statuses = []

    def get_or_create_thread(
        self,
        thread_id,
        owner_sub="",
        initial_values=None,
        agent_record_id="",
        agent_name="", **_kwargs):
        return StubThread()

    def get_thread(self, thread_id):
        return StubThread()

    def update_thread_status(self, thread_id, status):
        self.statuses.append(status)


class HangingClient:
    """A stream that yields once then blocks, so the consumer can abandon it."""

    async def execute_stream(self, thread_id, values, config=None, actor_id=None):
        yield {"event": {"contentBlockDelta": {"delta": {"text": "partial"}}}}
        await asyncio.sleep(3600)


class ExplodingClient:
    async def execute_stream(self, thread_id, values, config=None, actor_id=None):
        raise RuntimeError("bedrock is unhappy")
        yield  # pragma: no cover — makes this an async generator


def service_with(client):
    threads = StubThreadService()
    service = StreamingService(thread_service=threads, agentcore_client=client)
    service._get_agent_client = lambda config=None: client
    return service, threads


def run_until_cancelled(service):
    """Start the stream, take one chunk, then cancel — what Stop does."""

    async def scenario():
        response = await service.stream_thread_execution("t-1", StreamRequest(values=VALUES))
        agen = response.body_iterator.__aiter__()

        async def drain():
            async for _ in agen:
                pass

        task = asyncio.ensure_future(drain())
        # Let the generator start and emit before pulling the rug out.
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # aclose() runs the generator's finally, which is what a disconnecting
        # client triggers in the ASGI server.
        try:
            await agen.aclose()
        except (asyncio.CancelledError, RuntimeError):
            pass

    asyncio.run(scenario())


def test_a_cancelled_stream_lands_on_interrupted():
    """Otherwise the thread keeps `busy` forever and the filter fills with ghosts."""
    service, threads = service_with(HangingClient())

    run_until_cancelled(service)

    assert threads.statuses, "cancellation wrote no status at all"
    assert threads.statuses[-1] == ThreadStatus.INTERRUPTED


def test_a_disconnected_client_lands_on_interrupted():
    """The common case, and the one CancelledError alone does not cover.

    A streaming generator sits suspended at a `yield` almost all the time. Closing
    it there raises GeneratorExit, not CancelledError — and GeneratorExit is not an
    `Exception` either, so handling only cancellation still left the thread busy.
    """
    service, threads = service_with(HangingClient())

    async def scenario():
        response = await service.stream_thread_execution("t-1", StreamRequest(values=VALUES))
        agen = response.body_iterator.__aiter__()
        await agen.__anext__()  # generator is now parked at a yield
        await agen.aclose()  # what the ASGI server does on disconnect

    asyncio.run(scenario())

    assert threads.statuses[-1] == ThreadStatus.INTERRUPTED


def test_a_cancelled_stream_does_not_land_on_idle():
    """`idle` would claim the turn completed; it did not."""
    service, threads = service_with(HangingClient())

    run_until_cancelled(service)

    assert ThreadStatus.IDLE not in threads.statuses


def test_an_in_band_error_still_lands_on_error():
    """Cancellation handling must not swallow the existing error path."""
    service, threads = service_with(ExplodingClient())

    async def scenario():
        response = await service.stream_thread_execution("t-1", StreamRequest(values=VALUES))
        async for _ in response.body_iterator:
            pass

    asyncio.run(scenario())

    assert threads.statuses[-1] == ThreadStatus.ERROR
