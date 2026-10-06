"""
A silent stream must still put bytes on the wire.

A harness runs its tools inside AWS, so the server sees nothing at all while one
runs — `builtin-tools___execute_command` generating a .docx is minutes of silence.
Nothing between here and the browser tolerates that:

- Next.js proxies `/threads/*` to this server (`web/src/middleware.ts`), and its
  proxy sets `proxyTimeout` to 30s by default, applied as an *idle* timeout on the
  upstream socket (`v.setTimeout(30000, () => v.abort())` in `http-proxy`).
  Measured: a 25s gap survives, a 40s gap aborts the upstream request and the
  origin's next write fails with BrokenPipeError.
- The ALB in front of the deployed stack has its own 60s idle timeout.

Either one severs a turn that is going perfectly well, and the server can only read
that as the client disconnecting — so it writes `interrupted` and the run is over.
An SSE comment frame costs nothing and resets both clocks.

Comments, not events: `:` lines are ignored by every SSE parser, and useStream only
looks at lines starting with `data: `. A heartbeat must not be able to reach the
transcript.
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import services.streaming_service as streaming_service  # noqa: E402
from models.common import StreamRequest  # noqa: E402
from services.streaming_service import StreamingService  # noqa: E402

VALUES = {"messages": [{"id": "u-1", "type": "human", "content": "hello"}]}

# Short enough to keep the test quick, long enough that the events below are not
# themselves mistaken for gaps.
INTERVAL = 0.05
GAP = 0.4


class StubThread:
    def __init__(self):
        self.values = dict(VALUES)
        self.updated_at = ""


class StubRepo:
    def update(self, thread_id, thread):
        return thread


class StubThreadService:
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


class SilentToolClient:
    """One token, then a long silent tool run, then the turn ends."""

    async def execute_stream(self, thread_id, values, config=None, actor_id=None):
        yield {"event": {"contentBlockDelta": {"delta": {"text": "working"}}}}
        await asyncio.sleep(GAP)
        yield {"event": {"messageStop": {"messageId": "m-1", "fullText": "working"}}}


class BusyClient:
    """Never goes quiet, so it must never be padded with heartbeats."""

    async def execute_stream(self, thread_id, values, config=None, actor_id=None):
        for i in range(5):
            yield {"event": {"contentBlockDelta": {"delta": {"text": str(i)}}}}
        yield {"event": {"messageStop": {"messageId": "m-1", "fullText": "01234"}}}


class ExplodingClient:
    async def execute_stream(self, thread_id, values, config=None, actor_id=None):
        raise RuntimeError("bedrock is unhappy")
        yield  # pragma: no cover — makes this an async generator


@pytest.fixture(autouse=True)
def quick_heartbeat(monkeypatch):
    monkeypatch.setattr(
        streaming_service, "HEARTBEAT_INTERVAL_SECONDS", INTERVAL, raising=False
    )


def collect(client):
    threads = StubThreadService()
    service = StreamingService(thread_service=threads, agentcore_client=client)
    service._get_agent_client = lambda config=None: client

    async def scenario():
        response = await service.stream_thread_execution(
            "t-1", StreamRequest(values=VALUES)
        )
        return [chunk async for chunk in response.body_iterator]

    return asyncio.run(scenario())


def heartbeats(frames):
    return [f for f in frames if f.startswith(":")]


def test_a_silent_tool_run_is_padded_with_heartbeats():
    """Without these the proxy sees an idle socket and aborts a healthy turn."""
    frames = collect(SilentToolClient())

    assert heartbeats(frames), (
        f"a {GAP}s gap produced no keep-alive frame; the upstream socket sat idle"
    )


def test_heartbeats_keep_coming_for_as_long_as_the_gap_lasts():
    """One frame would reset the clock once and then let it run out again."""
    frames = collect(SilentToolClient())

    # Conservative: the gap fits ~8 intervals, so anything less than 2 means the
    # padding is not repeating.
    assert len(heartbeats(frames)) >= 2


def test_a_heartbeat_is_an_sse_comment_not_an_event():
    """useStream reads `data: ` lines; a heartbeat must be invisible to it."""
    for frame in heartbeats(collect(SilentToolClient())):
        assert frame.startswith(": ")
        assert "data:" not in frame
        assert frame.endswith("\n\n")


def test_the_real_events_still_arrive_in_order():
    frames = collect(SilentToolClient())
    payloads = [f for f in frames if f.startswith("data: ")]

    assert "thread_created" in payloads[0]
    assert "contentBlockDelta" in payloads[1]
    assert "messageStop" in payloads[2]
    assert '"event": "end"' in payloads[-1]


def test_a_busy_stream_is_not_padded():
    """Heartbeats are for silence; on a talking stream they are pure noise."""
    assert heartbeats(collect(BusyClient())) == []


def test_a_failing_stream_still_reports_its_error():
    """The heartbeat wrapper must not swallow what the source raises."""
    frames = collect(ExplodingClient())

    assert any("bedrock is unhappy" in f for f in frames)
