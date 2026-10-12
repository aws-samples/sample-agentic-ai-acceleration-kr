"""Per-runtime error alarms, driven by the metric index the dashboard already builds.

Two measured facts drive the design and both are tested here:

- A partial dimension set returns zero datapoints with no error (fact 18), so an
  alarm must use the *most specific* discovered set — the one that actually has
  data. Alarming on the widest set produces INSUFFICIENT_DATA forever, which
  looks exactly like health.
- `HarnessId` appears both bare and as a full ARN in separate series (fact 19),
  so the same series is reachable under more than one index key. Deduplication is
  by dimension set, not by ARN.
"""
import scripts.sync_agent_alarms as mod

RUNTIME = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/bap_default-FgHiJ67890"
HARNESS = "arn:aws:bedrock-agentcore:us-east-1:123456789012:harness/academic_writer-TuVwX10293"


def series(metric_name, dimensions):
    return {
        "Namespace": "AWS/Bedrock-AgentCore",
        "MetricName": metric_name,
        "Dimensions": [{"Name": n, "Value": v} for n, v in dimensions],
    }


WIDE = series("SystemErrors", [
    ("Operation", "InvokeAgentRuntime"),
    ("Resource", RUNTIME),
])
NARROW = series("SystemErrors", [
    ("Operation", "InvokeAgentRuntime"),
    ("Resource", RUNTIME),
    ("Name", "bap_default::DEFAULT"),
])


class StubCloudWatch:
    def __init__(self, existing=()):
        self.existing = list(existing)
        self.put = []
        self.deleted = []

    def get_paginator(self, name):
        assert name == "describe_alarms"
        existing = self.existing

        class Paginator:
            def paginate(self, **params):
                prefix = params.get("AlarmNamePrefix", "")
                yield {
                    "MetricAlarms": [
                        {"AlarmName": n} for n in existing if n.startswith(prefix)
                    ]
                }

        return Paginator()

    def put_metric_alarm(self, **params):
        self.put.append(params)

    def delete_alarms(self, **params):
        self.deleted.extend(params["AlarmNames"])


def test_the_most_specific_dimension_set_wins():
    """The widest set had zero datapoints in the live account; the narrow one had 7.

    `WIDE` carries no `Name`, which is exactly the shape of the partial rollup
    fact 18 measured as empty, so it is dropped outright rather than competing.
    """
    desired = mod.desired_alarms({RUNTIME: [WIDE, NARROW]})

    assert len(desired) == 1
    only = next(iter(desired.values()))
    names = {d["Name"] for d in only["Dimensions"]}
    assert names == {"Operation", "Resource", "Name"}


def test_a_series_without_a_name_dimension_gets_no_alarm_at_all():
    """Fact 18: those are the rollups CloudWatch does not populate. An alarm on an
    empty series sits in INSUFFICIENT_DATA looking exactly like health, and costs
    $0.10 a month to do it."""
    assert mod.desired_alarms({RUNTIME: [WIDE]}) == {}


def test_one_agent_gets_one_alarm_per_call_path():
    """Measured 2026-08-16: a harness agent has SystemErrors series under both
    `InvokeAgentRuntimeCommand` (our harness path) and `InvokeAgentRuntime` (a
    direct runtime invoke). They are different failures, so both are watched — and
    the alarm names must say which is which rather than differing only by digest.
    """
    command = series("SystemErrors", [
        ("Resource", RUNTIME),
        ("EndpointQualifier", "DEFAULT"),
        ("HarnessId", HARNESS),
        ("Operation", "InvokeAgentRuntimeCommand"),
        ("Name", "harness_academic_writer::DEFAULT"),
    ])
    direct = series("SystemErrors", [
        ("Resource", RUNTIME),
        ("EndpointQualifier", "DEFAULT"),
        ("HarnessId", HARNESS),
        ("Operation", "InvokeAgentRuntime"),
        ("Name", "harness_academic_writer::DEFAULT"),
    ])
    # The same two series are reachable under the runtime ARN and the harness ARN.
    desired = mod.desired_alarms({RUNTIME: [command, direct], HARNESS: [command, direct]})

    assert len(desired) == 2
    names = sorted(desired)
    assert any("InvokeAgentRuntimeCommand" in n for n in names)
    assert any(n.endswith(tuple()) or "InvokeAgentRuntime-" in n for n in names)
    # Distinguishable without reading the digest.
    assert len({n.rsplit("-", 1)[0] for n in names}) == 2


def test_one_series_reachable_under_two_arns_yields_one_alarm():
    both = series("SystemErrors", [
        ("Operation", "InvokeAgentRuntimeCommand"),
        ("Resource", RUNTIME),
        ("HarnessId", HARNESS),
        ("Name", "harness_academic_writer::DEFAULT"),
    ])
    desired = mod.desired_alarms({RUNTIME: [both], HARNESS: [both]})

    assert len(desired) == 1


def test_other_metrics_are_ignored():
    desired = mod.desired_alarms({RUNTIME: [series("Invocations", [("Resource", RUNTIME)])]})
    assert desired == {}


def test_alarm_names_are_prefixed_and_stable():
    desired = mod.desired_alarms({RUNTIME: [NARROW]})
    name = next(iter(desired))
    assert name.startswith(mod.ALARM_PREFIX)
    # Stable across runs, so a re-run updates in place instead of accumulating.
    again = next(iter(mod.desired_alarms({RUNTIME: [NARROW]})))
    assert name == again


