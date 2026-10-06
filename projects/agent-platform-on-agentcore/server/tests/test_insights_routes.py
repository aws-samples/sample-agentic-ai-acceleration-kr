"""The routes, and the failure shapes that matter.

Unconfigured usage table -> 501, matching knowledge: the request was fine and
auth succeeded, the feature simply is not provisioned here.

CloudWatch missing -> **200 with sources.cloudwatch false** on `/telemetry`, not
501 and not a 5xx. And `/summary` must not go near CloudWatch at all: it is the
route the page polls once a minute, and `GetMetricData` bills per metric
requested — measured 279 metrics a poll, $4.02 a day per open tab.

No cost estimate may appear without a tariff behind it — which is a rule about
provenance, not a ban. The model estimate is served again because its rates are
now *derived from the account's own bill* (`UnblendedCost / UsageQuantity` per
model and tier) rather than transcribed from a price page, and because the token
counts behind it separate the four tiers Bedrock bills at four rates. It is absent,
never zero, when any tier holding tokens has no rate.

The runtime estimate is refused unless **both** vended hours are present. Reading
them with a `0.0` default priced an agent CloudWatch had no resource series for at
exactly `$0.0000`, which is the one confusion this module exists to prevent.

Reconciliation compares **hours**, not dollars. The billed side is the account's
whole runtime bill and the measured side is our registry's agents, so a dollar
ratio divides two populations; on hours the rate drops out and the gap means
coverage.

Per-user detail -> owner-or-admin, matching thread ownership. The per-user
*leaderboard* is admin-only: it names people rather than public records.

**Billing and pricing are stubbed by default** (`wire`). Without an override the
route reaches the real Cost Explorer — $0.01 a call against numbers another team's
spending changes daily.
"""
import os
import sys
from datetime import datetime, timedelta
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException  # noqa: E402

import routes.insights as insights  # noqa: E402
from core.auth import AuthUser  # noqa: E402
from services.billing_service import BillingUnavailable  # noqa: E402
from services.telemetry_service import TelemetryUnavailable  # noqa: E402
from services.usage_service import UsageService  # noqa: E402

# Derive today's date at module level to keep tests invariant as time passes.
_today = datetime.utcnow().date()
_today_str = _today.strftime("%Y-%m-%d")
_current_month = _today.strftime("%Y-%m")
_seven_days_ago = (_today - timedelta(days=6)).strftime("%Y-%m-%d")
_thirty_days_ago = (_today - timedelta(days=29)).strftime("%Y-%m-%d")


class StubRepo:
    def __init__(self, items_by_pk=None):
        self.items_by_pk = items_by_pk or {}

    def query_prefix(self, pk, sk_prefix):
        # Event partitions (TURNS#, GUARDRAIL_EVENTS#) are read by prefix. The
        # real repository has this; a stub without it made every event read look
        # like a failed read and flipped `sources.usage` to false.
        return [item for item in self.items_by_pk.get(pk, []) if str(item.get("sk", "")).startswith(sk_prefix)]

    def query(self, pk, start_date, end_date):
        items = self.items_by_pk.get(pk, [])
        # Filter by date range: items with sk like "D#YYYY-MM-DD#..." should fall within [start_date, end_date]
        result = []
        for item in items:
            sk = item.get("sk", "")
            # Extract date from sort key format "D#YYYY-MM-DD#..."
            parts = sk.split("#")
            if len(parts) >= 2:
                item_date = parts[1]
                if start_date <= item_date <= end_date:
                    result.append(item)
        return result


class StubRecord:
    def __init__(self, record_id, name):
        self.record_id = record_id
        self.name = name
        self.harness_arn = f"arn:aws:bedrock-agentcore:us-east-1:1:harness/{record_id}"
        self.agent_runtime_arn = None
        self.descriptor_type = "A2A"


class StubRegistry:
    def __init__(self, records):
        self._records = records

    def agent_records(self):
        return self._records

    def list_records(self, descriptor_type=None, **kwargs):
        return []


class StubHarness:
    def __init__(self, models=None):
        self.models = models or {}

    def list_harnesses(self, with_tools=False):
        class H:
            pass

        out = []
        for record_id, model_id in self.models.items():
            harness = H()
            harness.harness_arn = (
                f"arn:aws:bedrock-agentcore:us-east-1:1:harness/{record_id}"
            )
            harness.harness_id = record_id
            harness.harness_name = record_id
            harness.model_id = model_id
            harness.tools = []
            harness.skills = []
            out.append(harness)
        return out


class StubTelemetry:
    """Counts its reads: a metered call made by the polled route is the bug."""

    def __init__(self, metrics=None, unavailable=False):
        self.metrics = metrics or {}
        self.unavailable = unavailable
        self.calls = 0

    def agent_metrics(self, arns, start, end):
        self.calls += 1
        if self.unavailable:
            raise TelemetryUnavailable("AccessDenied")
        return self.metrics


