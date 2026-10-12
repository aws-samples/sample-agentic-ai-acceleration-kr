"""The sweep runs once per harness turn, and only for a harness.

Three things are pinned here, each of which fails silently otherwise:

- A **runtime** agent must never be swept. It emits its own `artifact` events, so
  a sweep alongside them would register everything twice. Split on client kind,
  never on timing — the same rule the MCP Apps signal follows.
- The live path emits the collected files as `artifact` events, so the cards
  appear before the turn closes.
- The interrupted path spawns the delayed sweep instead of awaiting one. It cannot
  await (a closing generator has no loop), and the agent is usually still working
  when the client disconnects.
- Every collected file names the message the turn produced. The chat renders a card
  only through a per-message filter (`message_id` or `tool_call_id` must match), so
  a row stored without one is invisible: stored, downloadable through the API, and
  attached to nothing on screen. Measured on the deployed stack — all six swept
  rows had `message_id=None`, and this file used to assert exactly that.
"""
import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.artifact import ArtifactVersion  # noqa: E402
from models.common import StreamRequest  # noqa: E402
from services.streaming_service import StreamingService  # noqa: E402

THREAD = "t1"
HARNESS_ARN = "arn:aws:bedrock-agentcore:us-east-1:1:harness/writer-abc"
VALUES = {"messages": [{"id": "u-1", "type": "human", "content": "make a report"}]}


def swept_file():
    return ArtifactVersion(
        artifact_id="a1",
        version=1,
        thread_id=THREAD,
        title="보고서.docx",
        kind="file",
        s3_key="artifacts/t1/a1/v1.docx",
        size_bytes=54542,
        created_at="2026-08-14T09:40:00",
        filename="보고서.docx",
        source_path="/home/보고서.docx",
        source_mtime=5,
    )


class StubOutputs:
    enabled = True

    def __init__(self, collected=None):
        self.collected = collected if collected is not None else [swept_file()]
        self.swept = []
        self.spawned = []

    async def sweep(self, thread_id, harness_arn, message_id=None):
        self.swept.append((thread_id, harness_arn, message_id))
        return self.collected

    def spawn_delayed_sweep(self, thread_id, harness_arn, message_id=None):
        self.spawned.append((thread_id, harness_arn, message_id))


class StubRepo:
    def update(self, thread_id, thread):
        return thread


class StubThread:
    def __init__(self):
        self.values = dict(VALUES)
        self.updated_at = ""


class StubThreadService:
    def __init__(self):
        self.thread = StubThread()
        self.repository = StubRepo()
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


class FakeHarnessClient:
    """Stands in for HarnessClient; the service must recognise it as a harness."""

    async def execute_stream(self, thread_id, values, config=None, actor_id=None):
        yield {"event": {"messageStart": {"id": "msg-1"}}}
        yield {"event": {"contentBlockDelta": {"delta": {"text": "작성했습니다"}}}}
        yield {"event": {"messageStop": {"messageId": "msg-1", "fullText": "작성했습니다"}}}


class FakeRuntimeClient:
    async def execute_stream(self, thread_id, values, config=None, actor_id=None):
        yield {"event": {"messageStart": {"id": "msg-1"}}}
        yield {"event": {"messageStop": {"messageId": "msg-1", "fullText": "done"}}}


def build(outputs, harness=True):
    service = StreamingService(
        thread_service=StubThreadService(),
        agentcore_client=None,
        artifact_service=None,
        harness_output_service=outputs,
    )
    service._require_approved = lambda record_id, name: None
    return service


def request(harness=True):
    config = {"harness_arn": HARNESS_ARN} if harness else {"agent_runtime_arn": "arn:…:runtime/x"}
    return StreamRequest(values=VALUES, config=config)


async def collect(service, request_obj, client):
    """Drive the SSE generator and return the decoded `data:` frames."""
    service._get_agent_client = lambda config: client
    response = await service.stream_thread_execution(THREAD, request_obj)
    frames = []
    async for chunk in response.body_iterator:
        for line in chunk.splitlines():
            if line.startswith("data: "):
                frames.append(json.loads(line[6:]))
    return frames