def test_dry_run_writes_nothing():
    stub = StubCloudWatch()
    result = mod.sync(stub, {RUNTIME: [NARROW]}, "arn:aws:sns:us-east-1:1:t", apply=False)

    assert len(result["created"]) == 1
    assert stub.put == []
    assert stub.deleted == []


def test_apply_creates_the_alarm_with_the_topic_as_its_action():
    stub = StubCloudWatch()
    mod.sync(stub, {RUNTIME: [NARROW]}, "arn:aws:sns:us-east-1:1:t", apply=True)

    assert len(stub.put) == 1
    params = stub.put[0]
    assert params["AlarmActions"] == ["arn:aws:sns:us-east-1:1:t"]
    assert params["Namespace"] == "AWS/Bedrock-AgentCore"
    assert params["MetricName"] == "SystemErrors"
    assert params["TreatMissingData"] == "notBreaching"


def test_an_alarm_whose_runtime_disappeared_is_deleted():
    stale = mod.ALARM_PREFIX + "gone-forever"
    stub = StubCloudWatch(existing=[stale])
    result = mod.sync(stub, {RUNTIME: [NARROW]}, "arn:aws:sns:us-east-1:1:t", apply=True)

    assert result["deleted"] == [stale]
    assert stub.deleted == [stale]


def test_alarms_outside_the_prefix_are_never_touched():
    """Shared account. The script must not be able to delete someone else's alarm."""
    stub = StubCloudWatch(existing=["someone-elses-critical-alarm", mod.ALARM_PREFIX + "old"])
    result = mod.sync(stub, {}, "arn:aws:sns:us-east-1:1:t", apply=True)

    assert result["deleted"] == [mod.ALARM_PREFIX + "old"]
    assert "someone-elses-critical-alarm" not in stub.deleted


def test_an_existing_alarm_is_kept_and_still_rewritten():
    """PutMetricAlarm is an upsert, so a kept alarm is refreshed rather than
    skipped — the topic ARN or threshold may have changed since it was created."""
    stub = StubCloudWatch(existing=[next(iter(mod.desired_alarms({RUNTIME: [NARROW]})))])
    result = mod.sync(stub, {RUNTIME: [NARROW]}, "arn:aws:sns:us-east-1:1:t", apply=True)

    assert result["kept"] and not result["created"]
    assert len(stub.put) == 1


def test_a_paginator_that_ignores_the_prefix_still_cannot_get_an_alarm_deleted():
    """Defence in depth on a delete path, in a shared account.

    `_existing` passes `AlarmNamePrefix` *and* re-filters the result. Without the
    second filter, this test deletes `someone-elses-critical-alarm`: the guarantee
    would rest on CloudWatch honouring the parameter rather than on this code.
    """
    class IgnoresPrefix(StubCloudWatch):
        def get_paginator(self, name):
            existing = self.existing

            class Paginator:
                def paginate(self, **_params):
                    yield {"MetricAlarms": [{"AlarmName": n} for n in existing]}

            return Paginator()

    stub = IgnoresPrefix(existing=["someone-elses-critical-alarm"])
    result = mod.sync(stub, {}, "arn:aws:sns:us-east-1:1:t", apply=True)

    assert result["deleted"] == []
    assert stub.deleted == []


# --- registry scoping -------------------------------------------------------
#
# Measured 2026-08-16: an unscoped run over this shared account wanted 46 alarms,
# most of them other teams' runtimes, one a gateway and one a single gateway tool.
# CloudWatch charges $0.10 per alarm per month, and being paged because someone
# else's agent erred is noise. So the index is intersected with what the registry
# binds.

GATEWAY = "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/bap-gateway-gwexample04"
SOMEONE_ELSE = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/cardnews_agent-JkLmN77889"


class StubRegistry:
    def __init__(self, arns):
        self.arns = list(arns)

    def agent_records_by_arn(self):
        # Value shape is irrelevant here; only the keys are read.
        return {arn: object() for arn in self.arns}


def test_scope_keeps_only_the_arns_the_registry_binds():
    index = {RUNTIME: [NARROW], SOMEONE_ELSE: [WIDE], GATEWAY: [WIDE]}
    scoped = mod.scope_to(index, [RUNTIME])

    assert set(scoped) == {RUNTIME}


def test_scope_covers_both_arn_forms_a_harness_record_carries():
    """A harness-backed record binds its harness ARN and its companion runtime
    ARN, and the metric index reaches the same series under either."""
    index = {RUNTIME: [NARROW], HARNESS: [NARROW]}
    scoped = mod.scope_to(index, [RUNTIME, HARNESS])

    assert set(scoped) == {RUNTIME, HARNESS}
    # Still one alarm: dedup is by dimension set, not by ARN.
    assert len(mod.desired_alarms(scoped)) == 1


def test_an_empty_registry_scopes_to_nothing_not_to_everything():
    """'The registry told us about nothing' must never mean 'alarm on the whole
    account' — that is the 46-alarm outcome, arrived at silently."""
    index = {RUNTIME: [NARROW], SOMEONE_ELSE: [WIDE]}

    assert mod.scope_to(index, []) == {}


def test_registry_arns_reads_both_forms_from_the_index():
    arns = mod.registry_arns(StubRegistry([RUNTIME, HARNESS]))

    assert set(arns) == {RUNTIME, HARNESS}
