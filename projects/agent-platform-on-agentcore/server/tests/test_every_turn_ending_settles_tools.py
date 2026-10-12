"""Every way a turn can end must settle the tool blocks it opened.

`done` and a stream that simply runs out were already covered
(test_unsettled_tools_are_closed.py). They are not the only endings, and the three
that were missed all leave `ask_sql_specialist` — the longest-running tool in the
deployed ks_text2sql_agent — showing a spinner:

  - `final`, which some runtimes send instead of `done`.
  - A native Strands `messageStop` passed straight through.
  - A **failure mid-turn**: a reset connection, or a read timeout when the runtime
    stops heartbeating. This is the one that is actually unrecoverable in the UI.
    The other two emit a `messageStop`, and the browser settles pending calls when
    it applies one, so the spinner stops there by luck rather than by design. The
    error path emits no `messageStop` at all, so nothing anywhere ever settles the
    block: the spinner turns until the page is reloaded.

Measured on the deployed runtime: `sql_specialist` blocks stay legitimately pending
for 13-102s, since a parallel call waits for the earlier one to finish. So a turning
spinner is normal for a minute or more, and "still running" is indistinguishable
from "abandoned" without an explicit settle.

The drain has to be common to every ending rather than attached to `done`, because
which ending arrives is not something the server chooses.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.agentcore_client import AgentCoreClient  # noqa: E402


class FakeStream:
    """Frames, optionally cut short by a failure part-way through.

    `raise_at` reproduces the read error botocore raises when the stream dies
    mid-turn — the case the `except` clause handles.
    """

    def __init__(self, frames, raise_at=None):
        self._frames = frames
        self._raise_at = raise_at

    def iter_lines(self, chunk_size=1):
        for i, frame in enumerate(self._frames):
            if self._raise_at is not None and i == self._raise_at:
                raise OSError("Connection reset by peer")
            yield f"data: {frame}".encode("utf-8")


def _collect(frames, raise_at=None):
    client = AgentCoreClient.__new__(AgentCoreClient)

    async def run():
        stream = FakeStream(frames, raise_at)
        return [e async for e in client._process_sse_stream(stream)]

    return asyncio.run(run())


def _opened(events):
    out = []
    for event in events:
        start = event.get("event", {}).get("contentBlockStart")
        if start and "toolUse" in start.get("start", {}):
            tu = start["start"]["toolUse"]
            out.append((tu["toolUseId"], tu["name"]))
    return out


def _results(events):
    return [
        e["event"]["toolResult"]
        for e in events
        if "toolResult" in e.get("event", {})
    ]


def _kinds(events):
    return [k for e in events for k in e.get("event", {})]


def _unsettled(events):
    settled = {r["toolUseId"] for r in _results(events)}
    return [name for tool_id, name in _opened(events) if tool_id not in settled]


# A long specialist call in flight when the turn ends. Only the ending differs
# between the cases below.
IN_FLIGHT = [
    '{"type": "text", "chunk": "조회하겠습니다."}',
    '{"type": "tool_call", "tool": "ask_sql_specialist", "args": {"q": "매출"}}',
]


def test_final_settles_the_open_call():
    """A runtime that ends with `final` instead of `done`."""
    events = _collect(
        IN_FLIGHT + ['{"type": "final", "response": "완료", "usage": {}}']
    )

    assert _unsettled(events) == []


def test_a_native_message_stop_settles_the_open_call():
    """A Strands-native stop is forwarded as-is; the drain must still run."""
    events = _collect(
        IN_FLIGHT
        + ['{"event": {"messageStop": {"stopReason": "end_turn"}}}']
    )

    assert _unsettled(events) == []


def test_a_stream_that_fails_mid_turn_settles_the_open_call():
    """The regression: a dead connection left the spinner turning forever.

    No `messageStop` is emitted on this path, so the browser's own fallback never
    fires either — this is the one ending with no second line of defence.
    """
    events = _collect(IN_FLIGHT + ['{"type": "text", "chunk": "더"}'], raise_at=2)

    assert _unsettled(events) == []


def test_the_failed_turn_still_reports_the_error():
    """Settling the spinner must not swallow the failure."""
    events = _collect(IN_FLIGHT + ['{"type": "text", "chunk": "더"}'], raise_at=2)

    assert "error" in _kinds(events)


def test_the_failed_turn_closes_its_message():
    """A message left open is never persisted, and the UI keeps it streaming.

    The error alone does not close it: the browser applies `messageStop` to finish
    the turn, so a failure without one leaves a half-open assistant message.
    """
    events = _collect(IN_FLIGHT + ['{"type": "text", "chunk": "더"}'], raise_at=2)

    assert "messageStop" in _kinds(events)


def test_settling_precedes_the_stop_on_every_ending():
    """The UI closes the message on `messageStop`; later frames miss it."""
    for ending, raise_at in (
        (['{"type": "final", "response": "완료", "usage": {}}'], None),
        (['{"event": {"messageStop": {"stopReason": "end_turn"}}}'], None),
        (['{"type": "text", "chunk": "더"}'], 2),
    ):
        events = _collect(IN_FLIGHT + ending, raise_at=raise_at)
        kinds = _kinds(events)
        assert "toolResult" in kinds, f"nothing settled for ending {ending}"
        assert max(i for i, k in enumerate(kinds) if k == "toolResult") < kinds.index(
            "messageStop"
        ), f"settled too late for ending {ending}"


def test_a_failure_before_anything_started_opens_no_message():
    """A turn that produced nothing must not gain an empty bubble."""
    events = _collect(['{"type": "text", "chunk": "hi"}'], raise_at=0)

    assert _opened(events) == []
    assert "messageStop" not in _kinds(events)
    assert "error" in _kinds(events)


def test_no_ending_settles_a_call_twice():
    """A reported result is final; no ending may append a second one."""
    for ending, raise_at in (
        (['{"type": "done", "session_id": "s"}'], None),
        (['{"type": "final", "response": "완료", "usage": {}}'], None),
        (['{"event": {"messageStop": {"stopReason": "end_turn"}}}'], None),
        (['{"type": "text", "chunk": "더"}'], 3),
    ):
        events = _collect(
            [
                '{"type": "tool_call", "tool": "ask_sql_specialist", "args": {}}',
                '{"type": "tool_result", "tool": "ask_sql_specialist", "result": "5 rows"}',
                '{"type": "text", "chunk": "결과입니다."}',
            ]
            + ending,
            raise_at=raise_at,
        )
        results = _results(events)
        assert len(results) == 1, f"duplicate settle for ending {ending}: {results}"
        assert results[0]["result"] == "5 rows"