def user(sub="sub-1", is_admin=False):
    """Create a real AuthUser instance for testing."""
    groups = ["admin"] if is_admin else []
    return AuthUser(sub=sub, username=sub, groups=groups)


class SilentBilling:
    """A Cost Explorer that has nothing to say, and never calls AWS.

    Wired by default because without an override `_billing()` returns the process
    instance, which makes a real `GetCostAndUsage` — $0.01 a call, answers that
    change daily, and a suite that passes or fails depending on what another team
    billed the shared account for. It surfaced the moment `/summary` started
    deriving model rates: the "unpriced" test began seeing a real price.
    """

    def agentcore_costs(self, start_date, end_date):
        raise BillingUnavailable("no billing in tests")

    def per_agent_costs(self, start_date, end_date):
        raise BillingUnavailable("no billing in tests")

    def model_rates(self, start_date, end_date):
        raise BillingUnavailable("no billing in tests")

    def model_costs_by_agent(self, start_date, end_date):
        raise BillingUnavailable("no billing in tests")


class SilentPricing:
    """The Price List API, unreachable, so the pinned rates are used.

    Free to call in production, but still a network round trip, and a test that
    reaches the network is a test that fails on a plane.
    """

    def runtime_rates(self):
        from data.model_prices import RUNTIME_GB_HOUR_USD, RUNTIME_VCPU_HOUR_USD

        return {
            "vcpu_hour": RUNTIME_VCPU_HOUR_USD,
            "gb_hour": RUNTIME_GB_HOUR_USD,
            "source": "pinned",
        }


def wire(usage, telemetry):
    insights._usage_override = usage
    insights._telemetry_override = telemetry
    # Silent *unless already set*, so a test can install its own billing stub either
    # side of this call. `teardown_function` clears both, so "currently None" is a
    # reliable signal per test rather than order-dependent state.
    if insights._billing_override is None:
        insights._billing_override = SilentBilling()
    if insights._pricing_override is None:
        insights._pricing_override = SilentPricing()
    # Same shape for the directory: without an override the process instance would
    # make a real Cognito ListUsers per subject.
    if insights._directory_override is None:
        insights._directory_override = StubDirectory()


def teardown_function():
    insights._usage_override = None
    insights._telemetry_override = None
    insights._billing_override = None
    insights._pricing_override = None
    insights._directory_override = None
    insights._threads_override = None


class StubDirectory:
    """sub -> email, recording what was asked. Empty by default: the pool is not
    reachable from a test, and a test that wants names says which."""

    def __init__(self, emails=None):
        self.emails_by_sub = emails or {}
        self.asked = []

    def emails(self, subs):
        subs = [s for s in subs if s]
        self.asked.append(sorted(set(subs)))
        return {s: self.emails_by_sub[s] for s in subs if s in self.emails_by_sub}


class RaisingDirectory:
    def emails(self, subs):
        raise RuntimeError("directory down")


def usage_with_one_agent(items=None):
    return UsageService(
        repository=StubRepo(items if items is not None else {
            f"AGENTS#{_current_month}": [
                {"sk": f"D#{_today_str}#A#rec-1", "turns": Decimal(7),
                 "input_tokens": Decimal(25324), "output_tokens": Decimal(542),
                 "tool_calls": Decimal(7), "interrupted_turns": Decimal(3)},
            ],
            f"AGENT#rec-1#USERS#{_current_month}": [
                {"sk": f"D#{_today_str}#U#sub-1", "turns": Decimal(7)},
            ],
        }),
        registry=StubRegistry([StubRecord("rec-1", "기본 에이전트")]),
        harness=StubHarness({"rec-1": "global.anthropic.claude-sonnet-5"}),
    )


def test_an_unconfigured_usage_table_is_501():
    wire(UsageService(repository=None), StubTelemetry())

    try:
        insights.summary(days=7, user=user(is_admin=True))
    except HTTPException as exc:
        assert exc.status_code == 501
        return
    raise AssertionError("expected 501")


def test_the_leaderboard_carries_our_figures_and_the_agent_name():
    wire(usage_with_one_agent(), StubTelemetry())

    body = insights.summary(days=7, user=user(is_admin=True))

    row = body["agents"][0]
    assert row["record_id"] == "rec-1"
    assert row["name"] == "기본 에이전트"
    assert row["turns"] == 7
    assert row["distinct_users"] == 1
    assert row["interrupted_turns"] == 3


