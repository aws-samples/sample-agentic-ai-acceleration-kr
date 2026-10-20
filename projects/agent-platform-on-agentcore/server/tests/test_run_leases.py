"""Runs across server tasks (services/run_leases.py).

With two tasks behind Cloud Map DNS the in-memory broker alone let a second
POST start a second run on a busy thread 4 times in 12, and a reattach or Stop
reach the run only when it happened to land on the right task (bst load test,
2026-10-18). Two brokers with different holders over one lease store stand in
for two tasks here; the store keeps the conditions RunRepository writes.
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.auth as auth  # noqa: E402
import routes.threads as thread_routes  # noqa: E402
from models.thread import Thread  # noqa: E402
from services.run_broker import RunAlreadyActive, RunBroker  # noqa: E402
from services.run_leases import FORWARDED_HEADER, RunLeases  # noqa: E402

ALICE = auth.AuthUser(sub="user-1", username="alice", groups=[])


class Clock:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self):
        return self.now


class FakeRunRepository:
    """RunRepository's conditions over a dict."""

    def __init__(self):
        self.rows = {}

    def acquire(self, thread_id, run_id, holder, owner_sub, now_ms, lease_ms):
        row = self.rows.get(thread_id)
        if row is not None and row["lease_until"] >= now_ms:
            return False
        self.rows[thread_id] = {"thread_id": thread_id, "run_id": run_id, "holder": holder,
                                "owner_sub": owner_sub, "lease_until": now_ms + lease_ms}
        return True

    def renew(self, thread_id, run_id, now_ms, lease_ms):
        row = self.rows.get(thread_id)
        if row is None or row["run_id"] != run_id:
            return None
        row["lease_until"] = now_ms + lease_ms
        return dict(row)

    def release(self, thread_id, run_id):
        row = self.rows.get(thread_id)
        if row is not None and row["run_id"] == run_id:
            del self.rows[thread_id]

    def get(self, thread_id):
        return self.rows.get(thread_id)

    def request_cancel(self, thread_id, owner_sub, now_ms):
        row = self.rows.get(thread_id)
        if row is None or row["lease_until"] < now_ms:
            return False
        if owner_sub and row.get("owner_sub") not in (None, owner_sub):
            return False
        row["cancel_requested"] = True
        return True


def two_tasks(clock=None):
    repo, clock = FakeRunRepository(), clock or Clock()
    a = RunBroker(leases=RunLeases(repo, 8000, lease_seconds=60, clock=clock, address=lambda p: f"10.0.0.1:{p}"))
    b = RunBroker(leases=RunLeases(repo, 8000, lease_seconds=60, clock=clock, address=lambda p: f"10.0.0.2:{p}"))
    return repo, clock, a, b


async def hang():
    yield "data: {}\n\n"
    await asyncio.sleep(3600)


def test_a_run_on_one_task_refuses_a_second_on_the_other():
    repo, _, a, b = two_tasks()

    async def scenario():
        await a.claim("t-1", owner_sub="user-1")
        with pytest.raises(RunAlreadyActive):
            await b.claim("t-1", owner_sub="user-1")
        return b.get("t-1")

    assert asyncio.run(scenario()) is None, "the refused claim must leave no local reservation"
    assert repo.rows["t-1"]["holder"] == "10.0.0.1:8000"


def test_the_other_task_learns_who_holds_the_run():
    _, _, a, b = two_tasks()

    async def scenario():
        await a.claim("t-1")
        return b.remote_holder("t-1"), a.remote_holder("t-1")

    assert asyncio.run(scenario()) == ("10.0.0.1:8000", None)


def test_the_lease_goes_when_the_run_ends():
    repo, _, a, b = two_tasks()

    async def scenario():
        run = await a.claim("t-1")
        a.launch(run, hang())
        await asyncio.sleep(0.01)
        a.cancel("t-1")
        await asyncio.sleep(0.05)  # done callback, then the executor's delete
        await b.claim("t-1")

    asyncio.run(scenario())
    assert repo.rows["t-1"]["holder"] == "10.0.0.2:8000"


def test_a_failed_setup_frees_the_lease():
    repo, _, a, _ = two_tasks()

    async def scenario():
        run = await a.claim("t-1")
        a.release(run)
        await asyncio.sleep(0.05)

    asyncio.run(scenario())
    assert "t-1" not in repo.rows


def test_a_killed_holder_frees_the_thread_after_the_lease():
    """No shutdown ran, so nothing deleted the row; it stops being renewed."""
    repo, clock, a, b = two_tasks()

    async def scenario():
        await a.claim("t-1")
        clock.now += 59
        with pytest.raises(RunAlreadyActive):
            await b.claim("t-1")
        clock.now += 2
        await b.claim("t-1")

    asyncio.run(scenario())
    assert repo.rows["t-1"]["holder"] == "10.0.0.2:8000"


def test_renewal_keeps_a_long_run_claimed():
    _, clock, a, b = two_tasks()

    async def scenario():
        run = await a.claim("t-1")
        a.launch(run, hang())
        for _ in range(5):
            clock.now += 30
            await a.renew_leases()
        with pytest.raises(RunAlreadyActive):
            await b.claim("t-1")
        a.cancel("t-1")
        await asyncio.sleep(0.05)

    asyncio.run(scenario())


