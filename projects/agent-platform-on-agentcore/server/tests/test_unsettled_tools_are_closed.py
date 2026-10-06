"""A tool call with no result of its own must still be settled by the end of the turn.

Some tools structurally never report a result under their own name. In the deployed
ks_text2sql_agent (verified in its source):

  - `ask_sql_verified` stashes its adopted result as `ask_sql_specialist`
    (orchestration.py:313), because internally that is who produced it. A result
    labelled `ask_sql_verified` is never emitted at all.
  - `render_chart` stashes nothing into the tool-result bucket — it appends to the
    chart bucket instead, so its call frame is the only frame it ever produces.

The UI reads a tool call with no result as still running, so both left a spinner
turning: measured on a real multi-part question, three `ask_sql_verified` and two
`render_chart` blocks were opened and never settled.

This is fixed server-side rather than in that runtime because it is not that
runtime's problem alone — any runtime may emit a call without a matching result,
and the server is the one place that sees the turn end. `done`/`messageStop` is
where "no result is coming" becomes knowable, which is the same reasoning the
stored transcript already applies to tool-call status.

Deliberately settled with no `result` key: inventing one would claim the tool
returned something it never reported. The UI shows a completed call with no output,
which is exactly what happened.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.agentcore_client import AgentCoreClient  # noqa: E402


class FakeStream:
    def __init__(self, frames):
        self._frames = frames

    def iter_lines(self, chunk_size=1):
        for frame in self._frames:
            yield f"data: {frame}".encode("utf-8")


def _collect(frames):
    client = AgentCoreClient.__new__(AgentCoreClient)

    async def run():
        return [e async for e in client._process_sse_stream(FakeStream(frames))]

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


def _settled_ids(events):
    return [r["toolUseId"] for r in _results(events)]


# The real shape, from captured frames: the outer call is never named again, and
# the work comes back under the inner sub-agents' names.
VERIFIED_TURN = [
    '{"type": "tool_call", "tool": "ask_sql_verified", "args": {"question": "상위 3곳"}}',
    '{"type": "tool_result", "tool": "ask_validator", "result": "PASS"}',
    '{"type": "tool_result", "tool": "ask_sql_specialist", "result": "3 rows"}',
    '{"type": "tool_call", "tool": "render_chart", "args": {"kind": "bar"}}',
    '{"type": "text", "chunk": "상위 3개 업체입니다."}',
    '{"type": "done", "session_id": "s-1"}',
]


def test_no_spinner_survives_the_turn():
    """The regression: ask_sql_verified and render_chart spun forever."""
    events = _collect(VERIFIED_TURN)

    opened = _opened(events)
    settled = _settled_ids(events)
    unsettled = [(i, n) for i, n in opened if i not in settled]
    assert unsettled == [], (
        f"tool block(s) left running after the turn ended: {[n for _, n in unsettled]}"
    )


def test_the_closed_out_call_claims_no_output():
    """Settling must not fabricate a result the tool never reported."""
    events = _collect(VERIFIED_TURN)

    by_id = {i: n for i, n in _opened(events)}
    closed = [
        r for r in _results(events)
        if by_id.get(r["toolUseId"]) in ("ask_sql_verified", "render_chart")
    ]
    assert len(closed) == 2
    for r in closed:
        assert "result" not in r or r["result"] is None, (
            "a result was invented for a tool that never reported one"
        )


def test_a_real_result_is_untouched():
    """Only the unsettled calls are closed out; reported results keep their value."""
    events = _collect(VERIFIED_TURN)

    by_id = {i: n for i, n in _opened(events)}
    reported = {
        by_id[r["toolUseId"]]: r.get("result")
        for r in _results(events)
        if by_id.get(r["toolUseId"]) in ("ask_validator", "ask_sql_specialist")
    }
    assert reported == {"ask_validator": "PASS", "ask_sql_specialist": "3 rows"}


def test_the_close_out_precedes_message_stop():
    """The UI settles on messageStop, so anything after it arrives too late.

    A frame that lands after the message has been closed is not applied to it.
    """
    events = _collect(VERIFIED_TURN)

    kinds = [k for e in events for k in e.get("event", {})]
    last_result = max(i for i, k in enumerate(kinds) if k == "toolResult")
    stop = kinds.index("messageStop")
    assert last_result < stop


def test_several_unsettled_calls_are_all_closed():
    """A multi-part question opens several; none may be left behind."""
    events = _collect(
        [
            '{"type": "tool_call", "tool": "ask_sql_verified", "args": {"q": "a"}}',
            '{"type": "tool_call", "tool": "ask_sql_verified", "args": {"q": "b"}}',
            '{"type": "tool_call", "tool": "render_chart", "args": {"kind": "bar"}}',
            '{"type": "tool_call", "tool": "render_chart", "args": {"kind": "line"}}',
            '{"type": "text", "chunk": "두 차트입니다."}',
            '{"type": "done", "session_id": "s-2"}',
        ]
    )

    opened = _opened(events)
    assert len(opened) == 4
    assert sorted(_settled_ids(events)) == sorted(i for i, _ in opened)


def test_a_turn_that_ends_without_done_also_settles():
    """A truncated stream must not leave the spinners it opened.

    The synthetic messageStop already covers this case for text; the tool blocks
    have to ride the same path.
    """
    events = _collect(
        [
            '{"type": "tool_call", "tool": "ask_sql_verified", "args": {"q": "a"}}',
            '{"type": "text", "chunk": "부분 답변"}',
        ]
    )

    opened = _opened(events)
    assert len(opened) == 1
    assert _settled_ids(events) == [opened[0][0]]


def test_a_ping_settles_nothing_and_starts_nothing():
    """Prewarm sends only `done`; there is no turn to close out."""
    events = _collect(['{"type": "done", "session_id": "", "ping": true}'])

    assert _opened(events) == []
    assert _results(events) == []
    assert [k for e in events for k in e.get("event", {})] == []


def test_a_fully_settled_turn_gains_no_extra_results():
    """When every call reported, nothing is closed out — no duplicate results."""
    events = _collect(
        [
            '{"type": "tool_call", "tool": "ask_viz_specialist", "args": {}}',
            '{"type": "tool_result", "tool": "ask_viz_specialist", "result": "bar"}',
            '{"type": "text", "chunk": "차트입니다."}',
            '{"type": "done", "session_id": "s-3"}',
        ]
    )

    assert len(_results(events)) == 1
    assert _results(events)[0]["result"] == "bar"