def test_summary_makes_no_metered_cloudwatch_read():
    """The route the page polls. One `GetMetricData` here is $0.0028 a request at
    the measured sweep size, times every open tab, once a minute."""
    telemetry = StubTelemetry()
    wire(usage_with_one_agent(), telemetry)

    body = insights.summary(days=7, user=user(is_admin=True))

    assert telemetry.calls == 0
    assert "cloudwatch" not in body["sources"]
    assert "active_sessions" not in body["totals"], (
        "ActiveSessionCount is dimensioned per service, so in a shared account it "
        "counts other tenants' sessions"
    )
    assert "memory_tokens" not in body["totals"]


def test_the_telemetry_route_carries_the_vended_figures():
    metrics = {
        "arn:aws:bedrock-agentcore:us-east-1:1:harness/rec-1": {
            "invocations": 7.0, "latency_p90_ms": 1200.0,
            "error_rate": 0.1429, "vcpu_hours": 0.04, "gb_hours": 0.16,
        }
    }
    telemetry = StubTelemetry(metrics=metrics)
    wire(usage_with_one_agent(), telemetry)

    body = insights.telemetry(days=7, user=user(is_admin=True))

    assert telemetry.calls == 1
    row = body["agents"][0]
    assert row["record_id"] == "rec-1"
    assert row["latency_p90_ms"] == 1200.0
    assert row["error_rate"] == 0.1429
    # The performance tier prices nothing: runtime cost is the collector's.
    assert "estimated_runtime_cost" not in row
    assert body["sources"]["cloudwatch"] is True


def test_an_agent_cloudwatch_knows_nothing_about_is_omitted():
    """Not a row of nulls: "no series for this agent" and "this agent was idle"
    are different answers and the table renders them differently."""
    wire(usage_with_one_agent(), StubTelemetry(metrics={}))

    body = insights.telemetry(days=7, user=user(is_admin=True))

    assert body["agents"] == []


def test_missing_cloudwatch_degrades_that_panel_not_the_page():
    wire(usage_with_one_agent(), StubTelemetry(unavailable=True))

    telemetry = insights.telemetry(days=7, user=user(is_admin=True))
    assert telemetry["sources"]["cloudwatch"] is False
    assert telemetry["agents"] == []

    body = insights.summary(days=7, user=user(is_admin=True))
    assert body["agents"][0]["turns"] == 7, "our own figures survive"


def test_an_unmeasured_day_is_reported_as_unmeasured_not_as_zero_tokens():
    # One day inside the 7-day window (today), one day inside 30 days but outside 7 days.
    day_in_range = _today_str
    day_outside_7_inside_30 = (_today - timedelta(days=10)).strftime("%Y-%m-%d")

    usage = usage_with_one_agent({
        f"AGENTS#{_current_month}": [
            {"sk": f"D#{day_outside_7_inside_30}#A#rec-1", "turns": Decimal(4)},
            {"sk": f"D#{day_in_range}#A#rec-1", "turns": Decimal(2),
             "input_tokens": Decimal(500), "output_tokens": Decimal(50)},
        ],
    })
    wire(usage, StubTelemetry())

    body = insights.summary(days=30, user=user(is_admin=True))

    row = body["agents"][0]
    assert row["turns"] == 6
    assert row["input_tokens"] == 500
    # One rule: turns minus measured_turns. Neither legacy item was stamped by
    # the backfill, so both days are unmeasured until it runs — the heuristic
    # that used to guess "tokens present, so measured" is gone.
    assert row["unmeasured_turns"] == 6


def test_a_record_route_returns_that_records_trend_and_tools():
    wire(usage_with_one_agent(), StubTelemetry())

    body = insights.record_insights(record_id="rec-1", days=7, user=user())

    assert body["record_id"] == "rec-1"
    assert isinstance(body["daily"], list)
    assert isinstance(body["tools"], list)


def test_me_returns_the_callers_own_usage():
    wire(
        UsageService(
            repository=StubRepo({
                f"USERS#{_current_month}": [
                    {"sk": f"D#{_today_str}#U#sub-1", "turns": Decimal(9),
                     "input_tokens": Decimal(10)},
                    {"sk": f"D#{_today_str}#U#sub-2", "turns": Decimal(99),
                     "input_tokens": Decimal(10)},
                ],
            }),
            registry=StubRegistry([]),
            harness=StubHarness(),
        ),
        StubTelemetry(),
    )

    body = insights.my_insights(days=7, sub=None, user=user(sub="sub-1"))

    assert body["sub"] == "sub-1"
    assert body["totals"]["turns"] == 9, "another user's turns must not leak"


def test_only_an_admin_may_ask_about_someone_else():
    wire(usage_with_one_agent(), StubTelemetry())

    try:
        insights.my_insights(days=7, sub="sub-2", user=user(sub="sub-1"))
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("expected 403")

    body = insights.my_insights(
        days=7, sub="sub-2", user=user(sub="admin-1", is_admin=True)
    )
    assert body["sub"] == "sub-2"


