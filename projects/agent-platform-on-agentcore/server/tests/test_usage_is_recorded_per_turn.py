"""Tokens already reach this server and are thrown away.

`harness_event_adapter._on_metadata` and `agentcore_client` both parse
inputTokens/outputTokens into a `metadata` stream event, and nothing reads it.
These tests pin the three things that make recording it trustworthy: it lands
once, it lands even when the connection dies mid-turn, and the post-loop write
does not double count it.
"""
import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.common import StreamConfig, StreamRequest  # noqa: E402
from services.streaming_service import StreamingService  # noqa: E402

VALUES = {"messages": [{"id": "u-1", "type": "human", "content": "hello"}]}
# `stream_thread_execution` reads the agent from `request.config`, not from an
# argument. The stub thread carries the same id so the approval gate is skipped:
# it only fires for a turn that would *bind* a thread to a new agent.
REQUEST = StreamRequest(
    values=VALUES, config=StreamConfig(registry_record_id="rec-1")
)


RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:1:runtime/r-1"


class StubRegistry:
    """Resolves rec-1 so the target binding (services/agent_access.py) needs no registry."""

    def get_record(self, record_id):
        return SimpleNamespace(status="APPROVED", agent_runtime_arn=RUNTIME_ARN, harness_arn=None, qualifier=None)
    def chattable_record(self, record_id):
        # The approval gate reads the chattable revision; these doubles have one.
        return self.get_record(record_id)


class StubThread:
    def __init__(self, values):
        self.values = values
        self.updated_at = ""
        self.agent_record_id = "rec-1"


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
        return None


class RecordingUsage:
    configured = True

    def __init__(self):
        self.turns = []

    def record_turn(self, **kwargs):
        self.turns.append(kwargs)


class OneTurnClient:
    """A complete turn: text, one tool call, usage, then messageStop."""

    async def execute_stream(self, thread_id, values, config=None, actor_id=None):
        yield {"event": {"messageStart": {"id": "m-1"}}}
        yield {"event": {"contentBlockDelta": {"delta": {"text": "Hi"}}}}
        yield {"event": {"contentBlockStart": {
            "start": {"toolUse": {"toolUseId": "tu-1", "name": "WebSearch"}}}}}
        yield {"event": {"contentBlockStop": {}}}
        yield {"event": {"metadata": {"usage": {
            "inputTokens": 100, "outputTokens": 20, "totalTokens": 120}}}}
        yield {"event": {"messageStop": {"messageId": "m-1", "fullText": "Hi"}}}


class UsageThenHangingClient:
    """Spends tokens, then goes silent — a harness mid-tool, connection severed."""

    async def execute_stream(self, thread_id, values, config=None, actor_id=None):
        yield {"event": {"messageStart": {"id": "m-1"}}}
        yield {"event": {"contentBlockDelta": {"delta": {"text": "Researching"}}}}
        yield {"event": {"metadata": {"usage": {
            "inputTokens": 80, "outputTokens": 5, "totalTokens": 85}}}}
        await asyncio.sleep(3600)


def service_with(client):
    threads = StubThreadService()
    usage = RecordingUsage()
    service = StreamingService(
        thread_service=threads, agentcore_client=client, usage_service=usage,
        registry_service=StubRegistry(),
    )
    service._get_agent_client = lambda config=None: client
    return service, usage


def drain_all(service):
    async def scenario():
        response = await service.stream_thread_execution(
            "t-1", REQUEST, owner_sub="sub-1"
        )
        async for _ in response.body_iterator:
            pass

    asyncio.run(scenario())


def drain_then_disconnect(service, frames):
    async def scenario():
        response = await service.stream_thread_execution(
            "t-1", REQUEST, owner_sub="sub-1"
        )
        agen = response.body_iterator.__aiter__()
        for _ in range(frames):
            await agen.__anext__()
        await agen.aclose()

    asyncio.run(scenario())


def test_a_completed_turn_records_its_tokens_once():
    service, usage = service_with(OneTurnClient())

    drain_all(service)

    assert len(usage.turns) == 1, (
        f"expected one flush, got {len(usage.turns)} — the post-loop write is "
        "double counting"
    )
    turn = usage.turns[0]
    assert turn["input_tokens"] == 100
    assert turn["output_tokens"] == 20
    assert turn["agent_record_id"] == "rec-1"
    assert turn["owner_sub"] == "sub-1"


