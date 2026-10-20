"""Socket Mode connections and their manager (integrations/slack/connection.py)."""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from integrations.slack.api import SlackApiError, SlackResponse  # noqa: E402
from integrations.slack.connection import ConnectionManager, SocketModeConnection  # noqa: E402


def integration(harness_id="h1", integration_id="i1", status="active"):
    return {
        "harness_id": harness_id, "integration_id": integration_id, "status": status,
        "app_id": "A1", "team_id": "T1", "secret_arn": f"arn:{harness_id}",
    }


class Socket:
    """One scripted WebSocket session. Ends (like a closed socket) after its script."""

    def __init__(self, script, hold=False):
        self.script = list(script)
        self.hold = hold
        self.sent = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True

    def __aiter__(self):
        return self._messages()

    async def _messages(self):
        for message in self.script:
            yield json.dumps(message)
            await asyncio.sleep(0)
        if self.hold:
            while not self.closed:
                await asyncio.sleep(0.01)

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def close(self):
        self.closed = True


class Api:
    def __init__(self, errors=()):
        self.errors = list(errors)
        self.opened = 0
        self.closed = False

    async def open_connection(self, app_token):
        self.opened += 1
        if self.errors:
            raise self.errors.pop(0)
        return SlackResponse(data={"ok": True, "url": f"wss://example/{self.opened}"})

    async def aclose(self):
        self.closed = True


def connector(*sockets):
    queue = list(sockets)
    used = []

    def connect(url, **kwargs):
        socket = queue.pop(0) if queue else Socket([HELLO], hold=True)
        used.append((url, socket))
        return socket

    connect.used = used
    return connect


HELLO = {"type": "hello", "connection_info": {"app_id": "A1"}}
EVENT = {"type": "events_api", "envelope_id": "env-1", "payload": {"event": {"type": "message"}}}


def test_every_envelope_is_acked_and_events_are_dispatched():
    dispatched = []
    socket = Socket([HELLO, EVENT, {"type": "slash_commands", "envelope_id": "env-2"}], hold=True)
    conn = SocketModeConnection(integration(), "xoxb", "xapp", Api(), lambda *a: dispatched.append(a), connect=connector(socket))

    async def scenario():
        conn.start()
        await asyncio.sleep(0.05)
        state = conn.state
        await conn.stop()
        return state

    assert asyncio.run(scenario()) == "connected"
    assert socket.sent == [{"envelope_id": "env-1"}, {"envelope_id": "env-2"}]
    assert len(dispatched) == 1
    assert dispatched[0][1] == "xoxb" and dispatched[0][2] == EVENT["payload"]
    assert conn.last_event_at is not None
    assert conn.state == "stopped"


def test_disconnect_reconnects_at_once():
    api = Api()
    first = Socket([HELLO, {"type": "disconnect", "reason": "refresh_requested"}])
    second = Socket([HELLO], hold=True)
    conn = SocketModeConnection(integration(), "xoxb", "xapp", api, lambda *a: None, connect=connector(first, second))
    slept = []

    async def no_sleep(seconds):
        slept.append(seconds)

    conn._sleep = no_sleep

    async def scenario():
        conn.start()
        await asyncio.sleep(0.05)
        await conn.stop()

    asyncio.run(scenario())
    assert api.opened == 2
    assert slept == []


def test_a_dead_token_stops_retrying():
    api = Api(errors=[SlackApiError("apps.connections.open", "invalid_auth")])
    conn = SocketModeConnection(integration(), "xoxb", "xapp", api, lambda *a: None, connect=connector())
    asyncio.run(conn.run())
    assert conn.state == "auth_error" and conn.last_error == "invalid_auth"
    assert api.opened == 1


def test_transient_errors_back_off_and_retry():
    api = Api(errors=[SlackApiError("apps.connections.open", "network_error"), RuntimeError("boom")])
    conn = SocketModeConnection(integration(), "xoxb", "xapp", api, lambda *a: None, connect=connector(Socket([HELLO], hold=True)))
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    conn._sleep = fake_sleep

    async def scenario():
        conn.start()
        await asyncio.sleep(0.05)
        state = conn.state
        await conn.stop()
        return state

    assert asyncio.run(scenario()) == "connected"
    assert slept == [1.0, 2.0]