def test_days_is_clamped_to_the_supported_windows():
    wire(usage_with_one_agent(), StubTelemetry())

    assert insights.summary(days=7, user=user(is_admin=True))["days"] == 7
    assert insights.summary(days=30, user=user(is_admin=True))["days"] == 30
    assert insights.summary(days=9999, user=user(is_admin=True))["days"] == 30


def test_every_insights_route_is_sync():
    """A blocking boto3 call inside an `async def` stalls every request in the
    process, not just its own."""
    import inspect

    for handler in (
        insights.summary,
        insights.telemetry,
        insights.record_insights,
        insights.composition,
        insights.my_insights,
    ):
        assert not inspect.iscoroutinefunction(handler), handler.__name__


def test_an_unmeasured_window_still_reports_its_turns_and_its_ignorance():
    """A backfilled day carries turns and no token attribute at all. The floor is
    reported as a floor — `unmeasured_turns` beside a zero token count — and no
    cost is derived from it, because there is no cost estimate left to derive."""
    usage = UsageService(
        repository=StubRepo({
            f"AGENTS#{_current_month}": [
                # Unmeasured day: no token attributes, only turns
                {"sk": f"D#{_today_str}#A#rec-1", "turns": Decimal(6)},
            ],
        }),
        registry=StubRegistry([StubRecord("rec-1", "unmeasured-agent")]),
        harness=StubHarness({"rec-1": "global.anthropic.claude-sonnet-5"}),
    )
    wire(usage, StubTelemetry())

    body = insights.summary(days=7, user=user(is_admin=True))
    row = body["agents"][0]

    assert row["input_tokens"] == 0
    assert row["output_tokens"] == 0
    assert row["unmeasured_turns"] == 6
    assert row["model_ids"] == []
    assert row["model_cost_micros"] is None
    assert body["totals"]["unmeasured_turns"] == 6


def test_record_route_unmeasured_window_reports_the_unmeasured_turns():
    """record_insights carries the same floor signal as the leaderboard."""
    usage = UsageService(
        repository=StubRepo({
            f"AGENTS#{_current_month}": [
                {"sk": f"D#{_today_str}#A#rec-1", "turns": Decimal(6)},
            ],
        }),
        registry=StubRegistry([StubRecord("rec-1", "unmeasured-agent")]),
        harness=StubHarness({"rec-1": "global.anthropic.claude-sonnet-5"}),
    )
    wire(usage, StubTelemetry())

    body = insights.record_insights(record_id="rec-1", days=7, user=user())

    assert body["totals"]["input_tokens"] == 0
    assert body["totals"]["output_tokens"] == 0
    assert body["totals"]["unmeasured_turns"] == 6


def test_the_window_states_the_calendar_its_dates_are_in():
    """A date axis whose timezone is unstated is one the reader assumes is theirs.

    The counters bucket days at midnight in `USAGE_TIMEZONE` — UTC by default, which
    is 09:00 in Seoul — so a Korean morning's turns sit under the previous label. The
    zone travels with every response so the axis can say so.
    """
    wire(usage_with_one_agent(), StubTelemetry())

    body = insights.summary(days=7, user=user(is_admin=True))

    assert body["timezone"]
    # The window ends now, so its last day is still being written. Every consumer
    # that subtracts or draws that point needs to know, and none of them did: the
    # half-window delta was comparing a fraction of today against whole days.
    assert body["partial_day"] == body["end_date"]


def test_a_partition_that_could_not_be_read_is_admitted_in_sources():
    """`sources.usage` was hardcoded true, so a throttled shard shipped a total that
    was silently short — the one figure on this page that degraded by lying."""
    class BrokenRepo(StubRepo):
        def query(self, pk, start_date, end_date):
            raise RuntimeError("ProvisionedThroughputExceededException")

    wire(
        UsageService(
            repository=BrokenRepo({}),
            registry=StubRegistry([StubRecord("rec-1", "기본 에이전트")]),
            harness=StubHarness({}),
        ),
        StubTelemetry(),
    )

    body = insights.summary(days=7, user=user(is_admin=True))

    assert body["sources"]["usage"] is False


def test_a_healthy_read_still_reports_its_source_as_good():
    wire(usage_with_one_agent(), StubTelemetry())

    assert insights.summary(days=7, user=user(is_admin=True))["sources"]["usage"] is True


def test_the_error_rate_row_says_which_series_it_counted():
    """A rate built on `SystemErrors` alone is not the same claim as one built on
    both, and the row is where that distinction has to survive."""
    arn = "arn:aws:bedrock-agentcore:us-east-1:1:harness/rec-1"
    wire(
        usage_with_one_agent(),
        StubTelemetry({
            arn: {
                "invocations": 10.0, "error_rate": 0.1,
                "error_basis": ["system_errors"],
                "vcpu_hours": 1.0, "gb_hours": 2.0,
            }
        }),
    )

    body = insights.telemetry(days=7, user=user(is_admin=True))

    assert body["agents"][0]["error_rate"] == 0.1
    assert body["agents"][0]["error_basis"] == ["system_errors"]


