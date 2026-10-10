"""
Thread status when a run does not finish normally.

A run is drained by the RunBroker's background task, not by the HTTP response.
So a closed tab or a thread switch — which used to cancel the generator and
leave the thread `busy` forever, then `interrupted` once that was fixed — now
changes nothing: the run carries on and lands on `idle` like any other.

What *does* interrupt a run is cancelling it through the broker, which is what
the Stop button and server shutdown do. That path must still write
`interrupted`, and never `idle`.
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
    """Start the run, take one frame, then cancel it the way Stop does."""

    async def scenario():
        response = await service.stream_thread_execution("t-1", StreamRequest(values=VALUES))
        agen = response.body_iterator.__aiter__()
        await agen.__anext__()  # thread_created
        await asyncio.sleep(0.05)  # let the client emit its partial token
        assert service.run_broker.cancel("t-1") is True
        # The subscriber ends on its own once the run is gone.
        async for _ in agen:
            pass

    asyncio.run(scenario())


def test_a_cancelled_run_lands_on_interrupted():
    """Otherwise the thread keeps `busy` forever and the filter fills with ghosts."""
    service, threads = service_with(HangingClient())

    run_until_cancelled(service)

    assert threads.statuses, "cancellation wrote no status at all"
    assert threads.statuses[-1] == ThreadStatus.INTERRUPTED


def test_a_disconnected_client_does_not_interrupt_the_run():
    """The point of the broker: closing the tab is not a Stop.

    The response generator is closed (GeneratorExit) exactly as the ASGI server
    does on disconnect. The run behind it must still be registered and still
    unfinished, with no `interrupted` written. Cancelled at the end only to let
    the loop shut down cleanly.
    """
    service, threads = service_with(HangingClient())

    async def scenario():
        response = await service.stream_thread_execution("t-1", StreamRequest(values=VALUES))
        agen = response.body_iterator.__aiter__()
        await agen.__anext__()
        await agen.aclose()
        await asyncio.sleep(0.1)
        still_running = service.run_broker.get("t-1") is not None
        statuses_after_disconnect = list(threads.statuses)
        service.run_broker.cancel("t-1")
        await asyncio.sleep(0.05)
        return still_running, statuses_after_disconnect

    still_running, statuses = asyncio.run(scenario())
    assert still_running, "the run ended with its subscriber"
    assert ThreadStatus.INTERRUPTED not in statuses


def test_a_cancelled_run_does_not_land_on_idle():
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
