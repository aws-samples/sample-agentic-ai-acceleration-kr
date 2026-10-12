"""A runtime that streams its own `{"type": ...}` vocabulary must still reach the UI.

`_process_sse_stream` recognised two shapes: Strands-native `{"event": {...}}` and
a simplified `{"type": "text_delta", "delta": ...}`. A runtime built on
`BedrockAgentCoreApp` with an async-generator entrypoint yields whatever dicts it
likes, and the SDK frames each as one `data:` line — so a runtime emitting
`{"type": "text", "chunk": "..."}` fell through to the `else` branch and was
logged at debug and dropped.

Every frame being dropped is worse than an error: `started` stays False, so no
`messageStart` and no synthetic `messageStop` are emitted either. The stream ends
`completed` with zero content, which the UI renders as a silent no-op — no answer
and no error, even though the runtime answered correctly.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.agentcore_client import AgentCoreClient  # noqa: E402


class FakeStream:
    """Stands in for the botocore streaming body, which yields raw SSE lines."""

    def __init__(self, frames):
        self._frames = frames

    def iter_lines(self, chunk_size=1):
        for frame in self._frames:
            yield f"data: {frame}".encode("utf-8")


# Verbatim frame shapes captured from the deployed ks_text2sql_agent runtime.
TYPED_TURN = [
    '{"type": "thinking", "text": "분석 중…"}',
    '{"type": "heartbeat", "phase": "think", "label": "질문 분석 중", "elapsed_ms": 2000}',
    '{"type": "reasoning", "chunk": "user wants the equipment list"}',
    '{"type": "tool_call", "tool": "ask_sql_specialist", "args": {"question": "list equipment"}}',
    '{"type": "tool_result", "tool": "ask_sql_specialist", "result": "3 rows"}',
    '{"type": "text", "chunk": "설비 "}',
    '{"type": "text", "chunk": "3건입니다."}',
    '{"type": "done", "session_id": "s-1"}',
]


def _collect(frames):
    client = AgentCoreClient.__new__(AgentCoreClient)

    async def run():
        return [e async for e in client._process_sse_stream(FakeStream(frames))]

    return asyncio.run(run())


def _inner(events, key):
    return [e["event"][key] for e in events if key in e.get("event", {})]


def test_stream_client_outlasts_a_silent_tool_step():
    """botocore's 60s default read timeout is shorter than one silent sub-agent step.

    read_timeout bounds the gap between chunks, and this runtime's heartbeat stops
    once text begins, so a later sub-agent call streams nothing for 20-60s. At the
    default the stream is severed mid-answer. Retries must stay off: a retry
    re-runs the entire turn, billing twice and repeating tool side effects.
    """
    from agents.agentcore_client import stream_boto_config

    config = stream_boto_config()
    assert config.read_timeout >= 300
    assert config.retries["max_attempts"] == 0


def test_each_client_gets_its_own_boto_config():
    """Building a client normalises the config's retries dict in place.

    botocore rewrites `max_attempts: 0` to `total_max_attempts: 1` on the very
    object it is handed, so a single shared Config would be mutated by the first
    client constructed and the next reader would see different keys.
    """
    from agents.agentcore_client import stream_boto_config

    assert stream_boto_config() is not stream_boto_config()
    assert stream_boto_config().retries["max_attempts"] == 0


def test_typed_text_chunks_reach_the_ui():
    """The answer text must arrive as contentBlockDelta, not be silently dropped."""
    events = _collect(TYPED_TURN)

    deltas = [
        d["delta"]["text"]
        for d in _inner(events, "contentBlockDelta")
        if "text" in d.get("delta", {})
    ]
    assert "".join(deltas) == "설비 3건입니다."


def test_typed_turn_is_framed_by_start_and_stop():
    """Without messageStart/messageStop the UI never opens or persists the message."""
    events = _collect(TYPED_TURN)

    assert len(_inner(events, "messageStart")) == 1
    stops = _inner(events, "messageStop")
    assert len(stops) == 1
    assert stops[0]["fullText"] == "설비 3건입니다."


def test_typed_reasoning_stays_in_its_own_lane():
    """Reasoning must not be concatenated into the answer body."""
    events = _collect(TYPED_TURN)

    reasoning = [
        d["delta"]["reasoningContent"]["text"]
        for d in _inner(events, "contentBlockDelta")
        if "reasoningContent" in d.get("delta", {})
    ]
    assert reasoning == ["user wants the equipment list"]


def test_typed_tool_call_and_result_are_correlated():
    """A tool_call carries no toolUseId, so one is synthesised and reused by its result."""
    events = _collect(TYPED_TURN)

    starts = [
        s for s in _inner(events, "contentBlockStart") if "toolUse" in s.get("start", {})
    ]
    assert len(starts) == 1
    tool_use = starts[0]["start"]["toolUse"]
    assert tool_use["name"] == "ask_sql_specialist"

    results = _inner(events, "toolResult")
    assert len(results) == 1
    # Same id on both, or the UI leaves the tool call spinning forever.
    assert results[0]["toolUseId"] == tool_use["toolUseId"]
    assert results[0]["result"] == "3 rows"


def test_orphan_tool_result_still_reaches_the_ui():
    """A sub-agent's result arrives with no tool_call of its own.

    ks_text2sql_agent stashes sub-agent results and flushes them, so the
    orchestrator stream shows a `tool_result` for `ask_validator` that was never
    preceded by a `tool_call`. Dropping it would discard work the runtime did.
    """
    events = _collect(
        [
            '{"type": "tool_result", "tool": "ask_validator", "result": "9 checks passed"}',
            '{"type": "text", "chunk": "done"}',
            '{"type": "done", "session_id": "s-3"}',
        ]
    )

    starts = [
        s for s in _inner(events, "contentBlockStart") if "toolUse" in s.get("start", {})
    ]
    assert len(starts) == 1
    assert starts[0]["start"]["toolUse"]["name"] == "ask_validator"

    results = _inner(events, "toolResult")
    assert len(results) == 1
    assert results[0]["result"] == "9 checks passed"
    # Settled against the block that was opened for it, not left dangling.
    assert results[0]["toolUseId"] == starts[0]["start"]["toolUse"]["toolUseId"]


def test_repeated_tool_name_gets_distinct_ids():
    """Two calls to the same tool are distinct; ids must not be reused."""
    events = _collect(
        [
            '{"type": "tool_call", "tool": "ask_sql_specialist", "args": {"q": "a"}}',
            '{"type": "tool_result", "tool": "ask_sql_specialist", "result": "first"}',
            '{"type": "tool_call", "tool": "ask_sql_specialist", "args": {"q": "b"}}',
            '{"type": "tool_result", "tool": "ask_sql_specialist", "result": "second"}',
            '{"type": "done", "session_id": "s-4"}',
        ]
    )

    results = _inner(events, "toolResult")
    assert [r["result"] for r in results] == ["first", "second"]
    assert results[0]["toolUseId"] != results[1]["toolUseId"]


def test_liveness_frames_become_status_events():
    """thinking/heartbeat say what is happening during a silent step.

    Both carry a human-readable label; `thinking` puts it in `text` and
    `heartbeat` in `label`. Without these the UI shows nothing for the tens of
    seconds before the first token.
    """
    events = _collect(TYPED_TURN)

    statuses = _inner(events, "agentStatus")
    assert [s["label"] for s in statuses] == ["분석 중…", "질문 분석 중"]
    # The heartbeat's phase and elapsed time drive the status line's timer.
    assert statuses[1]["phase"] == "think"
    assert statuses[1]["elapsedMs"] == 2000


def test_status_precedes_the_answer_it_covers():
    """Status must arrive before the text, or it explains a wait already over."""
    events = _collect(TYPED_TURN)

    kinds = [k for e in events for k in e.get("event", {})]
    first_status = kinds.index("agentStatus")
    first_text = next(
        i
        for i, e in enumerate(events)
        if "text" in e.get("event", {}).get("contentBlockDelta", {}).get("delta", {})
    )
    assert first_status < first_text


def test_status_alone_does_not_open_a_message():
    """A status frame is not content: on its own it must not start an assistant turn.

    A prewarm ping emits `thinking` and `done` and no text. If status opened a
    message, that ping would persist an empty assistant bubble.
    """
    events = _collect(
        [
            '{"type": "thinking", "text": "분석 중…"}',
            '{"type": "done", "session_id": "", "ping": true}',
        ]
    )

    assert len(_inner(events, "agentStatus")) == 1
    assert _inner(events, "messageStart") == []
    assert _inner(events, "messageStop") == []


def test_label_less_liveness_frame_is_dropped():
    """An empty label would render a blank status row."""
    events = _collect(['{"type": "heartbeat", "phase": "think", "elapsed_ms": 5000}'])

    assert _inner(events, "agentStatus") == []


def test_typed_error_surfaces_instead_of_silence():
    """A runtime-reported failure must become a visible error event."""
    events = _collect(
        ['{"type": "error", "message": "질문이 비어 있습니다."}',
         '{"type": "done", "session_id": "s-2"}']
    )

    errors = [e for e in events if "error" in e.get("event", {})]
    assert len(errors) == 1
    assert "질문이 비어" in str(errors[0])


def test_ping_frame_does_not_fabricate_an_empty_message():
    """Prewarm sends `__ping__` and gets only `done`; that is not an assistant turn."""
    events = _collect(['{"type": "done", "session_id": "", "ping": true}'])

    assert _inner(events, "messageStart") == []
    assert _inner(events, "messageStop") == []


def test_typed_tool_args_are_serialised_as_a_json_string():
    """A typed tool_call's `args` dict must reach the UI as a JSON *string*.

    Strands-native toolUse.input is streamed partial-JSON text, and every
    consumer accumulates it with `+`: the server does
    `current_tool_uses[id]["input"] += input` and the browser does
    `currentInput + input`. Forwarding the dict verbatim made the server raise
    `can only concatenate str (not "dict") to str` (shown as an error card) and
    the browser build "[object Object]", which then failed JSON.parse. The block
    must carry a string, and it must round-trip back to the original arguments.
    """
    import json

    events = _collect(TYPED_TURN)

    inputs = [
        d["delta"]["toolUse"]["input"]
        for d in _inner(events, "contentBlockDelta")
        if "toolUse" in d.get("delta", {})
    ]
    assert len(inputs) == 1
    assert isinstance(inputs[0], str)
    # Accumulating it the way the server/browser do must yield valid JSON that
    # parses back to the arguments the runtime sent.
    assert json.loads("" + inputs[0]) == {"question": "list equipment"}


def test_typed_tool_args_keep_non_ascii_legible():
    """Korean argument values must not be escaped to \\uXXXX in the block."""
    events = _collect(
        [
            '{"type": "tool_call", "tool": "search_export_delivery", '
            '"args": {"selname": "판매처"}}',
            '{"type": "done", "session_id": "s-ko"}',
        ]
    )
    inputs = [
        d["delta"]["toolUse"]["input"]
        for d in _inner(events, "contentBlockDelta")
        if "toolUse" in d.get("delta", {})
    ]
    assert len(inputs) == 1
    assert "판매처" in inputs[0]