def test_the_user_leaderboard_is_admin_only():
    """These aggregates name people, not public registry records."""
    wire(usage_with_one_agent(), StubTelemetry())

    try:
        insights.user_leaderboard(days=7, user=user())
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("expected 403 for a plain user")

    body = insights.user_leaderboard(days=7, user=user(is_admin=True))
    assert body["totals"]["users"] >= 0
    assert "users" in body


SHARED_RUNTIME = "arn:aws:bedrock-agentcore:us-east-1:1:runtime/shared-KlMnO13579"


class ArnRecord:
    def __init__(self, record_id, harness_arn=None, runtime_arn=None):
        self.record_id = record_id
        self.name = record_id
        self.harness_arn = harness_arn
        self.agent_runtime_arn = runtime_arn
        self.descriptor_type = "A2A"


def telemetry_for(records, metrics):
    usage = UsageService(
        repository=StubRepo({}),
        registry=StubRegistry(records),
        harness=StubHarness({}),
    )
    wire(usage, StubTelemetry(metrics=metrics))
    return insights.telemetry(days=7, user=user(is_admin=True))


def test_two_records_sharing_a_runtime_are_not_counted_twice():
    """Measured on the live registry, and the aliases do not line up.

        harness_builtin_test_agent  runtime/harness_builtin_test_agent-KlMnO13579
        builtin_test_agent          harness/builtin_test_agent-OpQrS99001
                                  + runtime/harness_builtin_test_agent-KlMnO13579

    One physical runtime, two records, and their rows carry byte-identical vended
    figures because they describe the same thing. Summing the rows counted its hours
    twice, inflating the cost total and our side of the reconciliation — which made
    coverage look better than it is.

    The identity test has to be **overlap, not equality**: comparing the ARN sets
    for equality passes this test's simple twin case and misses exactly the shape
    above, where one record knows both aliases and the other knows one.

    The per-record rows stay: each record really did serve its turns, and an operator
    comparing two records wants both. It is the *totals* that count runtimes.
    """
    harness_arn = "arn:aws:bedrock-agentcore:us-east-1:1:harness/twin-OpQrS99001"
    result = telemetry_for(
        [
            ArnRecord("rec-a", runtime_arn=SHARED_RUNTIME),
            ArnRecord("rec-b", harness_arn=harness_arn, runtime_arn=SHARED_RUNTIME),
        ],
        {
            SHARED_RUNTIME: {"invocations": 2.0, "vcpu_hours": 0.5, "gb_hours": 10.0},
            # The harness alias carries the same series — it is the same runtime.
            harness_arn: {"invocations": 2.0, "vcpu_hours": 0.5, "gb_hours": 10.0},
        },
    )

    assert len(result["agents"]) == 2, "both records are still listed"
    assert result["totals"]["agents_listed"] == 2
    assert result["totals"]["distinct_runtimes"] == 1
    assert result["totals"]["vcpu_hours"] == 0.5, "one runtime, counted once"
    assert result["totals"]["gb_hours"] == 10.0


def test_genuinely_separate_runtimes_are_both_counted():
    """The de-duplication must not swallow two agents that really are two."""
    other = "arn:aws:bedrock-agentcore:us-east-1:1:runtime/other-abc"
    result = telemetry_for(
        [
            ArnRecord("rec-a", runtime_arn=SHARED_RUNTIME),
            ArnRecord("rec-b", runtime_arn=other),
        ],
        {
            SHARED_RUNTIME: {"invocations": 2.0, "vcpu_hours": 0.5, "gb_hours": 10.0},
            other: {"invocations": 3.0, "vcpu_hours": 1.0, "gb_hours": 20.0},
        },
    )

    assert result["totals"]["distinct_runtimes"] == 2
    assert result["totals"]["vcpu_hours"] == 1.5
    assert result["totals"]["gb_hours"] == 30.0


def test_non_admin_cannot_read_the_org_aggregates():
    """`/summary`, `/telemetry` and `/composition` are the whole fleet's usage and
    spend, so a plain user gets 403 — before the 501 that would otherwise leak that
    the feature exists and what it needs. The per-record and evaluation routes stay
    open: they are shared with the Registry page, which is not admin-only.
    """
    wire(usage_with_one_agent(), StubTelemetry())
    for call in (
        lambda: insights.summary(days=7, user=user()),
        lambda: insights.telemetry(days=7, user=user()),
        lambda: insights.composition(days=7, user=user()),
    ):
        try:
            call()
        except HTTPException as exc:
            assert exc.status_code == 403
        else:
            raise AssertionError("a non-admin must not read the org aggregates")