def test_a_flagged_stop_reaches_the_holder_on_its_next_renewal():
    repo, _, a, b = two_tasks()

    async def scenario():
        run = await a.claim("t-1", owner_sub="user-1")
        a.launch(run, hang())
        await asyncio.sleep(0.01)
        assert b.leases.request_cancel("t-1", "user-1") is True
        await a.renew_leases()
        await asyncio.sleep(0.05)
        return a.get("t-1")

    assert asyncio.run(scenario()) is None


def test_only_the_runs_owner_can_flag_it():
    repo, _, a, b = two_tasks()

    async def scenario():
        await a.claim("t-1", owner_sub="user-1")
        return b.leases.request_cancel("t-1", "someone-else")

    assert asyncio.run(scenario()) is False
    assert "cancel_requested" not in repo.rows["t-1"]


def test_a_lease_store_outage_lets_the_turn_run():
    class Down(FakeRunRepository):
        def acquire(self, *args):
            raise RuntimeError("DynamoDB unavailable")

    broker = RunBroker(leases=RunLeases(Down(), 8000, address=lambda p: "10.0.0.1:8000"))

    async def scenario():
        run = await broker.claim("t-1")
        with pytest.raises(RunAlreadyActive):
            await broker.claim("t-1")  # still refused on this task
        return run.lease_held

    assert asyncio.run(scenario()) is False


# --- routes: what a task does with a request for another task's run -----------


class StubThreadService:
    def require_owned(self, thread_id, owner_sub, is_admin=False):
        return Thread(thread_id=thread_id, created_at="2026-10-18T00:00:00",
                      updated_at="2026-10-18T00:00:00", owner_sub=owner_sub)


class Streaming:
    def __init__(self, broker):
        self.run_broker = broker


class _Url:
    def __init__(self, path):
        self.path = path


class FakeRequest:
    def __init__(self, path, headers=None):
        self.url = _Url(path)
        self.headers = headers or {"authorization": "Bearer tok"}


@pytest.fixture
def routes_on_b(monkeypatch):
    _, _, a, b = two_tasks()
    monkeypatch.setattr(thread_routes, "streaming_service", Streaming(b))
    monkeypatch.setattr(thread_routes, "thread_service", StubThreadService())
    return a, b


def test_a_reattach_on_the_other_task_is_relayed_from_the_holder(routes_on_b, monkeypatch):
    a, _ = routes_on_b
    opened = []

    async def fake_open(holder, path, authorization):
        opened.append((holder, path, authorization))

        async def frames():
            yield b"data: relayed\n\n"
        return frames()

    monkeypatch.setattr(thread_routes, "open_peer_stream", fake_open)

    async def scenario():
        await a.claim("t-1")
        response = await thread_routes.attach_thread_run(
            "t-1", FakeRequest("/threads/t-1/runs/stream"), ALICE
        )
        return [chunk async for chunk in response.body_iterator]

    assert asyncio.run(scenario()) == [b"data: relayed\n\n"]
    assert opened == [("10.0.0.1:8000", "/threads/t-1/runs/stream", "Bearer tok")]


def test_a_forwarded_reattach_is_never_forwarded_again(routes_on_b, monkeypatch):
    a, _ = routes_on_b
    monkeypatch.setattr(thread_routes, "open_peer_stream", lambda *a: pytest.fail("bounced"))

    async def scenario():
        await a.claim("t-1")
        await thread_routes.attach_thread_run(
            "t-1", FakeRequest("/threads/t-1/runs/stream", {FORWARDED_HEADER: "1"}), ALICE
        )

    with pytest.raises(thread_routes.HTTPException) as err:
        asyncio.run(scenario())
    assert err.value.status_code == 404


def test_a_stop_on_the_other_task_is_handed_to_the_holder(routes_on_b, monkeypatch):
    a, _ = routes_on_b
    sent = []
    monkeypatch.setattr(thread_routes, "forward_cancel", lambda h, p, auth: sent.append((h, p)) or True)

    async def scenario():
        await a.claim("t-1", owner_sub=ALICE.sub)

    asyncio.run(scenario())
    out = thread_routes.cancel_thread_run("t-1", FakeRequest("/threads/t-1/runs/cancel"), ALICE)
    assert out == {"cancelled": True}
    assert sent == [("10.0.0.1:8000", "/threads/t-1/runs/cancel")]


def test_an_unreachable_holder_gets_the_stop_on_its_lease(routes_on_b, monkeypatch):
    a, b = routes_on_b
    monkeypatch.setattr(thread_routes, "forward_cancel", lambda *args: None)

    async def scenario():
        await a.claim("t-1", owner_sub=ALICE.sub)

    asyncio.run(scenario())
    out = thread_routes.cancel_thread_run("t-1", FakeRequest("/threads/t-1/runs/cancel"), ALICE)
    assert out == {"cancelled": True}
    assert b.leases.repository.rows["t-1"]["cancel_requested"] is True
