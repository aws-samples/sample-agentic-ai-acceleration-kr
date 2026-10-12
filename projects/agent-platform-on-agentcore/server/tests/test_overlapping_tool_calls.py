"""Two calls to one tool, both open at once, must both settle.

`tool_use_ids` maps a tool *name* to the id of its open block, so the second
`tool_call` for a name overwrote the first. Once overwritten, nothing could ever
settle the first block: the UI treats a tool call with no result as still running,
so its spinner turned for the rest of the turn.

The existing coverage missed this because it only exercised the *serial* shape —
call, result, call, result — where the first entry is popped before the second is
written. An orchestrator that opens two `ask_sql_verified` calls before either
returns hits the overlapping shape, and that is what a multi-part question
produces ("상위 3곳과 월별 추이를 각각").

It compounds: the orphan branch below opens a *new* block for a result whose id it
cannot find, so one overlapping pair leaves one spinner turning and creates one
extra tool box that was never called.

A queue per name fixes it — results settle the oldest open call of that name, which
is the order the runtime flushes them in. Not a perfect pairing (the frames carry
no id to match on), but it is stable, and every block ends up settled.
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
    """Tool blocks the UI was told about, in order."""
    ids = []
    for event in events:
        start = event.get("event", {}).get("contentBlockStart")
        if start and "toolUse" in start.get("start", {}):
            ids.append(start["start"]["toolUse"]["toolUseId"])
    return ids


def _settled(events):
    return [
        r["toolUseId"] for r in
        (e["event"]["toolResult"] for e in events if "toolResult" in e.get("event", {}))
    ]


# Both calls open before either returns — the shape a multi-part question produces.
OVERLAPPING = [
    '{"type": "tool_call", "tool": "ask_sql_verified", "args": {"question": "상위 3곳"}}',
    '{"type": "tool_call", "tool": "ask_sql_verified", "args": {"question": "월별 추이"}}',
    '{"type": "tool_result", "tool": "ask_sql_verified", "result": "3 rows"}',
    '{"type": "tool_result", "tool": "ask_sql_verified", "result": "6 rows"}',
    '{"type": "text", "chunk": "두 결과입니다."}',
    '{"type": "done", "session_id": "s-1"}',
]


def test_no_tool_block_is_left_unsettled():
    """The regression: the first of two overlapping calls spun forever."""
    events = _collect(OVERLAPPING)

    opened, settled = _opened(events), _settled(events)
    unsettled = [i for i in opened if i not in settled]
    assert unsettled == [], (
        f"{len(unsettled)} tool block(s) left without a result — a spinner that "
        f"never stops: {unsettled}"
    )


def test_no_phantom_tool_box_is_invented():
    """A result whose id was lost used to open a block of its own.

    So one overlapping pair produced three tool boxes for two calls — one of them
    a call the agent never made.
    """
    events = _collect(OVERLAPPING)

    assert len(_opened(events)) == 2, (
        "a tool box was opened for a call that never happened"
    )


def test_both_results_still_arrive():
    """Fixing the correlation must not drop a result."""
    events = _collect(OVERLAPPING)

    results = [
        e["event"]["toolResult"]["result"]
        for e in events
        if "toolResult" in e.get("event", {})
    ]
    assert results == ["3 rows", "6 rows"]


def test_overlapping_calls_keep_distinct_ids():
    """Two calls are two calls; sharing an id would settle the wrong box."""
    events = _collect(OVERLAPPING)

    opened = _opened(events)
    assert len(set(opened)) == len(opened)
    assert len(set(_settled(events))) == 2


def test_results_settle_in_the_order_the_calls_opened():
    """The frames carry no id, so oldest-open is the only stable pairing.

    It also matches how the runtime flushes: results are stashed in completion
    order and drained in that order.
    """
    events = _collect(OVERLAPPING)

    opened, settled = _opened(events), _settled(events)
    assert settled == opened, "results were paired against the wrong calls"


def test_three_overlapping_calls_all_settle():
    """Nothing about the fix may depend on there being exactly two."""
    events = _collect(
        [
            '{"type": "tool_call", "tool": "ask_sql_verified", "args": {"q": "a"}}',
            '{"type": "tool_call", "tool": "ask_sql_verified", "args": {"q": "b"}}',
            '{"type": "tool_call", "tool": "ask_sql_verified", "args": {"q": "c"}}',
            '{"type": "tool_result", "tool": "ask_sql_verified", "result": "a"}',
            '{"type": "tool_result", "tool": "ask_sql_verified", "result": "b"}',
            '{"type": "tool_result", "tool": "ask_sql_verified", "result": "c"}',
            '{"type": "done", "session_id": "s-2"}',
        ]
    )

    assert len(_opened(events)) == 3
    assert _settled(events) == _opened(events)


def test_the_serial_shape_still_works():
    """The case that already worked: call, result, call, result."""
    events = _collect(
        [
            '{"type": "tool_call", "tool": "ask_sql_specialist", "args": {"q": "a"}}',
            '{"type": "tool_result", "tool": "ask_sql_specialist", "result": "first"}',
            '{"type": "tool_call", "tool": "ask_sql_specialist", "args": {"q": "b"}}',
            '{"type": "tool_result", "tool": "ask_sql_specialist", "result": "second"}',
            '{"type": "done", "session_id": "s-3"}',
        ]
    )

    results = [
        e["event"]["toolResult"]["result"]
        for e in events
        if "toolResult" in e.get("event", {})
    ]
    assert results == ["first", "second"]
    assert _settled(events) == _opened(events)


def test_a_sub_agent_result_still_opens_its_own_block():
    """The orphan branch must survive — it is load-bearing for the real runtime.

    `ask_sql_verified` calls `ask_sql_specialist` and `ask_validator` internally,
    and the runtime flushes their stashed results under those inner names. The
    orchestrator stream never showed a `tool_call` for either, so the only way to
    show that work is to open a block for it. Captured frames from the deployed
    ks_text2sql_agent have exactly this shape.
    """
    events = _collect(
        [
            '{"type": "tool_call", "tool": "ask_sql_verified", "args": {}}',
            '{"type": "tool_result", "tool": "ask_validator", "result": "PASS"}',
            '{"type": "tool_result", "tool": "ask_sql_specialist", "result": "3 rows"}',
            '{"type": "done", "session_id": "s-4"}',
        ]
    )

    names = [
        s["start"]["toolUse"]["name"]
        for e in events
        if (s := e.get("event", {}).get("contentBlockStart"))
        and "toolUse" in s.get("start", {})
    ]
    assert names == ["ask_sql_verified", "ask_validator", "ask_sql_specialist"]
    # Every inner result is shown. The outer call has no result frame of its own, so
    # it is closed out at the end of the turn rather than left spinning — see
    # test_unsettled_tools_are_closed.
    assert len(_settled(events)) == 3


def test_a_call_with_no_result_at_all_is_closed_out():
    """`render_chart` sends a call and no result (measured).

    Nothing during the turn can settle it, so it is closed out when the turn ends —
    with no `result`, since the tool never reported one. See
    test_unsettled_tools_are_closed for the full behaviour.
    """
    events = _collect(
        [
            '{"type": "tool_call", "tool": "render_chart", "args": {"kind": "bar"}}',
            '{"type": "text", "chunk": "차트입니다."}',
            '{"type": "done", "session_id": "s-5"}',
        ]
    )

    opened = _opened(events)
    assert len(opened) == 1
    assert _settled(events) == opened