def _two_agents_both_spending():
    """Two registry agents that each spent tokens this window."""
    items = {
        f"AGENTS#{_current_month}": [
            {"sk": f"D#{_today_str}#A#rec-1", "turns": Decimal(7),
             "input_tokens": Decimal(1000), "output_tokens": Decimal(200)},
            {"sk": f"D#{_today_str}#A#rec-2", "turns": Decimal(3),
             "input_tokens": Decimal(500), "output_tokens": Decimal(100)},
        ],
    }
    return UsageService(
        repository=StubRepo(items),
        registry=StubRegistry([
            StubRecord("rec-1", "에이전트 A"),
            StubRecord("rec-2", "에이전트 B"),
        ]),
        harness=StubHarness({
            "rec-1": "global.anthropic.claude-sonnet-5",
            "rec-2": "global.anthropic.claude-sonnet-5",
        }),
    )


def test_summary_carries_guardrail_rollup():
    class GRUsage(UsageService):
        def guardrail_totals(self, s, e):
            return {"rec-1": {"interventions": 2, "blocked_input": 2,
                              "blocked_output": 0, "anonymized_input": 0,
                              "anonymized_output": 0, "policy_pii": 2,
                              "policy_content": 0, "policy_topic": 0, "policy_word": 0}}
    u = usage_with_one_agent()
    u.__class__ = GRUsage
    wire(u, StubTelemetry())
    body = insights.summary(days=7, user=user(is_admin=True))
    assert body["guardrail"]["by_agent"]["rec-1"]["interventions"] == 2
    assert body["guardrail"]["totals"]["blocked_input"] == 2
    assert body["sources"]["guardrail"] is True


# --- guardrail depth: what, how sure, how often, and whether it was there at all


def _gr(interventions=2, scanned=7, filters=None, confidences=None):
    return {"interventions": interventions, "blocked_input": interventions, "blocked_output": 0,
            "anonymized_input": 0, "anonymized_output": 0, "policy_content": interventions,
            "policy_pii": 0, "policy_topic": 0, "policy_word": 0, "scanned_turns": scanned,
            "by_filter": filters or {"INSULTS": interventions},
            "by_confidence": confidences or {"HIGH": interventions}}


class GRDepthUsage(UsageService):
    def guardrail_totals(self, s, e):
        return {"rec-1": _gr()}

    def guardrail_user_totals(self, s, e):
        return {"sub-1": _gr(interventions=2, scanned=7)}

    def guardrail_daily_totals(self, s, e):
        return {_today_str: {"interventions": 2, "scanned_turns": 7}}

    def guardrail_events(self, s, e, limit=20):
        return [{"at": f"{_today_str}T10:00:00Z", "agent_record_id": "rec-1", "owner_sub": "sub-1",
                 "thread_id": "t-1", "turn_id": "t-1:m-1", "action": "BLOCKED", "stage": "input",
                 "policies": ["content"], "filter_types": ["INSULTS"], "confidences": ["HIGH"]}][:limit]


def test_summary_guardrail_lists_recent_interventions_with_thread_and_owner():
    """What tripped it and who: the admin-only summary carries the event rows,
    each pointing at the conversation, so the panel can open it."""
    u = usage_with_one_agent()
    u.__class__ = GRDepthUsage
    wire(u, StubTelemetry())
    body = insights.summary(days=7, user=user(is_admin=True))
    [event] = body["guardrail"]["recent"]
    assert event["thread_id"] == "t-1" and event["owner_sub"] == "sub-1"
    assert event["agent_record_id"] == "rec-1"
    assert event["filter_types"] == ["INSULTS"] and event["action"] == "BLOCKED" and event["stage"] == "input"


def test_summary_guardrail_carries_the_per_user_rollup():
    u = usage_with_one_agent()
    u.__class__ = GRDepthUsage
    wire(u, StubTelemetry())
    body = insights.summary(days=7, user=user(is_admin=True))
    assert body["guardrail"]["by_user"]["sub-1"]["interventions"] == 2


def test_summary_guardrail_totals_carry_filter_and_confidence_breakdowns():
    u = usage_with_one_agent()
    u.__class__ = GRDepthUsage
    wire(u, StubTelemetry())
    body = insights.summary(days=7, user=user(is_admin=True))
    totals = body["guardrail"]["totals"]
    assert totals["by_filter"] == {"INSULTS": 2}
    assert totals["by_confidence"] == {"HIGH": 2}
    assert totals["scanned_turns"] == 7


