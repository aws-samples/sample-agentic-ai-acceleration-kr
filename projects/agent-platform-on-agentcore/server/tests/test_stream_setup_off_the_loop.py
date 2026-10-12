"""Setting a stream up must not hold the event loop either.

Moving the SSE read to a worker thread (test_stream_yields_event_loop) fixed the
long stalls, but a shorter one survived: 4.9s, measured once per turn just before
the first frame. `stream_thread_execution` is an `async def`, and before it hands
back a StreamingResponse it makes three blocking calls straight from the loop:

    self.thread_service.get_thread(...)          # DynamoDB
    self._require_approved(...)                  # GetRegistryRecord
    self.thread_service.get_or_create_thread(..) # DynamoDB read + write

They have to run before the response, and deliberately so: a ThreadForbidden or
ThreadAgentMismatch raised here becomes a 403/409, whereas the same failure inside
the generator would arrive as an error event on a 200 the client was already told
had started. That ordering is right — running them *on the loop* is not.

None is fast. GetRegistryRecord was measured at 35s on a cold client and ~0.25s
warm, and every one of those seconds is a second in which no other request in the
process is served. The routes next door are plain `def` precisely so FastAPI keeps
their blocking work off the loop (see routes/threads.py); the streaming routes are
`async def` because they genuinely await, so they must offload by hand.

Asserted by thread identity rather than by elapsed time: what makes this correct
is *where* the call runs, and a timing threshold would be flaky in CI besides.
"""
import asyncio
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.thread import Thread  # noqa: E402
from services.run_broker import RunBroker  # noqa: E402
from services.streaming_service import StreamingService  # noqa: E402


class RecordingThreadService:
    """Notes which thread each blocking call ran on."""

    def __init__(self):
        self.threads_used: dict[str, threading.Thread] = {}
        self.repository = self

    def _note(self, name):
        self.threads_used[name] = threading.current_thread()

    def get_thread(self, thread_id):
        self._note("get_thread")
        return None

    def get_or_create_thread(self, thread_id, owner_sub="", initial_values=None,
                             agent_record_id="", agent_name="", **_kwargs):
        self._note("get_or_create_thread")
        stamp = "2026-08-09T00:00:00"
        thread = Thread(
            thread_id=thread_id,
            owner_sub=owner_sub,
            created_at=stamp,
            updated_at=stamp,
        )
        thread.values = {"messages": []}
        thread.agent_record_id = agent_record_id
        return thread

    def update(self, thread_id, thread):
        return thread


class RecordingRegistry:
    def __init__(self):
        self.thread_used = None

    def get_record(self, record_id):
        self.thread_used = threading.current_thread()

        class Record:
            status = "APPROVED"

        return Record()
    def chattable_record(self, record_id):
        # The approval gate reads the chattable revision; these doubles have one.
        return self.get_record(record_id)


class StubAgentClient:
    """Yields nothing: the setup calls are what is under test, not the stream."""

    async def execute_stream(self, thread_id, values, config=None, actor_id=None):
        if False:
            yield {}


class StubRequest:
    """Minimal stand-in for StreamRequest."""

    class Config:
        registry_record_id = "rec-1"
        registry_agent_name = "ks_text2sql_agent"
        agent_runtime_arn = "arn:aws:bedrock-agentcore:us-east-1:1:runtime/r-1"
        qualifier = "DEFAULT"

        def dict(self, exclude_none=False):
            return {
                "registry_record_id": self.registry_record_id,
                "registry_agent_name": self.registry_agent_name,
                "agent_runtime_arn": self.agent_runtime_arn,
                "qualifier": self.qualifier,
            }

    def __init__(self):
        self.config = self.Config()
        self.values = {"messages": [{"id": "u1", "type": "human", "content": "안녕"}]}


def _service(thread_service, registry):
    service = StreamingService.__new__(StreamingService)
    service.thread_service = thread_service
    service.agentcore_client = StubAgentClient()
    service.artifact_service = None
    service.mcp_apps_relay = None
    service.registry_service = registry
    service.run_broker = RunBroker()
    return service


