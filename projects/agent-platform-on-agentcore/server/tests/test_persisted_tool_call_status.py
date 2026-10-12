"""Tool calls written to DynamoDB must carry the status they ended on.

The UI cannot tell a stored transcript from a live one: it reads `tool_calls` the
same way in both cases and treats a call with no `status` as still running. So a
status-less tool call in the thread record renders a spinner that never stops —
the run is long over, but nothing will ever arrive to settle it.

`messageStop` is the point where that is knowable. It ends the model's turn, so
no further input or result for those calls is coming, which is exactly the
reasoning useStream already applies client-side when it settles pending calls on
messageStop. The stored copy has to make the same call, or reopening a thread
resurrects spinners the live view had already put to rest.
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.common import StreamRequest  # noqa: E402
from services.streaming_service import StreamingService  # noqa: E402

VALUES = {"messages": [{"id": "u1", "type": "human", "content": "hi"}]}

TOOL_TURN = [
    {"event": {"messageStart": {"id": "m1", "role": "assistant"}}},
    {"event": {"contentBlockDelta": {"delta": {"text": "calculating"}}}},
    {
        "event": {
            "contentBlockStart": {
                "contentBlockIndex": 1,
                "start": {"toolUse": {"toolUseId": "tu-1", "name": "calculate"}},
            }
        }
    },
    {
        "event": {
            "contentBlockDelta": {
                "contentBlockIndex": 1,
                "delta": {"toolUse": {"input": '{"expression":"2**20"}'}},
            }
        }
    },
    {"event": {"contentBlockStop": {"contentBlockIndex": 1}}},
    {"event": {"messageStop": {"stopReason": "tool_use", "messageId": "m1"}}},
]


class StubThread:
    def __init__(self, store):
        self.values = {"messages": list(VALUES["messages"])}
        self.updated_at = ""
        self._store = store


class StubRepo:
    """Keeps the last written message list, which is what the UI later reads."""

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
        t = StubThread(self.repository)
        # Carry forward whatever has been persisted, the way DynamoDB would.
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


def tool_calls_of(messages):
    return [tc for m in messages for tc in (m.get("tool_calls") or [])]


def test_a_persisted_tool_call_is_not_left_without_a_status():
    """No status reads as "still running" to the UI, forever."""
    calls = tool_calls_of(persisted_messages(TOOL_TURN))

    assert calls, "the tool call was not persisted at all"
    for call in calls:
        assert call.get("status"), f"tool call persisted with no status: {call}"


def test_a_persisted_tool_call_is_settled_not_pending():
    """messageStop ended the turn, so nothing will ever settle a `pending`."""
    calls = tool_calls_of(persisted_messages(TOOL_TURN))

    assert [c["status"] for c in calls] == ["completed"]


def test_the_arguments_still_survive_alongside_the_status():
    """The status must not come at the cost of what the call was."""
    calls = tool_calls_of(persisted_messages(TOOL_TURN))

    assert calls[0]["name"] == "calculate"
    assert calls[0]["args"] == {"expression": "2**20"}


def test_a_stream_that_ends_without_messageStop_also_settles():
    """The final-flush path persists the same shape as the messageStop path.

    A run cut short mid-turn is precisely when a spinner would otherwise be left
    running, so this path cannot be the one that omits the status.
    """
    truncated = TOOL_TURN[:-1]  # everything except messageStop
    calls = tool_calls_of(persisted_messages(truncated))

    assert calls, "the interrupted turn persisted no tool call"
    assert [c["status"] for c in calls] == ["completed"]


def test_a_tool_result_that_arrived_is_persisted_with_the_call():
    """Reopening a thread should show the result the live view showed."""
    with_result = TOOL_TURN[:-1] + [
        {"event": {"toolResult": {"toolUseId": "tu-1", "result": "1048576"}}},
        {"event": {"messageStop": {"stopReason": "tool_use", "messageId": "m1"}}},
    ]
    calls = tool_calls_of(persisted_messages(with_result))

    assert calls[0].get("result") == "1048576", (
        "the tool result was dropped on the way to storage, so a reopened "
        f"thread shows an empty tool box: {calls[0]}"
    )
