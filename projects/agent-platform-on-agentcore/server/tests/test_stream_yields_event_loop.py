"""A silent stream must not hold the event loop.

An agents-as-tools runtime emits nothing while a sub-agent runs: `@tool` wrappers
are blocking, so a step like `ask_sql_verified` — which generates and executes
several candidate SQL statements — goes 40-70s without a single frame. That
silence is normal and is not the runtime's bug to fix.

What is a bug is what the *server* does during it. Both stream clients iterate a
blocking botocore body from inside an `async` generator:

    for line in response_stream.iter_lines(chunk_size=1):
        await asyncio.sleep(0)  # Yield to event loop

The `await` runs only *after* a line has arrived, so it yields nothing during the
gap between lines. For that whole gap the coroutine sits inside a blocking socket
read while owning the event loop, and every other request in the process stops —
not just this stream's. Measured against the deployed ks_text2sql_agent: a 68s
silent gap made `GET /` (an `async def` returning a literal dict, touching no
boto3, no DynamoDB and no threadpool) take 67.3s, against 1ms idle. Neither
threadpool nor connection-pool exhaustion can explain that: `GET /` uses neither.

These tests assert the property directly rather than timing it — a wall-clock
threshold would be flaky in CI and would not say *why* it passed. A concurrent
"probe" coroutine counts its own ticks while the stream sits in its silent gap.
On the blocking implementation the probe cannot tick at all; once the read is
handed to a worker thread it ticks freely.
"""
import asyncio
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.agentcore_client import AgentCoreClient  # noqa: E402
from agents.harness_client import HarnessClient  # noqa: E402


# Long enough that a blocked loop is unambiguous, short enough to keep the suite
# quick. The real gap is 40-70s; the failure mode does not depend on the length.
GAP_SECONDS = 0.5
# How long the probe is given to tick. Generous relative to its interval so a
# slow machine cannot fail the test on scheduling jitter alone.
PROBE_INTERVAL = 0.01


class SilentThenSpeakingStream:
    """A botocore streaming body that blocks the calling thread mid-stream.

    `time.sleep` in `iter_lines` is exactly what botocore does while waiting on
    the socket: it holds whichever thread iterates it. Which thread that is, is
    the entire question under test.
    """

    def __init__(self, frames, gap_before_index=1, gap=GAP_SECONDS):
        self._frames = frames
        self._gap_before_index = gap_before_index
        self._gap = gap
        # The thread that actually performed the blocking wait, so a test can
        # assert the read was moved off the loop rather than merely appearing to
        # be. Set on the first blocking iteration.
        self.blocked_on_thread = None

    def _iterate(self):
        for i, frame in enumerate(self._frames):
            if i == self._gap_before_index:
                self.blocked_on_thread = threading.current_thread()
                # Deliberately the blocking sleep, not asyncio.sleep: the point
                # is that a plain blocking wait must not stall the loop.
                import time

                time.sleep(self._gap)
            yield f"data: {frame}".encode("utf-8")

    def iter_lines(self, chunk_size=1):
        return self._iterate()


class SilentThenSpeakingHarnessStream:
    """The harness equivalent: `response["stream"]` is a blocking iterable."""

    def __init__(self, events, gap_before_index=1, gap=GAP_SECONDS):
        self._events = events
        self._gap_before_index = gap_before_index
        self._gap = gap
        self.blocked_on_thread = None

    def __iter__(self):
        for i, event in enumerate(self._events):
            if i == self._gap_before_index:
                self.blocked_on_thread = threading.current_thread()
                import time

                time.sleep(self._gap)
            yield event


TYPED_FRAMES = [
    '{"type": "thinking", "text": "분석 중…"}',
    # The gap falls here — where ask_sql_verified runs its candidates.
    '{"type": "tool_call", "tool": "ask_sql_verified", "args": {}}',
    '{"type": "tool_result", "tool": "ask_sql_verified", "result": "3 rows"}',
    '{"type": "text", "chunk": "설비 3건입니다."}',
    '{"type": "done", "session_id": "s-1"}',
]


async def _drain_while_probing(agen):
    """Consume `agen` while a probe coroutine counts loop turns.

    The probe is what a *different user's* request amounts to: an ordinary
    coroutine wanting the loop. Its tick count during the stream is the measure.
    """
    ticks = 0
    stop = asyncio.Event()

    async def probe():
        nonlocal ticks
        while not stop.is_set():
            await asyncio.sleep(PROBE_INTERVAL)
            ticks += 1

    probe_task = asyncio.create_task(probe())
    try:
        events = [event async for event in agen]
    finally:
        stop.set()
        await probe_task
    return events, ticks


