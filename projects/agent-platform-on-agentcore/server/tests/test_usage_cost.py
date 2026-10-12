"""Turning tokens into money without pretending to know more than we do.

The model is on neither the stream nor the registry record, and where it *is*
depends on how the agent was built — so `model_map` reads two places. A
harness-backed agent's model is a field on the harness; a runtime-backed agent's is
its runtime's `MODEL_ID` environment variable, because `InvokeAgentRuntime` accepts
no per-request override and the environment therefore decides every turn. The second
half is not an edge case: on the live account the busiest agent is runtime-backed.

Three failure modes matter more than the arithmetic:

* a model we have no rate for must come back as None, not 0.0;
* either lookup throwing must leave the other's answers and the token figures intact;
* a tier holding tokens with no rate must refuse the whole estimate rather than
  return a partial price and call it the cost.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.usage_service import UsageService  # noqa: E402


class StubHarness:
    def __init__(self, harnesses, exploding=False):
        self._harnesses = harnesses
        self.exploding = exploding
        self.calls = 0

    def list_harnesses(self, with_tools=False):
        self.calls += 1
        if self.exploding:
            raise RuntimeError("GetHarness timed out")
        return self._harnesses


class StubRecord:
    def __init__(self, record_id, harness_arn=None, name="agent"):
        self.record_id = record_id
        self.name = name
        self.harness_arn = harness_arn
        self.agent_runtime_arn = None


class StubRegistry:
    def __init__(self, records):
        self._records = records

    def agent_records(self):
        return self._records


class Harness:
    def __init__(self, harness_id, harness_arn, model_id, tools=None, skills=None):
        self.harness_id = harness_id
        self.harness_arn = harness_arn
        self.harness_name = harness_id
        self.model_id = model_id
        self.tools = tools or []
        self.skills = skills or []
        self.status = "READY"


ARN = "arn:aws:bedrock-agentcore:us-east-1:1:harness/writer-abc"


def service_with(harness, registry):
    return UsageService(repository=None, registry=registry, harness=harness)


def test_a_records_model_comes_from_the_harness_it_points_at():
    service = service_with(
        StubHarness([Harness("writer-abc", ARN, "global.anthropic.claude-sonnet-5")]),
        StubRegistry([StubRecord("rec-1", harness_arn=ARN)]),
    )

    assert service.model_map()["rec-1"] == "global.anthropic.claude-sonnet-5"


def test_a_record_pointing_at_nothing_has_no_model():
    """Neither a harness nor a runtime, so there is nowhere to read a model from."""
    service = service_with(
        StubHarness([]), StubRegistry([StubRecord("rec-2", harness_arn=None)])
    )

    assert "rec-2" not in service.model_map()


def test_the_harness_listing_is_fetched_once_per_window():
    harness = StubHarness([Harness("writer-abc", ARN, "anthropic.claude-sonnet-5")])
    service = service_with(harness, StubRegistry([StubRecord("rec-1", harness_arn=ARN)]))

    service.model_map()
    service.model_map()

    assert harness.calls == 1, (
        "GetHarness fan-out is slow; a second identical read in the same request "
        "must come from the cache"
    )


def test_a_harness_failure_yields_no_models_rather_than_raising():
    """Cost is optional; the token counts next to it are not."""
    service = service_with(
        StubHarness([], exploding=True),
        StubRegistry([StubRecord("rec-1", harness_arn=ARN)]),
    )

    assert service.model_map() == {}


def test_concurrent_calls_serialize_the_harness_fetch():
    """Multiple threads entering model_map() while cache is not fresh must fetch
    only once. The lock serializes them so the first fetches and others block
    and then find the cache fresh."""
    import threading

    class BlockingHarness:
        def __init__(self, harnesses):
            self._harnesses = harnesses
            self.calls = 0

        def list_harnesses(self, with_tools=False):
            self.calls += 1
            time.sleep(0.1)  # Block briefly to create race window
            return self._harnesses

    harness = BlockingHarness([Harness("writer-abc", ARN, "anthropic.claude-sonnet-5")])
    service = service_with(harness, StubRegistry([StubRecord("rec-1", harness_arn=ARN)]))

    results = []

    def call_model_map():
        results.append(service.model_map())

    threads = [threading.Thread(target=call_model_map) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert harness.calls == 1, (
        "All three threads should serialize behind the lock; "
        f"first fetches, others wait and get cached result, but got {harness.calls} calls"
    )
    assert all(r == {"rec-1": "anthropic.claude-sonnet-5"} for r in results)


class StubRuntime:
    def __init__(self, arn, model_id):
        self.agent_runtime_arn = arn
        self.model_id = model_id
        self.name = arn.split("/")[-1]


class RuntimeRegistry:
    """A registry that can also list runtimes, like the real one."""

    def __init__(self, records, runtimes=(), exploding=False):
        self._records = records
        self._runtimes = list(runtimes)
        self.exploding = exploding
        self.runtime_calls = 0

    def agent_records(self):
        return self._records

    def list_agent_runtimes(self):
        self.runtime_calls += 1
        if self.exploding:
            raise RuntimeError("GetAgentRuntime timed out")
        return self._runtimes


RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:1:runtime/bap_default-FgHiJ67890"


def runtime_backed(record_id="rec-2"):
    record = StubRecord(record_id, harness_arn=None)
    record.agent_runtime_arn = RUNTIME_ARN
    return record


def test_a_runtime_backed_agents_model_comes_from_its_environment():
    """The half of `model_map` that was missing, and it is not an edge case.

    A runtime-backed agent has no harness, and `InvokeAgentRuntime` takes no
    per-request model override — so the runtime's `MODEL_ID` environment variable is
    the model for every turn it serves. Measured on the live account,
    `bap_default` is runtime-backed, is the busiest agent by turns, and is the only
    one with recorded tokens: with the harness lookup alone every model cost on the
    page read "추정 불가" while the rates sat right there.
    """
    registry = RuntimeRegistry(
        [runtime_backed()],
        runtimes=[StubRuntime(RUNTIME_ARN, "global.anthropic.claude-haiku-4-5-20251001-v1:0")],
    )
    service = service_with(StubHarness([]), registry)

    assert service.model_map() == {
        "rec-2": "global.anthropic.claude-haiku-4-5-20251001-v1:0"
    }


def test_the_harness_wins_when_a_record_has_both():
    """A harness-backed record's companion runtime is AWS-managed, and its
    environment is not what the harness runs."""
    record = StubRecord("rec-1", harness_arn=ARN)
    record.agent_runtime_arn = RUNTIME_ARN
    registry = RuntimeRegistry(
        [record], runtimes=[StubRuntime(RUNTIME_ARN, "wrong.model")]
    )
    service = service_with(
        StubHarness([Harness("writer-abc", ARN, "global.anthropic.claude-sonnet-5")]),
        registry,
    )

    assert service.model_map()["rec-1"] == "global.anthropic.claude-sonnet-5"


def test_the_runtime_listing_is_skipped_when_no_record_needs_it():
    """It fans out `GetAgentRuntime` per runtime; an all-harness registry has no use
    for it and must not pay for it."""
    registry = RuntimeRegistry(
        [StubRecord("rec-1", harness_arn=ARN)],
        runtimes=[StubRuntime(RUNTIME_ARN, "some.model")],
    )
    service = service_with(
        StubHarness([Harness("writer-abc", ARN, "anthropic.claude-sonnet-5")]),
        registry,
    )

    service.model_map()

    assert registry.runtime_calls == 0


def test_one_resolver_failing_does_not_cost_the_other_its_answers():
    """A runtime-backed agent's model is still readable when the harness listing
    times out — they are independent lookups against different APIs."""
    registry = RuntimeRegistry(
        [StubRecord("rec-1", harness_arn=ARN), runtime_backed()],
        runtimes=[StubRuntime(RUNTIME_ARN, "global.anthropic.claude-haiku-4-5")],
    )
    service = service_with(StubHarness([], exploding=True), registry)

    models = service.model_map()

    assert models == {"rec-2": "global.anthropic.claude-haiku-4-5"}


def test_a_runtime_listing_failure_leaves_the_harness_answers_intact():
    registry = RuntimeRegistry(
        [StubRecord("rec-1", harness_arn=ARN), runtime_backed()], exploding=True
    )
    service = service_with(
        StubHarness([Harness("writer-abc", ARN, "anthropic.claude-sonnet-5")]),
        registry,
    )

    assert service.model_map() == {"rec-1": "anthropic.claude-sonnet-5"}