@pytest.mark.asyncio
async def test_a_harness_turn_is_swept_and_emits_artifact_events(monkeypatch):
    import services.streaming_service as module

    monkeypatch.setattr(module, "HarnessClient", FakeHarnessClient)
    outputs = StubOutputs()
    frames = await collect(build(outputs), request(), FakeHarnessClient())

    # The message the turn just persisted, not None: `messageStop` clears the
    # tracking variable, and the sweep runs after it.
    assert outputs.swept == [(THREAD, HARNESS_ARN, "msg-1")]

    # frames are already parsed dicts (from collect function)
    events = frames

    # The turn's last word is `end`; anything emitted after it is invisible to the panel.
    assert events[-1].get("event") == "end", events[-1]

    artifact_positions = [
        index
        for index, event in enumerate(events)
        if isinstance(event.get("event"), dict) and "artifact" in event["event"]
    ]
    assert artifact_positions, "no artifact frame reached the browser"
    assert artifact_positions[-1] < len(events) - 1

    # Check the artifact content
    assert events[artifact_positions[0]]["event"]["artifact"]["kind"] == "file"
    assert events[artifact_positions[0]]["event"]["artifact"]["title"] == "보고서.docx"
    assert events[artifact_positions[0]]["event"]["artifact"]["stored"] is True


@pytest.mark.asyncio
async def test_a_runtime_turn_is_never_swept(monkeypatch):
    import services.streaming_service as module

    monkeypatch.setattr(module, "HarnessClient", FakeHarnessClient)
    outputs = StubOutputs()
    await collect(build(outputs), request(harness=False), FakeRuntimeClient())
    assert outputs.swept == []
    assert outputs.spawned == []


@pytest.mark.asyncio
async def test_nothing_collected_emits_nothing(monkeypatch):
    import services.streaming_service as module

    monkeypatch.setattr(module, "HarnessClient", FakeHarnessClient)
    outputs = StubOutputs(collected=[])
    frames = await collect(build(outputs), request(), FakeHarnessClient())
    artifacts = [f for f in frames if isinstance(f.get("event"), dict) and "artifact" in f["event"]]
    assert not artifacts


@pytest.mark.asyncio
async def test_an_interrupted_turn_spawns_the_delayed_sweep(monkeypatch):
    import services.streaming_service as module

    monkeypatch.setattr(module, "HarnessClient", FakeHarnessClient)

    class Hanging(FakeHarnessClient):
        async def execute_stream(self, thread_id, values, config=None, actor_id=None):
            yield {"event": {"messageStart": {"id": "msg-1"}}}
            yield {"event": {"contentBlockDelta": {"delta": {"text": "작업 중"}}}}
            await asyncio.sleep(10)

    outputs = StubOutputs()
    service = build(outputs)
    service._get_agent_client = lambda config: Hanging()
    response = await service.stream_thread_execution(THREAD, request())

    iterator = response.body_iterator.__aiter__()
    # Read multiple frames to ensure sweep_harness_arn is set
    for _ in range(3):
        try:
            await iterator.__anext__()
        except StopAsyncIteration:
            break

    # A browser going away no longer interrupts anything — the run is drained
    # by the broker's task. Stop does, through the broker; that is the path
    # that must still schedule the recovery.
    assert service.run_broker.cancel(THREAD) is True
    async for _ in iterator:
        pass

    # The partial message was persisted under this id, so a file recovered minutes
    # later still has a message to hang its card on.
    assert outputs.spawned == [(THREAD, HARNESS_ARN, "msg-1")]
    assert outputs.swept == [], "the interrupted path must not await a sweep"


@pytest.mark.asyncio
async def test_a_sweep_failure_does_not_break_the_turn(monkeypatch):
    import services.streaming_service as module

    monkeypatch.setattr(module, "HarnessClient", FakeHarnessClient)

    class Exploding(StubOutputs):
        async def sweep(self, thread_id, harness_arn, message_id=None):
            raise RuntimeError("command api down")

    frames = await collect(build(Exploding()), request(), FakeHarnessClient())
    events = [f for f in frames]
    assert events[-1].get("event") == "end"
    assert events[-1]["data"]["status"] == "completed"
