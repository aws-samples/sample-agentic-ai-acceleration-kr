"""A stored turn must remember whether a tool call came before or after the text.

One turn is one message: `_process_sse_stream` mints a single messageId and the
harness adapter deliberately reuses its base id across the whole tool loop, so an
answer and the calls that produced it land in the *same* record. `content` and
`tool_calls` are separate fields on that record, which means their relative order
is nowhere in the stored shape — and the UI, reading it back, could only paint all
text and then all calls. The final answer therefore rendered *above* the
`ask_sql_specialist` call it was derived from.

`contentOffset` is that missing order: how much answer text had accumulated when
each call opened. It is derived from text the server already tracks, so it costs
no new wire field, and a turn whose call precedes all text records 0.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.common import StreamRequest  # noqa: E402
from services.streaming_service import StreamingService  # noqa: E402

VALUES = {"messages": [{"id": "u1", "type": "human", "content": "설비 목록"}]}


def _tool_use(index, tool_use_id, name, args):
    return [
        {
            "event": {
                "contentBlockStart": {
                    "contentBlockIndex": index,
                    "start": {"toolUse": {"toolUseId": tool_use_id, "name": name}},
                }
            }
        },
        {
            "event": {
                "contentBlockDelta": {
                    "contentBlockIndex": index,
                    "delta": {"toolUse": {"input": args}},
                }
            }
        },
        {"event": {"contentBlockStop": {"contentBlockIndex": index}}},
    ]


def _text(chunk):
    return {"event": {"contentBlockDelta": {"delta": {"text": chunk}}}}


# The shape the report describes: a preamble, a sub-agent call, then the answer.
INTERLEAVED_TURN = [
    {"event": {"messageStart": {"id": "m1", "role": "assistant"}}},
    _text("확인해 보겠습니다."),
    *_tool_use(1, "tu-1", "ask_sql_specialist", '{"question":"list equipment"}'),
    {"event": {"toolResult": {"toolUseId": "tu-1", "result": "3 rows"}}},
    _text("설비는 3건입니다."),
    {"event": {"messageStop": {"stopReason": "end_turn", "messageId": "m1"}}},
]


class StubThread:
    def __init__(self):
        self.values = {"messages": list(VALUES["messages"])}
        self.updated_at = ""


class StubRepo:
    def __init__(self):
        self.messages = []

    def update(self, thread_id, thread):
        self.messages = thread.values.get("messages", [])
        return thread


class StubThreadService:
    def __init__(self):
        self.repository = StubRepo()

    def get_or_create_thread(
        self,
        thread_id,
        owner_sub="",
        initial_values=None,
        agent_record_id="",
        agent_name="", **_kwargs):
        return self._thread()

    def get_thread(self, thread_id):
        return self._thread()

    def _thread(self):
        t = StubThread()
        if self.repository.messages:
            t.values["messages"] = [dict(m) for m in self.repository.messages]
        return t

    def update_thread_status(self, thread_id, status):
        pass


class ScriptedClient:
    def __init__(self, events):
        self.events = events

    async def execute_stream(self, thread_id, values, config=None, actor_id=None):
        for event in self.events:
            yield event


def persisted_messages(events):
    threads = StubThreadService()
    client = ScriptedClient(events)
    service = StreamingService(thread_service=threads, agentcore_client=client)
    service._get_agent_client = lambda config=None: client

    async def scenario():
        response = await service.stream_thread_execution(
            "t-1", StreamRequest(values=VALUES)
        )
        async for _ in response.body_iterator:
            pass

    asyncio.run(scenario())
    return threads.repository.messages


def ai_message(messages):
    return next(m for m in messages if m.get("type") == "ai")


def test_a_tool_call_records_where_it_sat_in_the_answer():
    """Without this the UI has nothing to order text and calls by."""
    call = ai_message(persisted_messages(INTERLEAVED_TURN))["tool_calls"][0]

    assert "contentOffset" in call, (
        "the persisted call carries no position, so a reopened thread must render "
        f"it after the whole answer: {call}"
    )


def test_the_offset_splits_the_answer_where_the_call_happened():
    """The offset is a cut point into `content`, so it must land between the two halves."""
    message = ai_message(persisted_messages(INTERLEAVED_TURN))
    offset = message["tool_calls"][0]["contentOffset"]

    assert message["content"][:offset] == "확인해 보겠습니다."
    assert message["content"][offset:] == "설비는 3건입니다."


def test_a_call_before_any_text_records_zero():
    """A turn that calls a tool first has no preamble to sit after."""
    events = [
        {"event": {"messageStart": {"id": "m1", "role": "assistant"}}},
        *_tool_use(1, "tu-1", "ask_sql_specialist", '{"question":"q"}'),
        _text("설비는 3건입니다."),
        {"event": {"messageStop": {"stopReason": "end_turn", "messageId": "m1"}}},
    ]
    call = ai_message(persisted_messages(events))["tool_calls"][0]

    assert call["contentOffset"] == 0


def test_two_calls_around_text_get_increasing_offsets():
    """Offsets are what put a later call after the text that preceded it."""
    events = [
        {"event": {"messageStart": {"id": "m1", "role": "assistant"}}},
        _text("먼저 조회합니다."),
        *_tool_use(1, "tu-1", "ask_sql_specialist", '{"question":"a"}'),
        _text("이제 검증합니다."),
        *_tool_use(2, "tu-2", "ask_validator", '{"sql":"select 1"}'),
        _text("완료."),
        {"event": {"messageStop": {"stopReason": "end_turn", "messageId": "m1"}}},
    ]
    message = ai_message(persisted_messages(events))
    first, second = message["tool_calls"]

    assert first["contentOffset"] == len("먼저 조회합니다.")
    assert second["contentOffset"] == len("먼저 조회합니다.이제 검증합니다.")
    # Strictly increasing, or the two calls would collapse into one group.
    assert first["contentOffset"] < second["contentOffset"]


def test_the_offset_does_not_displace_what_was_already_stored():
    """Position is additive: name, args, status and result must all survive."""
    call = ai_message(persisted_messages(INTERLEAVED_TURN))["tool_calls"][0]

    assert call["name"] == "ask_sql_specialist"
    assert call["args"] == {"question": "list equipment"}
    assert call["status"] == "completed"
    assert call["result"] == "3 rows"


def test_the_offset_is_counted_the_way_javascript_counts():
    """The consumer is `String.prototype.slice`, which indexes UTF-16 code units.

    Python's `len()` counts code points, so the two agree only until the answer
    contains an astral character. An emoji — which agents emit readily — is two
    UTF-16 units but one code point, so counting in code points would place the
    cut one character early for every emoji that preceded the call, and the answer
    would visibly lose a character into the tool call above it.
    """
    events = [
        {"event": {"messageStart": {"id": "m1", "role": "assistant"}}},
        _text("조회 완료 🚀 그리고"),
        *_tool_use(1, "tu-1", "ask_validator", '{"sql":"select 1"}'),
        _text("끝"),
        {"event": {"messageStop": {"stopReason": "end_turn", "messageId": "m1"}}},
    ]
    message = ai_message(persisted_messages(events))
    offset = message["tool_calls"][0]["contentOffset"]

    prefix = "조회 완료 🚀 그리고"
    utf16_len = len(prefix.encode("utf-16-le")) // 2
    assert offset == utf16_len
    # The emoji is what makes this a real difference rather than a tautology.
    assert utf16_len == len(prefix) + 1

    # Sliced as the browser would: the prefix must come back whole.
    units = message["content"].encode("utf-16-le")
    assert units[: offset * 2].decode("utf-16-le") == prefix
    assert units[offset * 2 :].decode("utf-16-le") == "끝"


def test_a_turn_cut_short_still_records_the_position():
    """The final-flush path writes the same shape as the messageStop path."""
    truncated = INTERLEAVED_TURN[:-1]
    call = ai_message(persisted_messages(truncated))["tool_calls"][0]

    assert call["contentOffset"] == len("확인해 보겠습니다.")