@pytest.fixture
def wiring():
    thread_service = RecordingThreadService()
    registry = RecordingRegistry()
    service = _service(thread_service, registry)
    return service, thread_service, registry


def _run_setup(service):
    """Call the entry point and report the loop's own thread."""

    async def run():
        loop_thread = threading.current_thread()
        response = await service.stream_thread_execution(
            "t-1", StubRequest(), actor_id="actor-1", owner_sub="sub-1"
        )
        return loop_thread, response

    return asyncio.run(run())


def test_thread_lookup_runs_off_the_loop(wiring):
    """The pre-stream DynamoDB read must not block the loop."""
    service, thread_service, _ = wiring
    loop_thread, _ = _run_setup(service)

    used = thread_service.threads_used.get("get_thread")
    assert used is not None, "get_thread was never called"
    assert used is not loop_thread, "DynamoDB read ran on the event loop's thread"


def test_approval_check_runs_off_the_loop(wiring):
    """GetRegistryRecord takes up to 35s cold; it cannot own the loop.

    This is the call that produced the 4.9s stall still visible after the SSE
    read was moved off the loop.
    """
    service, _, registry = wiring
    loop_thread, _ = _run_setup(service)

    assert registry.thread_used is not None, "the approval gate never ran"
    assert registry.thread_used is not loop_thread, (
        "GetRegistryRecord ran on the event loop's thread"
    )


def test_thread_creation_runs_off_the_loop(wiring):
    """The DynamoDB read+write that pins the agent must be offloaded too."""
    service, thread_service, _ = wiring
    loop_thread, _ = _run_setup(service)

    used = thread_service.threads_used.get("get_or_create_thread")
    assert used is not None, "get_or_create_thread was never called"
    assert used is not loop_thread, "thread creation ran on the event loop's thread"


def test_setup_failures_still_reach_the_route(wiring):
    """Offloading must not swallow the exceptions the route turns into 403/409.

    The whole reason these calls precede the response is that their failures have
    to become status codes rather than error events on a 200. Running them in a
    worker thread must re-raise into the caller, not strand the exception there.
    """
    from services.streaming_service import AgentNotApproved

    service, _, _ = wiring

    class RejectingRegistry:
        def get_record(self, record_id):
            class Record:
                status = "DRAFT"

            return Record()
        def chattable_record(self, record_id):
            # The approval gate reads the chattable revision; these doubles have one.
            return self.get_record(record_id)

    service.registry_service = RejectingRegistry()

    with pytest.raises(AgentNotApproved):
        _run_setup(service)


def test_the_loop_keeps_running_during_setup():
    """A slow setup call must not stop other requests being served.

    The property, stated directly: a probe coroutine has to keep getting turns
    while the registry call is in flight.
    """
    GAP = 0.4
    INTERVAL = 0.01

    class SlowRegistry:
        def get_record(self, record_id):
            import time

            time.sleep(GAP)

            class Record:
                status = "APPROVED"

            return Record()
        def chattable_record(self, record_id):
            # The approval gate reads the chattable revision; these doubles have one.
            return self.get_record(record_id)

    service = _service(RecordingThreadService(), SlowRegistry())

    async def run():
        ticks = 0
        stop = asyncio.Event()

        async def probe():
            nonlocal ticks
            while not stop.is_set():
                await asyncio.sleep(INTERVAL)
                ticks += 1

        task = asyncio.create_task(probe())
        try:
            await service.stream_thread_execution(
                "t-1", StubRequest(), actor_id="a", owner_sub="s"
            )
        finally:
            stop.set()
            await task
        return ticks

    ticks = asyncio.run(run())
    expected = GAP / INTERVAL
    assert ticks >= expected / 2, (
        f"loop stalled during stream setup: probe ticked {ticks} times, "
        f"expected at least {expected / 2:.0f}"
    )
