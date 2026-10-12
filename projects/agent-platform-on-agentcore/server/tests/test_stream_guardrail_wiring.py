"""The stream feeds the guardrail ledger from Bedrock's own trace.

Two model calls with a trace are **one** scanned turn — the scan counter answers
"was this agent guarded", per turn, and a tool round-trip must not double it.
An intervention is recorded with the filter labels the trace named, so the
ledger can say *what* fired, not only which policy family.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from models.common import StreamRequest  # noqa: E402
from services.streaming_service import StreamingService  # noqa: E402
from test_stream_turn_identity import RUNTIME_ARN, VALUES, StubRegistry, StubThreadService  # noqa: E402
from test_streaming_guardrail import REAL_INPUT_BLOCKED, REAL_NO_ACTION  # noqa: E402


class GuardrailRecordingUsage:
    configured = True

    def __init__(self):
        self.events = []
        self.scans = []

    def resolve_model_for(self, **kwargs):
        return "global.anthropic.claude-sonnet-5"

    def record_turn(self, **kwargs):
        return {"event": "created", "cost_micros": 0}

    def record_guardrail_event(self, **kwargs):
        self.events.append(kwargs)

    def record_guardrail_scan(self, agent_record_id, owner_sub, date=None):
        self.scans.append((agent_record_id, owner_sub))


class GuardedTwoCallClient:
    """Both model calls carried a trace; only the first intervened."""

    async def execute_stream(self, thread_id, values, config=None, actor_id=None):
        yield {"event": {"messageStart": {"id": "m-1"}}}
        yield {"event": {"metadata": REAL_INPUT_BLOCKED}}
        yield {"event": {"contentBlockDelta": {"delta": {"text": "hi"}}}}
        yield {"event": {"metadata": REAL_NO_ACTION}}
        yield {"event": {"messageStop": {"stopReason": "end_turn"}}}


class UnguardedClient:
    async def execute_stream(self, thread_id, values, config=None, actor_id=None):
        yield {"event": {"messageStart": {"id": "m-1"}}}
        yield {"event": {"metadata": {"usage": {"inputTokens": 10, "outputTokens": 2}}}}
        yield {"event": {"messageStop": {"stopReason": "end_turn"}}}


def run(client, usage):
    service = StreamingService(
        thread_service=StubThreadService(), agentcore_client=client, usage_service=usage,
        registry_service=StubRegistry(),
    )
    service._get_agent_client = lambda config=None: client

    async def scenario():
        response = await service.stream_thread_execution(
            "t-1", StreamRequest(values=VALUES, config={"agent_runtime_arn": RUNTIME_ARN,
                                                        "registry_record_id": "rec-1"}),
            owner_sub="sub-1",
        )
        async for _ in response.body_iterator:
            pass

    asyncio.run(scenario())


def test_a_guarded_turn_is_scanned_once_however_many_model_calls():
    usage = GuardrailRecordingUsage()
    run(GuardedTwoCallClient(), usage)
    assert usage.scans == [("rec-1", "sub-1")]


def test_an_intervention_is_recorded_with_its_filter_labels():
    usage = GuardrailRecordingUsage()
    run(GuardedTwoCallClient(), usage)
    assert len(usage.events) == 1
    event = usage.events[0]
    assert event["agent_record_id"] == "rec-1" and event["owner_sub"] == "sub-1"
    assert event["action"] == "BLOCKED" and event["stage"] == "input"
    assert event["policies"] == ["content"]
    assert event["filter_types"] == ["INSULTS"]
    assert event["confidences"] == ["HIGH"]


def test_an_unguarded_turn_records_no_scan():
    usage = GuardrailRecordingUsage()
    run(UnguardedClient(), usage)
    assert usage.scans == [] and usage.events == []


def test_an_intervention_names_the_thread_and_turn_it_happened_on():
    """The stream is the only place that knows which turn a trace belongs to, so it
    must hand the ledger the thread and the same turn_id the turn event uses —
    that is what lets the recent-interventions list open the conversation."""
    usage = GuardrailRecordingUsage()
    run(GuardedTwoCallClient(), usage)
    [event] = usage.events
    assert event["thread_id"] == "t-1"
    assert event["turn_id"].startswith("t-1:")


class TurnRecordingUsage(GuardrailRecordingUsage):
    def __init__(self):
        super().__init__()
        self.turns = []

    def record_turn(self, **kwargs):
        self.turns.append(kwargs)
        return {"event": "created", "cost_micros": 0}


class BlockedAfterStopClient:
    """The real shape of an input block: the canned reply, messageStop, and only
    then the metadata — zero usage, a trace that says BLOCKED."""

    async def execute_stream(self, thread_id, values, config=None, actor_id=None):
        yield {"event": {"messageStart": {"id": "m-1"}}}
        yield {"event": {"contentBlockDelta": {"delta": {"text": "요청을 처리할 수 없습니다."}}}}
        yield {"event": {"messageStop": {"stopReason": "guardrail_intervened"}}}
        yield {"event": {"redactContent": {"redactUserContentMessage": "x"}}}
        yield {"event": {"metadata": REAL_INPUT_BLOCKED}}


class RuntimeErrorClient:
    """The runtime gave up (a Bedrock 503) and said so as an event; the stream
    then closed normally."""

    async def execute_stream(self, thread_id, values, config=None, actor_id=None):
        yield {"event": {"messageStart": {"id": "m-1"}}}
        yield {"event": {"error": {"error": "ServiceUnavailableException"}}}
        yield {"event": {"messageStop": {"stopReason": "error"}}}


def test_a_turn_blocked_before_the_model_ran_is_recorded_as_measured_and_blocked():
    usage = TurnRecordingUsage()
    run(BlockedAfterStopClient(), usage)
    assert sum(t["turns"] for t in usage.turns) == 1
    last = usage.turns[-1]
    assert last["usage_reported"] is True and last["blocked"] is True
    assert last["input_tokens"] == 0 and last["output_tokens"] == 0


def test_a_runtime_error_event_makes_the_turn_a_failure():
    usage = TurnRecordingUsage()
    run(RuntimeErrorClient(), usage)
    assert sum(t["turns"] for t in usage.turns) == 1
    assert any(t["failed"] for t in usage.turns)
