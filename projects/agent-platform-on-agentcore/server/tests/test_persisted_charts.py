"""A chart must still be there when the thread is reopened.

Charts and verification notes arrive *after* `messageStop` — the runtime emits its
answer, ends the turn, then flushes what it stashed:

    …text… → messageStop → chart → verification → done

`messageStop` is where the assistant message is written to DynamoDB, so anything
after it was yielded to the browser and then forgotten. The live view showed the
chart; reopening the thread showed the answer with no chart under it.

That is the same defect the stored tool-call status had, and it matters more here,
because the chart's other route is worse: the orchestrator writes the image into
the answer as markdown, and that URL is presigned for five minutes while the
object behind it lives for days. So a reopened thread rendered a broken image and
the data that could have redrawn it had been dropped twice over.

The fix records them like tool results and rewrites the message, so the stored copy
matches what the live view rendered.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.common import StreamRequest  # noqa: E402
from services.streaming_service import StreamingService  # noqa: E402

VALUES = {"messages": [{"id": "u1", "type": "human", "content": "차트로 보여줘"}]}

CHART_SPEC = {
    "kind": "bar",
    "data": [{"업체": "주식회사대림테크", "금형비합계": 3890173100}],
    "encoding": {"x": "업체", "y": "금형비합계", "color": None},
    "title": "금형비 지급액 상위 3개 업체",
}

# The real ordering: the chart lands after the turn has already been closed.
CHART_TURN = [
    {"event": {"messageStart": {"id": "m1", "role": "assistant"}}},
    {"event": {"contentBlockDelta": {"delta": {"text": "상위 3개 업체입니다."}}}},
    {
        "event": {
            "messageStop": {
                "stopReason": "end_turn",
                "messageId": "m1",
                "fullText": "상위 3개 업체입니다.",
            }
        }
    },
    {
        "event": {
            "chart": {
                "spec": CHART_SPEC,
                "url": "https://staging.s3.amazonaws.com/charts/s/step-1.png?Expires=1",
                "source": "render_chart",
            }
        }
    },
    {
        "event": {
            "verification": {
                "method": "execution_consensus",
                "k": 3,
                "n_valid": 3,
                "agreement": 0.667,
                "verdict": "PASS",
            }
        }
    },
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
        self, thread_id, owner_sub="", initial_values=None,
        agent_record_id="", agent_name="", **_kwargs):
        return self._thread()

    def get_thread(self, thread_id):
        return self._thread()

    def _thread(self):
        t = StubThread(self.repository)
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


def test_a_chart_is_persisted_with_its_message():
    """Reopening the thread must show the chart the live view showed."""
    message = ai_message(persisted_messages(CHART_TURN))

    assert message.get("charts"), "the chart was not saved with the message"
    assert len(message["charts"]) == 1


def test_the_persisted_chart_keeps_its_spec():
    """The spec is what redraws; without it the record is only a dead image link."""
    message = ai_message(persisted_messages(CHART_TURN))

    assert message["charts"][0]["spec"] == CHART_SPEC
    assert message["charts"][0]["spec"]["data"][0]["업체"] == "주식회사대림테크"


def test_a_verification_is_persisted_with_its_message():
    message = ai_message(persisted_messages(CHART_TURN))

    assert message.get("verifications"), "the verification was not saved"
    assert message["verifications"][0]["verdict"] == "PASS"


def test_the_answer_text_is_not_disturbed():
    """Rewriting the message to add a chart must not lose what was already there."""
    message = ai_message(persisted_messages(CHART_TURN))

    assert message["content"] == "상위 3개 업체입니다."
    assert message["id"] == "m1"


def test_the_message_is_not_duplicated():
    """The record is rewritten in place, not appended a second time."""
    messages = persisted_messages(CHART_TURN)

    ai_messages = [m for m in messages if m.get("type") == "ai"]
    assert len(ai_messages) == 1


def test_several_charts_in_one_turn_are_all_kept():
    """A turn can draw more than one chart; the later must not replace the first."""
    second = {
        "event": {
            "chart": {
                "spec": {**CHART_SPEC, "title": "월별 추이", "kind": "line"},
                "source": "render_chart",
            }
        }
    }
    message = ai_message(persisted_messages([*CHART_TURN, second]))

    titles = [c["spec"]["title"] for c in message["charts"]]
    assert titles == ["금형비 지급액 상위 3개 업체", "월별 추이"]


def test_a_chart_before_messageStop_is_also_kept():
    """Ordering is the runtime's choice, so neither order may drop the chart."""
    turn = [
        {"event": {"messageStart": {"id": "m1", "role": "assistant"}}},
        {"event": {"contentBlockDelta": {"delta": {"text": "보여드립니다."}}}},
        {"event": {"chart": {"spec": CHART_SPEC, "source": "render_chart"}}},
        {
            "event": {
                "messageStop": {
                    "stopReason": "end_turn",
                    "messageId": "m1",
                    "fullText": "보여드립니다.",
                }
            }
        },
    ]
    message = ai_message(persisted_messages(turn))

    assert len(message.get("charts") or []) == 1


def test_a_turn_with_no_chart_stores_no_chart_key():
    """An empty list on every message would be noise in every stored record."""
    turn = [
        {"event": {"messageStart": {"id": "m1", "role": "assistant"}}},
        {"event": {"contentBlockDelta": {"delta": {"text": "안녕하세요."}}}},
        {
            "event": {
                "messageStop": {
                    "stopReason": "end_turn",
                    "messageId": "m1",
                    "fullText": "안녕하세요.",
                }
            }
        },
    ]
    message = ai_message(persisted_messages(turn))

    assert "charts" not in message
    assert "verifications" not in message


def test_a_chart_with_no_message_still_survives():
    """A chart is work the runtime did; it must not be lost for want of a message.

    The turn produced no text and no tool call, so nothing would otherwise be
    written at all.
    """
    turn = [{"event": {"chart": {"spec": CHART_SPEC, "source": "render_chart"}}}]
    messages = persisted_messages(turn)

    ai_messages = [m for m in messages if m.get("type") == "ai"]
    assert len(ai_messages) == 1
    assert len(ai_messages[0]["charts"]) == 1