def test_summary_guardrail_agent_carries_an_intervention_rate():
    """Two interventions in seven turns: the rate, not the count, is what compares
    a busy agent to a quiet one."""
    u = usage_with_one_agent()
    u.__class__ = GRDepthUsage
    wire(u, StubTelemetry())
    body = insights.summary(days=7, user=user(is_admin=True))
    agent = body["guardrail"]["by_agent"]["rec-1"]
    assert agent["intervention_rate"] == 2 / 7
    assert body["guardrail"]["totals"]["intervention_rate"] == 2 / 7


def test_summary_marks_an_agent_that_had_turns_but_no_guardrail_scan():
    """The governance gap: turns went through with no guardrail on the call. An
    agent with zero interventions is not the same thing as an agent that was
    never checked, and only the scan count can tell them apart."""
    class Unscanned(GRDepthUsage):
        def guardrail_totals(self, s, e):
            return {}

        def guardrail_daily_totals(self, s, e):
            # Scans began yesterday, so today's turns are judged.
            yesterday = (_today - timedelta(days=1)).strftime("%Y-%m-%d")
            return {yesterday: {"interventions": 0, "scanned_turns": 1}}
    u = usage_with_one_agent()
    u.__class__ = Unscanned
    wire(u, StubTelemetry())
    body = insights.summary(days=7, user=user(is_admin=True))
    row = body["agents"][0]
    assert row["turns"] == 7
    assert row["guardrail_scanned_turns"] == 0
    assert body["guardrail"]["unguarded_agents"] == ["rec-1"]


def test_summary_daily_points_carry_guardrail_interventions():
    u = usage_with_one_agent()
    u.__class__ = GRDepthUsage
    wire(u, StubTelemetry())
    body = insights.summary(days=7, user=user(is_admin=True))
    today = next(p for p in body["daily"] if p["date"] == _today_str)
    assert today["guardrail_interventions"] == 2
    others = [p for p in body["daily"] if p["date"] != _today_str]
    assert all(p["guardrail_interventions"] == 0 for p in others)


def test_user_leaderboard_rows_carry_guardrail_interventions():
    u = usage_with_one_agent({
        f"USERS#{_current_month}": [
            {"sk": f"D#{_today_str}#U#sub-1", "turns": Decimal(7),
             "input_tokens": Decimal(100), "output_tokens": Decimal(10)},
        ],
    })
    u.__class__ = GRDepthUsage
    wire(u, StubTelemetry())
    body = insights.user_leaderboard(days=7, user=user(is_admin=True))
    row = next(r for r in body["users"] if r["sub"] == "sub-1")
    assert row["guardrail_interventions"] == 2
    assert body["totals"]["guardrail_interventions"] == 2


def test_unguarded_only_judges_turns_after_scan_counting_began():
    """Scan counting shipped mid-window. An agent whose turns all predate the
    first recorded scan is not unguarded, it is unjudged — listing it would name
    a guarded agent as a gap for a week. Only turns on or after the first scan
    day count against it."""
    yesterday = (_today - timedelta(days=1)).strftime("%Y-%m-%d")
    two_days_ago = (_today - timedelta(days=2)).strftime("%Y-%m-%d")

    class MidWindow(GRDepthUsage):
        def guardrail_totals(self, s, e):
            return {"rec-1": _gr(interventions=0, scanned=3, filters={}, confidences={})}

        def guardrail_daily_totals(self, s, e):
            return {yesterday: {"interventions": 0, "scanned_turns": 3}}

    u = UsageService(
        repository=StubRepo({
            f"AGENTS#{_current_month}": [
                {"sk": f"D#{_today_str}#A#rec-1", "turns": Decimal(3)},
                # rec-2 ran only before scans existed; rec-3 ran after and was never scanned.
                {"sk": f"D#{two_days_ago}#A#rec-2", "turns": Decimal(4)},
                {"sk": f"D#{_today_str}#A#rec-3", "turns": Decimal(2)},
            ],
        }),
        registry=StubRegistry([StubRecord("rec-1", "A"), StubRecord("rec-2", "B"), StubRecord("rec-3", "C")]),
        harness=StubHarness({}),
    )
    u.__class__ = MidWindow
    wire(u, StubTelemetry())
    body = insights.summary(days=7, user=user(is_admin=True))
    assert body["guardrail"]["unguarded_agents"] == ["rec-3"]


def test_no_scan_in_window_means_nobody_is_called_unguarded():
    class NoScans(GRDepthUsage):
        def guardrail_totals(self, s, e):
            return {}

        def guardrail_daily_totals(self, s, e):
            return {}

    u = usage_with_one_agent()
    u.__class__ = NoScans
    wire(u, StubTelemetry())
    body = insights.summary(days=7, user=user(is_admin=True))
    assert body["guardrail"]["unguarded_agents"] == []