class Table:
    def __init__(self, rows):
        self.rows = rows

    def list_integrations(self):
        return [dict(r) for r in self.rows]


class Handler:
    def __init__(self):
        self.active_threads = {}
        self.calls = []

    async def handle(self, integration, token, payload):
        self.calls.append(integration["harness_id"])
        self.active_threads[f"t-{integration['harness_id']}"] = integration["harness_id"]
        try:
            await asyncio.sleep(3600)
        finally:
            self.active_threads.pop(f"t-{integration['harness_id']}", None)


class Broker:
    def __init__(self):
        self.cancelled = []

    def cancel(self, thread_id, owner_sub=None):
        self.cancelled.append(thread_id)
        return True


def manager(rows, load_tokens=None, connect=None, handler=None, broker=None):
    return ConnectionManager(
        repository=Table(rows),
        api=Api(),
        handler=handler or Handler(),
        load_tokens=load_tokens or (lambda arn: ("xoxb", "xapp")),
        run_broker=broker,
        connect=connect or connector(),
    )


def test_one_integrations_failure_does_not_touch_another():
    def load(arn):
        if arn == "arn:h2":
            raise RuntimeError("AccessDenied")
        return ("xoxb", "xapp")

    m = manager([integration("h1"), integration("h2", "i2")], load_tokens=load)

    async def scenario():
        await m.reconcile()
        await asyncio.sleep(0.05)
        states = (m.status("h1")["state"], m.status("h2")["state"])
        await m.stop_receiving()
        return states

    assert asyncio.run(scenario()) == ("connected", "secret_error")


def test_reconcile_follows_the_table():
    rows = [integration("h1")]
    m = manager(rows)

    async def scenario():
        await m.reconcile()
        assert set(m._connections) == {"h1"}
        rows.append(integration("h2", "i2"))
        rows[0]["status"] = "deleting"
        await m.reconcile()
        assert set(m._connections) == {"h2"}
        # Re-linked elsewhere: a new integration id replaces the connection.
        rows[1]["integration_id"] = "i3"
        await m.reconcile()
        assert m._connections["h2"].integration_id == "i3"
        await m.stop_receiving()

    asyncio.run(scenario())


def test_removing_an_integration_stops_its_turns_only():
    handler, broker = Handler(), Broker()
    m = manager([integration("h1"), integration("h2", "i2")], handler=handler, broker=broker)

    async def scenario():
        await m.reconcile()
        m._dispatch(integration("h1"), "xoxb", {})
        m._dispatch(integration("h2", "i2"), "xoxb", {})
        await asyncio.sleep(0.02)
        await m.remove("h1", drain_seconds=0.05)
        remaining = sorted(m._turns.values())
        await m.stop_receiving()
        await m.drain(timeout=0.05)
        return remaining

    assert asyncio.run(scenario()) == ["h2"]
    assert broker.cancelled == ["t-h1"]
    assert m.status("h1")["state"] == "disconnected"


def test_shutdown_stops_receiving_before_draining():
    handler = Handler()
    m = manager([integration("h1")], handler=handler)

    async def scenario():
        await m.start(reconcile_seconds=3600)
        m._dispatch(integration("h1"), "xoxb", {})
        await asyncio.sleep(0.02)
        await m.stop_receiving()
        assert m._connections == {}
        # A late reconcile or add after shutdown starts nothing.
        await m.add(integration("h9", "i9"))
        assert m._connections == {}
        assert len(m._turns) == 1
        await m.drain(timeout=0.05)
        return len(m._turns)

    assert asyncio.run(scenario()) == 0
    assert m.api.closed


def test_turns_beyond_the_limit_wait_their_turn():
    started = []

    class Slow:
        active_threads = {}

        async def handle(self, integration, token, payload):
            started.append(payload["n"])
            await asyncio.sleep(0.05)

    m = ConnectionManager(
        repository=Table([]), api=Api(), handler=Slow(), load_tokens=lambda arn: ("", ""), max_concurrent=2,
    )

    async def scenario():
        for n in range(4):
            m._dispatch(integration(), "xoxb", {"n": n})
        await asyncio.sleep(0.02)
        first = list(started)
        await asyncio.sleep(0.2)
        return first

    assert asyncio.run(scenario()) == [0, 1]
    assert started == [0, 1, 2, 3]
