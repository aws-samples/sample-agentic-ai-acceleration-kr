"""
An interrupted turn must keep the work it had already done.

Persisting only on `messageStop` means nothing is stored for the whole of a
multi-minute tool loop — a harness runs its tools inside AWS and emits one
`messageStop`, at the very end. So when the connection died mid-turn the
cancellation handler wrote `interrupted` and dropped everything: the stored thread
held the user's question and no assistant message at all.

That is what emptied the screen. The browser rebuilds a thread from the stored
record on (re)mount, so a record with no assistant message renders as the question
alone — every tool call and every token that had been on screen vanished, while
the harness was still working on the other side.

Measured against the real thing: thread e1caddb6 (agent academic_writer) came out
of a severed stream as `interrupted` with `values.messages == [human, human]`.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.common import StreamRequest, ThreadStatus  # noqa: E402
from services.streaming_service import StreamingService  # noqa: E402

VALUES = {"messages": [{"id": "u-1", "type": "human", "content": "write me a docx"}]}


class StubThread:
    def __init__(self, values):
        self.values = values
        self.updated_at = ""


class StubRepo:
    """Writes land in the one dict every read also comes from, as DynamoDB does."""

    def __init__(self, thread):
        self.thread = thread
        self.writes = 0

    def update(self, thread_id, thread):
        self.thread.values = thread.values
        self.writes += 1
        return thread


class StubThreadService:
    def __init__(self):
        self.thread = StubThread(dict(VALUES))
        self.repository = StubRepo(self.thread)
        self.statuses = []

    def get_or_create_thread(
        self,
        thread_id,
        owner_sub="",
        initial_values=None,
        agent_record_id="",
        agent_name="", **_kwargs):
        return self.thread

    def get_thread(self, thread_id):
        return self.thread

    def update_thread_status(self, thread_id, status):
        self.statuses.append(status)


class WorkingThenHangingClient:
    """Answers a little, calls a tool, then goes silent — a harness mid-tool."""

    async def execute_stream(self, thread_id, values, config=None, actor_id=None):
        yield {"event": {"messageStart": {"id": "m-1"}}}
        yield {"event": {"contentBlockDelta": {"delta": {"text": "Researching"}}}}
        yield {
            "event": {
                "contentBlockStart": {
                    "start": {"toolUse": {"toolUseId": "tu-1", "name": "WebSearch"}}
                }
            }
        }
        yield {
            "event": {
                "contentBlockDelta": {"delta": {"toolUse": {"input": '{"q": "physical ai"}'}}}
            }
        }
        yield {"event": {"contentBlockStop": {}}}
        # The tool now runs inside AWS. Nothing comes out until it finishes.
        await asyncio.sleep(3600)


def service_with(client):
    threads = StubThreadService()
    service = StreamingService(thread_service=threads, agentcore_client=client)
    service._get_agent_client = lambda config=None: client
    return service, threads


def drain_then_disconnect(service):
    """Read the whole prelude, then close the generator — what a severed proxy does."""

    async def scenario():
        response = await service.stream_thread_execution(
            "t-1", StreamRequest(values=VALUES)
        )
        agen = response.body_iterator.__aiter__()
        # Six frames: thread_created plus the five the client emits before it hangs.
        for _ in range(6):
            await agen.__anext__()
        await agen.aclose()

    asyncio.run(scenario())


def stored_messages(threads):
    return threads.thread.values.get("messages", [])


def test_an_interrupted_turn_stores_its_partial_answer():
    service, threads = service_with(WorkingThenHangingClient())

    drain_then_disconnect(service)

    ai = [m for m in stored_messages(threads) if m.get("type") == "ai"]
    assert ai, (
        "the interrupted turn stored no assistant message — a reopened thread "
        "shows the question alone and the answer looks lost"
    )
    assert ai[-1]["content"] == "Researching"


def test_an_interrupted_turn_stores_the_tool_calls_it_made():
    """The calls were on screen; losing them is what reads as 'everything vanished'."""
    service, threads = service_with(WorkingThenHangingClient())

    drain_then_disconnect(service)

    ai = [m for m in stored_messages(threads) if m.get("type") == "ai"]
    tool_calls = ai[-1].get("tool_calls") or []
    assert [c["name"] for c in tool_calls] == ["WebSearch"]
    # A stored call with no status reads as still running, so the spinner would
    # turn forever in a thread whose run ended long ago.
    assert tool_calls[0]["status"] == "completed"
    assert tool_calls[0]["args"] == {"q": "physical ai"}


def test_the_user_message_is_not_lost_either():
    """The partial write must amend the transcript, not replace it."""
    service, threads = service_with(WorkingThenHangingClient())

    drain_then_disconnect(service)

    kinds = [m.get("type") for m in stored_messages(threads)]
    assert kinds == ["human", "ai"]


def test_persisting_does_not_disturb_the_interrupted_status():
    """`interrupted` is what tells the sidebar the turn did not finish."""
    service, threads = service_with(WorkingThenHangingClient())

    drain_then_disconnect(service)

    assert threads.statuses[-1] == ThreadStatus.INTERRUPTED
    assert ThreadStatus.IDLE not in threads.statuses