def test_runtime_stream_lets_other_requests_run_during_a_silent_gap():
    """The loop must keep serving while a sub-agent step emits nothing.

    Fails on the blocking implementation: the probe gets no turn for the whole
    gap, so it ticks ~0 times. With the read on a worker thread it ticks roughly
    gap/interval times.
    """
    client = AgentCoreClient.__new__(AgentCoreClient)
    stream = SilentThenSpeakingStream(TYPED_FRAMES)

    events, ticks = asyncio.run(
        _drain_while_probing(client._process_sse_stream(stream))
    )

    # Half the theoretical maximum: enough to prove the loop ran throughout,
    # loose enough to survive scheduling jitter on a busy machine.
    expected = GAP_SECONDS / PROBE_INTERVAL
    assert ticks >= expected / 2, (
        f"event loop stalled during the silent gap: probe ticked {ticks} times, "
        f"expected at least {expected / 2:.0f}"
    )


def test_runtime_stream_blocking_read_happens_off_the_loop_thread():
    """The blocking wait must occur on a worker, not on the loop's own thread.

    Complements the tick count: a generous timing threshold could in principle
    be met by luck, whereas the thread identity is categorical.
    """
    client = AgentCoreClient.__new__(AgentCoreClient)
    stream = SilentThenSpeakingStream(TYPED_FRAMES)

    async def run():
        loop_thread = threading.current_thread()
        async for _ in client._process_sse_stream(stream):
            pass
        return loop_thread

    loop_thread = asyncio.run(run())

    assert stream.blocked_on_thread is not None, "the stub never blocked"
    assert stream.blocked_on_thread is not loop_thread, (
        "the blocking read ran on the event loop's thread"
    )


def test_runtime_stream_still_delivers_every_frame_in_order():
    """Moving the read off the loop must not drop or reorder frames.

    The whole point of the change is invisible to the client, so the existing
    contract is asserted alongside it.
    """
    client = AgentCoreClient.__new__(AgentCoreClient)
    stream = SilentThenSpeakingStream(TYPED_FRAMES)

    async def run():
        return [event async for event in client._process_sse_stream(stream)]

    events = asyncio.run(run())
    kinds = [k for e in events for k in e.get("event", {})]

    assert kinds.count("messageStart") == 1
    assert kinds.count("messageStop") == 1
    deltas = [
        d["delta"]["text"]
        for e in events
        if (d := e.get("event", {}).get("contentBlockDelta"))
        and "text" in d.get("delta", {})
    ]
    assert "".join(deltas) == "설비 3건입니다."
    # Status precedes the answer, and the tool call precedes its result.
    assert kinds.index("agentStatus") < kinds.index("contentBlockDelta")
    assert kinds.index("contentBlockStart") < kinds.index("toolResult")


HARNESS_EVENTS = [
    {"messageStart": {"role": "assistant"}},
    # The gap: a harness is silent while its AWS-side tools run.
    {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "완료"}}},
    {"messageStop": {"stopReason": "end_turn"}},
]


def _harness_client(stream):
    client = HarnessClient.__new__(HarnessClient)
    client.harness_arn = "arn:aws:bedrock-agentcore:us-east-1:1:harness/h-1"
    client.qualifier = "DEFAULT"
    client.region_name = "us-east-1"

    class FakeControl:
        def invoke_harness(self, **kwargs):
            return {"stream": stream}

    client.agentcore_client = FakeControl()
    return client


def test_harness_stream_lets_other_requests_run_during_a_silent_gap():
    """Same defect, same fix, in the harness path.

    A harness pauses while its tools run inside AWS, so it has the identical
    silent gap — and iterates its blocking `response["stream"]` the same way.
    """
    stream = SilentThenSpeakingHarnessStream(HARNESS_EVENTS)
    client = _harness_client(stream)

    values = {"messages": [{"id": "u1", "type": "human", "content": "안녕"}]}
    events, ticks = asyncio.run(
        _drain_while_probing(client.execute_stream("t-1", values))
    )

    expected = GAP_SECONDS / PROBE_INTERVAL
    assert ticks >= expected / 2, (
        f"event loop stalled during the harness gap: probe ticked {ticks} times, "
        f"expected at least {expected / 2:.0f}"
    )
    # The turn still completed, so the tick count is not the result of an early
    # abort that skipped the gap entirely.
    assert any("end" in e.get("event", {}) for e in events)


def test_harness_stream_blocking_read_happens_off_the_loop_thread():
    stream = SilentThenSpeakingHarnessStream(HARNESS_EVENTS)
    client = _harness_client(stream)
    values = {"messages": [{"id": "u1", "type": "human", "content": "안녕"}]}

    async def run():
        loop_thread = threading.current_thread()
        async for _ in client.execute_stream("t-1", values):
            pass
        return loop_thread

    loop_thread = asyncio.run(run())

    assert stream.blocked_on_thread is not None, "the stub never blocked"
    assert stream.blocked_on_thread is not loop_thread, (
        "the harness blocking read ran on the event loop's thread"
    )