def test_the_first_scan_day_itself_is_not_judged():
    """Day granularity cannot split the first scan day into before-deploy and
    after-deploy turns, so a guarded agent that ran earlier that day would be
    listed as a gap for the rest of it. The verdict starts the next day."""
    class ScansOnlyToday(GRDepthUsage):
        def guardrail_totals(self, s, e):
            return {"rec-1": _gr(interventions=0, scanned=1, filters={}, confidences={})}

        def guardrail_daily_totals(self, s, e):
            return {_today_str: {"interventions": 0, "scanned_turns": 1}}

    u = UsageService(
        repository=StubRepo({
            f"AGENTS#{_current_month}": [
                {"sk": f"D#{_today_str}#A#rec-1", "turns": Decimal(1)},
                {"sk": f"D#{_today_str}#A#rec-2", "turns": Decimal(4)},
            ],
        }),
        registry=StubRegistry([StubRecord("rec-1", "A"), StubRecord("rec-2", "B")]),
        harness=StubHarness({}),
    )
    u.__class__ = ScansOnlyToday
    wire(u, StubTelemetry())
    body = insights.summary(days=7, user=user(is_admin=True))
    assert body["guardrail"]["unguarded_agents"] == []


# --- naming people -----------------------------------------------------------
#
# The pool signs people in by email, so Cognito mints the username as a UUID and
# the access token's `username` claim is the sub again. A name exists only via
# ListUsers, which the server does at read time for the admin-only views. The
# response carries `subjects` (sub -> email) beside the rows; the client decides
# how much of the email to show.


def test_user_leaderboard_names_its_subjects():
    wire(usage_with_one_agent({
        f"USERS#{_current_month}": [
            {"sk": f"D#{_today_str}#U#sub-1", "turns": Decimal(7),
             "input_tokens": Decimal(100), "output_tokens": Decimal(10)},
        ],
    }), StubTelemetry())
    insights._directory_override = StubDirectory({"sub-1": "alice@example.com"})

    body = insights.user_leaderboard(days=7, user=user(is_admin=True))

    assert body["subjects"] == {"sub-1": "alice@example.com"}
    assert insights._directory_override.asked == [["sub-1"]]


def test_summary_names_the_people_the_guardrail_section_mentions():
    """Both the per-user rollup keys and the owners on the recent event rows are
    subs the panel renders, so both are resolved — in one directory call."""
    u = usage_with_one_agent()
    u.__class__ = GRDepthUsage
    wire(u, StubTelemetry())
    insights._directory_override = StubDirectory({"sub-1": "alice@example.com"})

    body = insights.summary(days=7, user=user(is_admin=True))

    assert body["subjects"] == {"sub-1": "alice@example.com"}
    assert insights._directory_override.asked == [["sub-1"]]


class FakeThread:
    def __init__(self, thread_id, owner_sub):
        self.thread_id = thread_id
        self.owner_sub = owner_sub
        self.created_at = self.updated_at = f"{_today_str}T00:00:00Z"
        self.status = None
        self.metadata = {}


class StubThreads:
    def __init__(self, threads):
        self.threads = threads

    def search_threads(self, **kwargs):
        return self.threads


def test_record_threads_name_owners_for_an_admin_only():
    wire(usage_with_one_agent(), StubTelemetry())
    insights._threads_override = StubThreads([FakeThread("t-1", "sub-1"), FakeThread("t-2", "sub-2")])
    insights._directory_override = StubDirectory({"sub-1": "alice@example.com"})

    body = insights.record_threads("rec-1", limit=10, user=user(is_admin=True))
    assert body["subjects"] == {"sub-1": "alice@example.com"}
    assert insights._directory_override.asked == [["sub-1", "sub-2"]]

    # A plain user's list carries no owners, so there is nothing to name — and the
    # directory is not consulted on their behalf.
    insights._directory_override = StubDirectory({"sub-1": "alice@example.com"})
    body = insights.record_threads("rec-1", limit=10, user=user(sub="sub-1"))
    assert body["subjects"] == {}
    assert insights._directory_override.asked == []


def test_a_broken_directory_leaves_the_rows_unnamed_but_served():
    """Names are decoration on a usage page. The task role lacked ListUsers for the
    whole life of the feature; a directory that raises must cost the labels, not
    the page."""
    wire(usage_with_one_agent({
        f"USERS#{_current_month}": [
            {"sk": f"D#{_today_str}#U#sub-1", "turns": Decimal(7),
             "input_tokens": Decimal(100), "output_tokens": Decimal(10)},
        ],
    }), StubTelemetry())
    insights._directory_override = RaisingDirectory()

    body = insights.user_leaderboard(days=7, user=user(is_admin=True))

    assert body["subjects"] == {}
    assert body["users"][0]["sub"] == "sub-1"
