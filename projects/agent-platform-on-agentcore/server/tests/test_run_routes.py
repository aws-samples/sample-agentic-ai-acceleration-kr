"""
HTTP contract for attaching to and cancelling a run in flight.

The UI reopens a `busy` thread by attaching to its run and replays what it
missed; it stops a run by cancelling it, because dropping the connection no
longer does. Both go through ownership like every other thread route.
"""
import asyncio
import os
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.auth as auth  # noqa: E402
import routes.threads as thread_routes  # noqa: E402
from models.thread import Thread  # noqa: E402
from services.run_broker import RunBroker  # noqa: E402

ALICE = auth.AuthUser(sub="user-1", username="alice", groups=[])


class StubThreadService:
    def require_owned(self, thread_id, owner_sub, is_admin=False):
        return Thread(
            thread_id=thread_id,
            created_at="2026-08-07T00:00:00",
            updated_at="2026-08-07T00:00:00",
            owner_sub=owner_sub,
        )


class StubStreamingService:
    def __init__(self):
        self.run_broker = RunBroker()


def client_with(streaming):
    app = FastAPI()
    app.include_router(thread_routes.router)
    app.dependency_overrides[auth.current_user] = lambda: ALICE
    thread_routes.streaming_service = streaming
    thread_routes.thread_service = StubThreadService()
    return TestClient(app)


async def two_frames_then_hang():
    yield "data: {\"a\": 1}\n\n"
    yield "data: {\"b\": 2}\n\n"
    await asyncio.sleep(3600)


def test_attaching_to_a_thread_without_a_run_is_404():
    client = client_with(StubStreamingService())
    assert client.get("/threads/t-1/runs/stream").status_code == 404


def test_attaching_replays_the_run_so_far():
    """Called as a function, not over TestClient: Starlette's test transport
    buffers a whole response body before returning it, and a run in flight has
    no end to wait for. The route is thin enough that its own return value is
    the contract — the status and headers are what TestClient would send."""
    streaming = StubStreamingService()
    thread_routes.streaming_service = streaming
    thread_routes.thread_service = StubThreadService()

    async def scenario():
        streaming.run_broker.start("t-1", two_frames_then_hang())
        await asyncio.sleep(0.05)  # both frames are out before anyone listens
        response = thread_routes.attach_thread_run("t-1", ALICE)
        assert response.status_code == 200
        assert response.media_type == "text/event-stream"
        assert response.headers["cache-control"] == "no-cache, no-transform"
        agen = response.body_iterator.__aiter__()
        baseline = await agen.__anext__()  # see test_attaching_sends_the_baseline_before_the_replay
        first = await agen.__anext__()
        second = await agen.__anext__()
        assert streaming.run_broker.cancel("t-1") is True
        rest = [f async for f in agen]
        return first, second, rest

    first, second, rest = asyncio.run(scenario())
    assert '"a": 1' in first, "the frame from before the attach was not replayed"
    assert '"b": 2' in second
    assert rest == [], "the subscriber must end with the run"


def test_cancel_reports_whether_anything_was_running():
    streaming = StubStreamingService()
    client = client_with(streaming)

    async def hanging():
        yield "data: {}\n\n"
        await asyncio.sleep(3600)

    async def start():
        streaming.run_broker.start("t-1", hanging())
        await asyncio.sleep(0.01)

    with client:
        assert client.post("/threads/t-1/runs/cancel").json() == {"cancelled": False}
        client.portal.call(start)
        assert client.post("/threads/t-1/runs/cancel").json() == {"cancelled": True}


class NotFoundThreadService(StubThreadService):
    """The thread does not exist yet — Stop during a first turn's setup."""

    def require_owned(self, thread_id, owner_sub, is_admin=False):
        from services.thread_service import ThreadNotFound

        raise ThreadNotFound(thread_id)


def test_cancel_on_a_thread_not_yet_created_reaches_its_reservation():
    """Stop during a first turn's setup: the row does not exist (404 from
    ownership), but the run is reserved by this caller, and that is what the
    Stop must land on."""
    streaming = StubStreamingService()
    client = client_with(streaming)
    thread_routes.thread_service = NotFoundThreadService()

    async def reserve():
        streaming.run_broker.reserve("t-new", owner_sub=ALICE.sub)

    with client:
        client.portal.call(reserve)
        assert client.post("/threads/t-new/runs/cancel").json() == {"cancelled": True}

    async def scenario():
        cancelled = []

        async def source():
            try:
                yield "a"
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

        run = streaming.run_broker.get("t-new")
        streaming.run_broker.launch(run, source())
        await asyncio.sleep(0.05)
        return cancelled, streaming.run_broker.get("t-new")

    assert asyncio.run(scenario()) == ([True], None)


def test_attaching_sends_the_baseline_before_the_replay():
    """The reattaching client keeps only the stored messages the run started
    from; it learns which from the first frame, before any replayed event."""
    streaming = StubStreamingService()
    thread_routes.streaming_service = streaming
    thread_routes.thread_service = StubThreadService()

    async def scenario():
        reservation = streaming.run_broker.reserve("t-1", owner_sub=ALICE.sub)
        streaming.run_broker.launch(reservation, two_frames_then_hang(), ["u-1", "a-1", "u-2"])
        await asyncio.sleep(0.05)
        response = thread_routes.attach_thread_run("t-1", ALICE)
        agen = response.body_iterator.__aiter__()
        first = await agen.__anext__()
        second = await agen.__anext__()
        streaming.run_broker.cancel("t-1")
        async for _ in agen:
            pass
        return first, second

    first, second = asyncio.run(scenario())
    assert first.startswith("data: ")
    import json

    payload = json.loads(first[len("data: "):])
    assert payload == {"event": "run_baseline", "data": {"message_ids": ["u-1", "a-1", "u-2"]}}
    assert '"a": 1' in second


def test_deleting_a_thread_stops_its_run():
    streaming = StubStreamingService()
    client = client_with(streaming)

    class DeletingThreadService(StubThreadService):
        deleted = []

        def delete_thread(self, thread_id):
            self.deleted.append(thread_id)

    thread_routes.thread_service = DeletingThreadService()

    async def hanging():
        yield "data: {}\n\n"
        await asyncio.sleep(3600)

    async def start():
        streaming.run_broker.start("t-1", hanging())
        await asyncio.sleep(0.01)

    async def settled():
        await asyncio.sleep(0.05)
        return streaming.run_broker.get("t-1")

    with client:
        client.portal.call(start)
        assert client.delete("/threads/t-1").json() == {"status": "deleted"}
        assert client.portal.call(settled) is None, "the run outlived its thread"
