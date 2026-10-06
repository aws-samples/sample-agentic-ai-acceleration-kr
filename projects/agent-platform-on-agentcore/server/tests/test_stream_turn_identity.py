"""The stream hands the ledger what it needs to write one exact turn event.

`turn_id` is `{thread_id}:{human message id}` — the same request flushing twice
must present the same id so the ledger counts it once. The model is resolved
*before* the turn runs, from the ARN the request names, so the event is priced
against what the agent actually ran. `model_calls` counts metadata events.
"""
import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.common import StreamRequest  # noqa: E402
from services.streaming_service import StreamingService  # noqa: E402

VALUES = {"messages": [{"id": "h-42", "type": "human", "content": "hello"}]}
RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:1:runtime/x-1"
HARNESS_ARN = "arn:aws:bedrock-agentcore:us-east-1:1:harness/h-1"

class StubRegistry:
    """Answers the record every run here names, so the server-side target binding
    (services/agent_access.py) resolves without a registry call."""

    def __init__(self, harness_arn=None):
        self.harness_arn = harness_arn

    def get_record(self, record_id):
        return SimpleNamespace(
            status="APPROVED", agent_runtime_arn=RUNTIME_ARN, harness_arn=self.harness_arn, qualifier=None
        )



class StubThread:
    def __init__(self, values):
        self.values = values
        self.updated_at = ""
        self.agent_record_id = "rec-1"
        self.agent_name = "agent"
        self.owner_sub = "sub-1"


class StubRepo:
    def __init__(self, thread):
        self.thread = thread

    def update(self, thread_id, thread):
        self.thread.values = thread.values
        return thread


class StubThreadService:
    def __init__(self):
        self.thread = StubThread(dict(VALUES))
        self.repository = StubRepo(self.thread)

    def get_or_create_thread(self, thread_id, owner_sub="", initial_values=None,
                             agent_record_id="", agent_name="", **_kwargs):
        return self.thread

    def get_thread(self, thread_id):
        return self.thread

    def update_thread_status(self, thread_id, status):
        pass


class RecordingUsage:
    configured = True

    def __init__(self):
        self.calls = []
        self.resolved_for = []

    def resolve_model_for(self, *, harness_arn=None, agent_runtime_arn=None):
        self.resolved_for.append((harness_arn, agent_runtime_arn))
        return "global.anthropic.claude-sonnet-5"

    def record_turn(self, **kwargs):
        self.calls.append(kwargs)
        return {"event": "created", "cost_micros": 0}

    def record_guardrail_event(self, **kwargs):
        pass


class TwoModelCallsClient:
    """A turn whose model was called twice (a tool round-trip), then stopped."""

    async def execute_stream(self, thread_id, values, config=None, actor_id=None):
        yield {"event": {"messageStart": {"id": "m-1"}}}
        yield {"event": {"contentBlockDelta": {"delta": {"text": "hi"}}}}
        yield {"event": {"metadata": {"usage": {"inputTokens": 10, "outputTokens": 2}}}}
        yield {"event": {"metadata": {"usage": {"inputTokens": 20, "outputTokens": 3}}}}
        yield {"event": {"messageStop": {"stopReason": "end_turn"}}}


def run(service, config):
    async def scenario():
        response = await service.stream_thread_execution(
            "t-1", StreamRequest(values=VALUES, config=config)
        )
        async for _ in response.body_iterator:
            pass

    asyncio.run(scenario())


def test_flush_passes_turn_identity_and_model():
    usage = RecordingUsage()
    client = TwoModelCallsClient()
    service = StreamingService(
        thread_service=StubThreadService(), agentcore_client=client, usage_service=usage,
        registry_service=StubRegistry(),
    )
    service._get_agent_client = lambda config=None: client

    run(service, {"agent_runtime_arn": RUNTIME_ARN, "registry_record_id": "rec-1"})

    assert usage.calls, "the turn was never recorded"
    first = usage.calls[0]
    assert first["turn_id"] == "t-1:h-42"
    assert first["thread_id"] == "t-1"
    assert first["model_id"] == "global.anthropic.claude-sonnet-5"
    assert first["started_at"] and first["ended_at"]
    assert first["model_calls"] == 2
    assert first["input_tokens"] == 30 and first["output_tokens"] == 5
    assert usage.resolved_for == [(None, RUNTIME_ARN)]
    # Every flush of this request carries the same turn id.
    assert {call["turn_id"] for call in usage.calls} == {"t-1:h-42"}


def test_a_thread_model_override_is_what_the_turn_is_priced_at():
    """`config.model_id` is what InvokeHarness actually runs (harness_client sends it),
    so pricing at the harness's default model would store a permanently wrong figure."""
    usage = RecordingUsage()
    client = TwoModelCallsClient()
    service = StreamingService(
        thread_service=StubThreadService(), agentcore_client=client, usage_service=usage,
        registry_service=StubRegistry(harness_arn=HARNESS_ARN),
    )
    service._get_agent_client = lambda config=None: client

    run(service, {"harness_arn": HARNESS_ARN,
                  "registry_record_id": "rec-1", "model_id": "global.anthropic.claude-opus-5-5"})

    assert usage.calls[0]["model_id"] == "global.anthropic.claude-opus-5-5"
    assert usage.resolved_for == [], "no lookup when the request names the model"