def test_a_completed_turn_records_the_tools_it_called():
    service, usage = service_with(OneTurnClient())

    drain_all(service)

    assert usage.turns[0]["tool_calls"] == {"WebSearch": 1}


def test_an_interrupted_turn_still_records_the_tokens_it_burned():
    """They are already on the invoice. Cost that exists only on the bill and not
    on the dashboard is exactly what makes the dashboard untrustworthy."""
    service, usage = service_with(UsageThenHangingClient())

    drain_then_disconnect(service, frames=4)

    assert len(usage.turns) == 1
    assert usage.turns[0]["input_tokens"] == 80
    assert usage.turns[0]["interrupted"] is True


def test_a_turn_that_produced_no_message_still_records_its_tokens():
    """`_persist_turn` returns early with nothing to store — but the tokens were
    spent anyway, which is why the flush is a sibling call and not a line inside
    it."""

    class TokensOnlyClient:
        async def execute_stream(self, thread_id, values, config=None, actor_id=None):
            yield {"event": {"metadata": {"usage": {
                "inputTokens": 40, "outputTokens": 0, "totalTokens": 40}}}}

    service, usage = service_with(TokensOnlyClient())

    drain_all(service)

    assert len(usage.turns) == 1
    assert usage.turns[0]["input_tokens"] == 40


def test_recording_survives_an_unconfigured_usage_service():
    threads = StubThreadService()
    client = OneTurnClient()
    service = StreamingService(
        thread_service=threads, agentcore_client=client, registry_service=StubRegistry()
    )
    service._get_agent_client = lambda config=None: client

    async def scenario():
        response = await service.stream_thread_execution(
            "t-1", REQUEST, owner_sub="sub-1"
        )
        async for _ in response.body_iterator:
            pass

    asyncio.run(scenario())


def test_usage_from_several_model_calls_is_summed_not_maxed():
    """Measured 2026-08-15: each metadata event is one model call's usage, not a
    running total — harness outputTokens decrease across events, which a cumulative
    figure cannot do. Bedrock bills every call's prompt, so a turn's cost is the sum.
    Taking the max undercounted a real 18-call turn 4.5x."""

    class MultiCallClient:
        async def execute_stream(self, thread_id, values, config=None, actor_id=None):
            yield {"event": {"messageStart": {"id": "m-1"}}}
            for input_tokens, output_tokens in ((10587, 122), (10731, 104), (10858, 86)):
                yield {"event": {"metadata": {"usage": {
                    "inputTokens": input_tokens,
                    "outputTokens": output_tokens,
                    "totalTokens": input_tokens + output_tokens,
                }}}}
            yield {"event": {"messageStop": {"messageId": "m-1", "fullText": "done"}}}

    service, usage = service_with(MultiCallClient())

    drain_all(service)

    assert len(usage.turns) == 1
    assert usage.turns[0]["input_tokens"] == 32176, "max() would give 10858"
    assert usage.turns[0]["output_tokens"] == 312, "max() would give 122"


def test_a_request_flushing_multiple_times_records_turns_once():
    """Turns count requests, not flushes. A request that produces several
    messageStop events (or ends mid-loop and flushes both post-loop and on
    cancellation) must record turns=1 total, not once per flush. The live
    table showed turns=3 for two requests: one normal, one severed.

    In this test, the first flush records turns=1, the second records turns=0
    (which the real repository drops). We check that exactly one flush has
    turns > 0, and that total is 1.
    """

    class MultiStopClient:
        """A runtime that emits messageStop twice per turn (measured behavior)."""

        async def execute_stream(self, thread_id, values, config=None, actor_id=None):
            yield {"event": {"messageStart": {"id": "m-1"}}}
            yield {"event": {"contentBlockDelta": {"delta": {"text": "Hi"}}}}
            yield {"event": {"metadata": {"usage": {
                "inputTokens": 100, "outputTokens": 20, "totalTokens": 120}}}}
            # First messageStop with interrupted=False
            yield {"event": {"messageStop": {"messageId": "m-1", "fullText": "Hi"}}}
            # After stopping, more events arrive (observed on live harness)
            yield {"event": {"messageStart": {"id": "m-1"}}}
            yield {"event": {"contentBlockDelta": {"delta": {"text": " there"}}}}
            yield {"event": {"metadata": {"usage": {
                "inputTokens": 5, "outputTokens": 5, "totalTokens": 10}}}}
            # Second messageStop
            yield {"event": {"messageStop": {"messageId": "m-1", "fullText": "Hi there"}}}

    service, usage = service_with(MultiStopClient())

    drain_all(service)

    # Multiple flushes happen, but only the first one should have turns > 0
    turns_recorded = [t["turns"] for t in usage.turns if t.get("turns", 0) > 0]
    assert len(turns_recorded) == 1, (
        f"expected one flush with turns > 0, got {len(turns_recorded)} — "
        "multiple flushes in one request recorded turns each time"
    )
    assert sum(turns_recorded) == 1, (
        f"total turns should be 1, got {sum(turns_recorded)}"
    )


def test_thread_started_is_true_only_on_first_flush_of_first_turn():
    """thread_started distinguishes a thread's first turn from later ones,
    so it must be True exactly once: on the first flush, if this is the
    thread's first turn (no prior assistant message in stored transcript).
    """

    class SimpleTurnClient:
        async def execute_stream(self, thread_id, values, config=None, actor_id=None):
            yield {"event": {"messageStart": {"id": "m-1"}}}
            yield {"event": {"contentBlockDelta": {"delta": {"text": "Hi"}}}}
            yield {"event": {"metadata": {"usage": {
                "inputTokens": 100, "outputTokens": 20, "totalTokens": 120}}}}
            yield {"event": {"messageStop": {"messageId": "m-1", "fullText": "Hi"}}}

    service, usage = service_with(SimpleTurnClient())

    drain_all(service)

    assert len(usage.turns) == 1
    assert usage.turns[0]["thread_started"] is True, (
        "first turn on a thread must mark thread_started=True"
    )


def test_thread_started_is_false_on_second_turn():
    """A turn on a thread that already has an assistant message is not
    a thread start."""

    class SimpleTurnClient:
        async def execute_stream(self, thread_id, values, config=None, actor_id=None):
            yield {"event": {"messageStart": {"id": "m-2"}}}
            yield {"event": {"contentBlockDelta": {"delta": {"text": "Okay"}}}}
            yield {"event": {"metadata": {"usage": {
                "inputTokens": 50, "outputTokens": 10, "totalTokens": 60}}}}
            yield {"event": {"messageStop": {"messageId": "m-2", "fullText": "Okay"}}}

    # Start with a thread that already has an AI message
    threads = StubThreadService()
    threads.thread.values["messages"] = [
        {"id": "u-1", "type": "human", "content": "hello"},
        {"id": "m-1", "type": "ai", "content": "Hi there"},
    ]
    usage = RecordingUsage()
    service = StreamingService(
        thread_service=threads, agentcore_client=SimpleTurnClient(), usage_service=usage,
        registry_service=StubRegistry(),
    )
    service._get_agent_client = lambda config=None: SimpleTurnClient()

    drain_all(service)

    assert len(usage.turns) == 1
    assert usage.turns[0]["thread_started"] is False, (
        "second turn on a thread must mark thread_started=False"
    )


def test_a_failed_turn_records_the_turn_and_the_tokens_it_burned():
    """The path that recorded nothing at all.

    A turn that dies on a model error or a runtime 5xx used to reach neither
    `_persist_turn` nor `_flush_usage`: the partial answer was discarded, the tokens
    already spent vanished, and the turn never appeared in any count. So the
    leaderboard undercounted traffic and spend by however many turns failed, and the
    platform had no failure figure of its own — "중단된 턴" counts client disconnects
    only, which left the first number an operator asks for answerable exclusively
    from the metered CloudWatch tier.
    """

    class FailsMidTurnClient:
        async def execute_stream(self, thread_id, values, config=None, actor_id=None):
            yield {"event": {"messageStart": {"id": "m-1"}}}
            yield {"event": {"contentBlockDelta": {"delta": {"text": "Working"}}}}
            yield {"event": {"metadata": {"usage": {
                "inputTokens": 60, "outputTokens": 9, "totalTokens": 69}}}}
            raise RuntimeError("ModelStreamErrorException")

    service, usage = service_with(FailsMidTurnClient())

    drain_all(service)

    assert len(usage.turns) == 1, "a failed turn is still a turn"
    turn = usage.turns[0]
    assert turn["turns"] == 1
    assert turn["input_tokens"] == 60
    assert turn["output_tokens"] == 9
    assert turn["failed"] is True
    # Two distinct endings, not one "did not finish": a client hanging up is a fact
    # about a reader and a model error is a fact about the platform.
    assert turn["interrupted"] is False


def test_a_failed_turn_keeps_whatever_the_agent_had_already_said():
    """Same recovery as the cancellation path, which this one lacked entirely."""

    class FailsAfterTextClient:
        async def execute_stream(self, thread_id, values, config=None, actor_id=None):
            yield {"event": {"messageStart": {"id": "m-1"}}}
            yield {"event": {"contentBlockDelta": {"delta": {"text": "Half an answer"}}}}
            raise RuntimeError("boom")

    threads = StubThreadService()
    usage = RecordingUsage()
    client = FailsAfterTextClient()
    service = StreamingService(
        thread_service=threads, agentcore_client=client, usage_service=usage,
        registry_service=StubRegistry(),
    )
    service._get_agent_client = lambda config=None: client

    async def scenario():
        response = await service.stream_thread_execution(
            "t-1", REQUEST, owner_sub="sub-1"
        )
        async for _ in response.body_iterator:
            pass

    asyncio.run(scenario())

    stored = [
        message
        for message in threads.thread.values["messages"]
        if message.get("type") == "ai"
    ]
    assert stored and stored[0]["content"] == "Half an answer"


def test_the_cache_tiers_are_recorded_apart_from_plain_input():
    """`inputTokens` does not include them, and they are not billed like them.

    Converse's `TokenUsage` documents `inputTokens` as "the number of tokens sent in
    the request to the model" and carries `cacheReadInputTokens` as its own field, so
    reading only the first two undercounts the prompt on any path where caching is
    on — silently, because the day still carries a token attribute and nothing marks
    the total as a floor. Recording them apart is also what makes a model cost
    possible: Bedrock bills a cache read at roughly a tenth of an uncached input
    token, so one combined counter priced at one rate is off by that factor.
    """

    class CachingClient:
        async def execute_stream(self, thread_id, values, config=None, actor_id=None):
            yield {"event": {"messageStart": {"id": "m-1"}}}
            yield {"event": {"contentBlockDelta": {"delta": {"text": "Hi"}}}}
            yield {"event": {"metadata": {"usage": {
                "inputTokens": 300,
                "outputTokens": 20,
                "cacheReadInputTokens": 18000,
                "cacheWriteInputTokens": 1200,
                "totalTokens": 320,
            }}}}
            yield {"event": {"messageStop": {"messageId": "m-1", "fullText": "Hi"}}}

    service, usage = service_with(CachingClient())

    drain_all(service)

    turn = usage.turns[0]
    assert turn["input_tokens"] == 300, "cache reads must not inflate plain input"
    assert turn["cache_read_tokens"] == 18000
    assert turn["cache_write_tokens"] == 1200


def test_cache_tiers_accumulate_across_a_multi_call_turn():
    """Each metadata event is one model call, not a running total."""

    class TwoCallClient:
        async def execute_stream(self, thread_id, values, config=None, actor_id=None):
            yield {"event": {"messageStart": {"id": "m-1"}}}
            yield {"event": {"metadata": {"usage": {
                "inputTokens": 10, "cacheReadInputTokens": 100}}}}
            yield {"event": {"metadata": {"usage": {
                "inputTokens": 5, "cacheReadInputTokens": 250,
                "cacheWriteInputTokens": 7}}}}
            yield {"event": {"messageStop": {"messageId": "m-1", "fullText": ""}}}

    service, usage = service_with(TwoCallClient())

    drain_all(service)

    turn = usage.turns[0]
    assert turn["input_tokens"] == 15
    assert turn["cache_read_tokens"] == 350
    assert turn["cache_write_tokens"] == 7


def test_a_turn_that_only_read_cache_is_still_a_measured_turn():
    """The flush guard has to count the cache tiers or the turn looks unmeasured.

    A turn served entirely from cache reports no `inputTokens` at all. Left out of
    the "did this spend anything" test, its tokens would be dropped and the day would
    be marked unmeasured — which renders as "모름" over a turn we measured exactly.
    """

    class CacheOnlyClient:
        async def execute_stream(self, thread_id, values, config=None, actor_id=None):
            yield {"event": {"metadata": {"usage": {"cacheReadInputTokens": 9000}}}}

    service, usage = service_with(CacheOnlyClient())

    drain_all(service)

    assert len(usage.turns) == 1
    assert usage.turns[0]["cache_read_tokens"] == 9000
