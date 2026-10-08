"""`/summary` serves the ledger, the collector and the reconciliation — from DDB only.

No estimate survives here. Model cost is the sum the ledger wrote at turn time,
runtime cost is what the collector priced from CloudWatch quantities, and the bill
appears only as the `RECON#` snapshot the six-hourly job left behind. The route
must not reach Cost Explorer, CloudWatch or the Price List: a billing stub that
raises on any call is wired to prove it.
"""
import os
import sys
from datetime import datetime, timedelta
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routes.insights as insights  # noqa: E402
from core.auth import AuthUser  # noqa: E402
from services.collector_service import CollectorService  # noqa: E402
from services.usage_service import UsageService  # noqa: E402

_today = datetime.utcnow().date()
TODAY = _today.strftime("%Y-%m-%d")
YESTERDAY = (_today - timedelta(days=1)).strftime("%Y-%m-%d")
# The newest day whose bill the reconciler could have read whole (`_bill_settled`).
SETTLED = (_today - timedelta(days=2)).strftime("%Y-%m-%d")
MONTH = _today.strftime("%Y-%m")
RUNTIME = "arn:aws:bedrock-agentcore:us-east-1:1:runtime/bap_default-x"
OTHER_RUNTIME = "arn:aws:bedrock-agentcore:us-east-1:1:runtime/other-y"


class Repo:
    """A keyed table: `query` by date range, `query_prefix` by sort-key prefix."""

    def __init__(self, items):
        self.items = {}
        for pk, rows in items.items():
            for row in rows:
                self.items[(pk, row["sk"])] = {"pk": pk, **row}

    def query(self, pk, start, end):
        return [r for (p, s), r in self.items.items() if p == pk and f"D#{start}" <= s <= f"D#{end}￿"]

    def query_prefix(self, pk, prefix):
        return [r for (p, s), r in self.items.items() if p == pk and s.startswith(prefix)]

    def get(self, pk, sk):
        return self.items.get((pk, sk))


class Record:
    def __init__(self, record_id, name, runtime_arn=None, harness_arn=None):
        self.record_id = record_id
        self.name = name
        self.agent_runtime_arn = runtime_arn
        self.harness_arn = harness_arn
        self.descriptor_type = "A2A"


class Registry:
    def __init__(self, records):
        self._records = records

    def agent_records(self):
        return self._records

    def list_records(self, descriptor_type=None, **kwargs):
        return []

    def list_agent_runtimes(self):
        return []


class Harness:
    def list_harnesses(self, with_tools=False):
        return []


class ExplodingBilling:
    def __getattr__(self, name):
        raise AssertionError(f"/summary reached Cost Explorer via {name}")


class ExplodingTelemetry:
    def agent_metrics(self, *a, **k):
        raise AssertionError("/summary reached CloudWatch")


def admin():
    return AuthUser(sub="admin-1", username="admin", groups=["admin"])


def D(value):
    return Decimal(value)


def repo_with_everything():
    return Repo({
        f"AGENTS#{MONTH}": [
            {"sk": f"D#{TODAY}#A#rec-1", "turns": D(3), "measured_turns": D(3), "input_tokens": D(1_000_000),
             "output_tokens": D(10_000), "tool_calls": D(2), "model_cost_micros": D(2_100_000), "priced_turns": D(3)},
            {"sk": f"D#{TODAY}#A#rec-2", "turns": D(1), "measured_turns": D(1), "input_tokens": D(500),
             "unpriced_turns": D(1)},
        ],
        f"AGENT_MODELS#{MONTH}": [
            {"sk": f"D#{TODAY}#A#rec-1#M#global.anthropic.claude-sonnet-5", "turns": D(3), "input_tokens": D(1_000_000),
             "output_tokens": D(10_000), "model_cost_micros": D(2_100_000), "priced_turns": D(3), "measured_turns": D(3)},
            {"sk": f"D#{TODAY}#A#rec-2#M#global.anthropic.claude-opus-5-5", "turns": D(1), "input_tokens": D(500),
             "unpriced_turns": D(1), "measured_turns": D(1)},
        ],
        f"AGENT#rec-1#USERS#{MONTH}": [{"sk": f"D#{TODAY}#U#sub-1", "turns": D(3)}],
        f"AGENT#rec-2#USERS#{MONTH}": [{"sk": f"D#{TODAY}#U#sub-1", "turns": D(1)}],
        f"RESOURCES#{MONTH}": [
            {"sk": f"D#{YESTERDAY}#R#{RUNTIME}", "vcpu_hours_micro": D(300_000), "gb_hours_micro": D(59_659_000),
             "runtime_cost_micros": D(590_628), "invocations": D(150), "complete": True,
             "collected_at": f"{TODAY}T00:05:00+00:00", "rate_source": "price_list"},
            {"sk": f"D#{TODAY}#R#{RUNTIME}", "vcpu_hours_micro": D(100_000), "gb_hours_micro": D(20_000_000),
             "runtime_cost_micros": D(197_950), "invocations": D(40), "complete": False,
             "collected_at": f"{TODAY}T10:05:00+00:00", "rate_source": "price_list"},
        ],
        f"GATEWAYS#{MONTH}": [
            {"sk": f"D#{TODAY}#G#arn:aws:bedrock-agentcore:us-east-1:1:gateway/g", "invocations": D(200),
             "gateway_cost_micros": D(1_000), "complete": False, "collected_at": f"{TODAY}T10:05:00+00:00"},
        ],
        f"RECON#{MONTH}": [
            # Two days ago: Cost Explorer had a whole day to ingest it before the
            # 06:00 check. Yesterday: checked before CE finished — a fifth of the
            # day billed against all of ours, the shape of the live 09-23 row.
            {"sk": f"D#{SETTLED}#K#agent_runtime#bap_default", "billed_micros": D(590_900), "ours_micros": D(590_628),
             "diff_micros": D(272), "ce_estimated": False, "checked_at": f"{TODAY}T06:00:00Z"},
            {"sk": f"D#{YESTERDAY}#K#agent_runtime#bap_default", "billed_micros": D(118_650), "ours_micros": D(590_628),
             "diff_micros": D(-471_978), "ce_estimated": True, "checked_at": f"{TODAY}T06:00:00Z"},
            {"sk": f"D#{TODAY}#K#component#runtime", "billed_micros": D(590_900), "ours_micros": D(788_578),
             "diff_micros": D(-197_678), "ce_estimated": True, "checked_at": f"{TODAY}T06:00:00Z"},
            {"sk": f"D#{TODAY}#K#model_rate#claude-sonnet-5|global|output", "billed_usd_per_1m_micro": D(12_000_000),
             "card_usd_per_1m_micro": D(10_000_000), "match": False, "registered": True, "region": "us-east-1",
             "checked_at": f"{TODAY}T06:00:00Z"},
            {"sk": f"D#{TODAY}#K#model_rate#claude-opus-5-5|global|output", "billed_usd_per_1m_micro": D(30_000_000),
             "registered": False, "region": "us-east-1", "checked_at": f"{TODAY}T06:00:00Z"},
            {"sk": f"D#{TODAY}#K#summary#latest", "checked_at": f"{TODAY}T06:00:00Z", "start_date": YESTERDAY,
             "end_date": TODAY, "mismatch_count": D(1), "unregistered_families": ["claude-opus-5-5"]},
        ],
    })


def wire(repo):
    registry = Registry([Record("rec-1", "bap_default", runtime_arn=RUNTIME), Record("rec-2", "content_creator", runtime_arn=OTHER_RUNTIME)])
    usage = UsageService(repository=repo, registry=registry, harness=Harness())
    insights._usage_override = usage
    insights._telemetry_override = ExplodingTelemetry()
    insights._billing_override = ExplodingBilling()
    insights._collector_override = CollectorService(
        repository=repo, cloudwatch=None, pricing=None, registry=registry, harness=Harness(), region="us-east-1"
    )
    return usage


def teardown_function():
    insights._usage_override = None
    insights._telemetry_override = None
    insights._billing_override = None
    insights._collector_override = None


def test_summary_cost_block_comes_from_the_ledger_and_the_collector():
    wire(repo_with_everything())
    out = insights.summary(days=7, user=admin())

    cost = out["cost"]
    assert cost["model"]["micros"] == 2_100_000
    assert cost["model"]["priced_turns"] == 3
    assert cost["model"]["unpriced_turns"] == 1
    assert cost["model"]["unregistered_models"] == ["global.anthropic.claude-opus-5-5"]
    assert cost["model"]["rate_card_version"]
    assert cost["runtime"]["micros"] == 590_628 + 197_950
    assert cost["runtime"]["vcpu_hours_micro"] == 400_000
    assert cost["runtime"]["gb_hours_micro"] == 79_659_000
    assert cost["runtime"]["complete_through"] == YESTERDAY
    assert cost["runtime"]["as_of"] == f"{TODAY}T10:05:00+00:00"
    assert cost["gateway"]["micros"] == 1_000 and cost["gateway"]["invocations"] == 200
    assert cost["total_micros"] == 2_100_000 + 590_628 + 197_950 + 1_000
    assert out["sources"]["collector"] is True
    assert out["sources"]["usage_logs"] is False


def test_summary_rows_carry_model_runtime_total_and_billed_diff():
    wire(repo_with_everything())
    rows = {row["name"]: row for row in insights.summary(days=7, user=admin())["agents"]}

    default = rows["bap_default"]
    assert default["model_cost_micros"] == 2_100_000
    assert default["runtime_cost_micros"] == 590_628 + 197_950
    assert default["total_cost_micros"] == 2_100_000 + 590_628 + 197_950
    assert default["invocations"] == 190
    assert default["model_ids"] == ["global.anthropic.claude-sonnet-5"]
    # The bill so far includes yesterday's partial figure; the gap does not.
    assert default["billed_runtime_micros"] == 590_900 + 118_650
    assert default["cost_diff_micros"] == 272
    assert default["priced_turns"] == 3 and default["unpriced_turns"] == 0

    other = rows["content_creator"]
    assert other["model_cost_micros"] is None
    assert other["unpriced_turns"] == 1
    assert other["runtime_cost_micros"] is None
    assert other["total_cost_micros"] is None


def test_summary_reports_reconciliation_and_rate_card_findings():
    wire(repo_with_everything())
    cost = insights.summary(days=7, user=admin())["cost"]

    assert cost["billed"]["runtime_micros"] == 590_900 + 118_650
    assert cost["billed"]["component"] == {"runtime": 590_900}
    assert cost["billed"]["latest_day"] == YESTERDAY
    assert cost["diff"]["runtime_micros"] == 272
    assert cost["diff"]["days_compared"] == 1 and cost["diff"]["through"] == SETTLED
    assert cost["rate_card"]["mismatches"] == [{
        "family": "claude-sonnet-5", "routing": "global", "tier": "output",
        "card_micro": 10_000_000, "billed_micro": 12_000_000,
    }]
    assert cost["rate_card"]["unregistered_families"] == ["claude-opus-5-5"]
    assert cost["rate_card"]["checked_at"] == f"{TODAY}T06:00:00Z"


def test_summary_models_section_lists_each_model_with_its_cost():
    wire(repo_with_everything())
    models = {m["model_id"]: m for m in insights.summary(days=7, user=admin())["models"]}
    assert models["global.anthropic.claude-sonnet-5"]["model_cost_micros"] == 2_100_000
    assert models["global.anthropic.claude-sonnet-5"]["family"] == "claude-sonnet-5"
    assert models["global.anthropic.claude-sonnet-5"]["routing"] == "global"
    assert models["global.anthropic.claude-opus-5-5"]["model_cost_micros"] is None
    assert models["global.anthropic.claude-opus-5-5"]["registered"] is False


def test_summary_without_collector_items_says_so_instead_of_zero():
    repo = repo_with_everything()
    for key in [k for k in repo.items if k[0].startswith(("RESOURCES#", "GATEWAYS#", "RECON#"))]:
        del repo.items[key]
    wire(repo)
    out = insights.summary(days=7, user=admin())
    assert out["sources"]["collector"] is False
    assert out["sources"]["recon"] is False
    assert out["cost"]["runtime"]["micros"] is None
    assert out["cost"]["billed"] is None
    assert out["cost"]["total_micros"] is None
    assert out["cost"]["rate_card"]["mismatches"] == []


def test_daily_series_carries_model_and_runtime_cost():
    wire(repo_with_everything())
    daily = {point["date"]: point for point in insights.summary(days=7, user=admin())["daily"]}
    assert daily[TODAY]["model_cost_micros"] == 2_100_000
    assert daily[TODAY]["runtime_cost_micros"] == 197_950
    assert daily[YESTERDAY]["runtime_cost_micros"] == 590_628
    assert daily[YESTERDAY]["model_cost_micros"] == 0


def test_users_carry_ledger_model_cost_and_no_runtime_without_session_logs():
    repo = repo_with_everything()
    repo.items[(f"USERS#{MONTH}", f"D#{TODAY}#U#sub-1")] = {
        "pk": f"USERS#{MONTH}", "sk": f"D#{TODAY}#U#sub-1", "turns": D(4), "measured_turns": D(4),
        "input_tokens": D(1_000_500), "output_tokens": D(10_000), "model_cost_micros": D(2_100_000),
        "priced_turns": D(3), "unpriced_turns": D(1),
        "threads_started": D(2), "interrupted_turns": D(1), "tool_calls": D(9),
    }
    repo.items[(f"AGENTS#{MONTH}", f"D#{TODAY}#A#rec-1")]["interrupted_turns"] = D(3)
    wire(repo)
    out = insights.user_leaderboard(days=7, user=admin())
    row = out["users"][0]
    # How this person works: four turns in two threads, one walked away from,
    # nine tool calls — the counters every USERS# row carries, surfaced as-is.
    assert (row["threads_started"], row["interrupted_turns"], row["tool_calls"]) == (2, 1, 9)
    assert (out["totals"]["threads_started"], out["totals"]["interrupted_turns"], out["totals"]["tool_calls"]) == (2, 1, 9)
    # The agent ledger knows of two more interrupted turns than any row does
    # (rec-1: 3). They are reported as unattributed, not silently dropped.
    assert out["totals"]["interrupted_turns_unattributed"] == 2
    assert out["totals"]["failed_turns_unattributed"] == 0
    assert row["model_cost_micros"] == 2_100_000
    assert row["unpriced_turns"] == 1
    assert row["runtime_cost_micros"] is None
    assert row["total_cost_micros"] is None
    assert out["sources"]["usage_logs"] is False
    assert out["sources"]["usage_logs_since"] is None
    assert out["totals"]["model_cost_micros"] == 2_100_000
    # Which agents this person used, most-used first, and on how many days.
    assert row["agents"] == [
        {"record_id": "rec-1", "name": "bap_default", "turns": 3},
        {"record_id": "rec-2", "name": "content_creator", "turns": 1},
    ]
    assert row["active_days"] == 1
    daily = {point["date"]: point["users"] for point in out["daily_active_users"]}
    assert daily[TODAY] == 1 and daily[YESTERDAY] == 0 and len(daily) == 7
    assert "estimated_cost" not in out["totals"]


def test_users_runtime_cost_is_attributed_through_session_logs():
    repo = repo_with_everything()
    repo.items[(f"USERS#{MONTH}", f"D#{TODAY}#U#sub-1")] = {
        "pk": f"USERS#{MONTH}", "sk": f"D#{TODAY}#U#sub-1", "turns": D(1), "measured_turns": D(1),
        "input_tokens": D(10), "model_cost_micros": D(20), "priced_turns": D(1),
    }
    repo.items[(f"TURNS#{MONTH}", f"T#{TODAY}T09:00:00Z#thread-1:h-1")] = {
        "pk": f"TURNS#{MONTH}", "sk": f"T#{TODAY}T09:00:00Z#thread-1:h-1", "turn_id": "thread-1:h-1",
        "thread_id": "thread-1", "owner_sub": "sub-1", "agent_record_id": "rec-1", "ended_at": f"{TODAY}T09:00:00Z",
    }
    repo.items[(f"SESSIONS#{MONTH}", f"D#{TODAY}#S#thread-1#R#{RUNTIME}")] = {
        "pk": f"SESSIONS#{MONTH}", "sk": f"D#{TODAY}#S#thread-1#R#{RUNTIME}", "runtime_cost_micros": D(5_844),
        "vcpu_hours_micro": D(12_500), "gb_hours_micro": D(500_000),
    }
    wire(repo)
    out = insights.user_leaderboard(days=7, user=admin())
    row = out["users"][0]
    assert row["runtime_cost_micros"] == 5_844
    assert row["total_cost_micros"] == 20 + 5_844
    assert out["sources"]["usage_logs"] is True
    # The column covers conversations from this day on and nothing before it,
    # which is why it does not sum to the Runtime tile.
    assert out["sources"]["usage_logs_since"] == TODAY


def test_record_route_carries_ledger_cost_models_and_recent_turns():
    repo = repo_with_everything()
    repo.items[(f"TURNS#{MONTH}", f"T#{TODAY}T09:00:00Z#thread-1:h-1")] = {
        "pk": f"TURNS#{MONTH}", "sk": f"T#{TODAY}T09:00:00Z#thread-1:h-1", "turn_id": "thread-1:h-1",
        "thread_id": "thread-1", "owner_sub": "sub-1", "agent_record_id": "rec-1", "ended_at": f"{TODAY}T09:00:00Z",
        "model_id": "global.anthropic.claude-sonnet-5", "model_cost_micros": D(700_000), "status": "completed",
        "input_tokens": D(333_333), "measured": True,
    }
    wire(repo)
    out = insights.record_insights("rec-1", days=7, user=admin())
    assert out["model_cost_micros"] == 2_100_000
    assert out["runtime_cost_micros"] == 590_628 + 197_950
    assert [m["model_id"] for m in out["models"]] == ["global.anthropic.claude-sonnet-5"]
    assert out["turns"][0]["turn_id"] == "thread-1:h-1"
    assert out["turns"][0]["model_cost_micros"] == 700_000
    assert "estimated_model_cost" not in out


def test_telemetry_no_longer_prices_anything():
    wire(repo_with_everything())

    class Telemetry:
        def agent_metrics(self, arns, start, end):
            return {RUNTIME: {"invocations": 10.0, "latency_p90_ms": 100.0, "vcpu_hours": 1.0, "gb_hours": 2.0}}

    insights._telemetry_override = Telemetry()
    insights._pricing_override = ExplodingBilling()
    out = insights.telemetry(days=7, user=admin())
    row = out["agents"][0]
    assert row["latency_p90_ms"] == 100.0
    assert "estimated_runtime_cost" not in row
    assert "estimated_runtime_cost" not in out["totals"]
    assert "reconciliation" not in out
    insights._pricing_override = None


def test_keepwarm_session_cost_is_reported_separately_and_not_attributed_to_users():
    repo = repo_with_everything()
    repo.items[(f"SESSIONS#{MONTH}", f"D#{TODAY}#S#keepwarm-bap-default-000#R#{RUNTIME}")] = {
        "pk": f"SESSIONS#{MONTH}", "sk": f"D#{TODAY}#S#keepwarm-bap-default-000#R#{RUNTIME}",
        "runtime_cost_micros": D(9_000), "keepwarm": True,
    }
    repo.items[(f"SESSIONS#{MONTH}", f"D#{TODAY}#S#thread-1#R#{RUNTIME}")] = {
        "pk": f"SESSIONS#{MONTH}", "sk": f"D#{TODAY}#S#thread-1#R#{RUNTIME}",
        "runtime_cost_micros": D(1_000), "keepwarm": False,
    }
    wire(repo)
    out = insights.summary(days=7, user=admin())
    assert out["cost"]["runtime"]["keepwarm_micros"] == 9_000
    assert out["cost"]["runtime"]["session_micros"] == 10_000
    assert out["sources"]["usage_logs"] is True


def test_the_partial_day_is_never_in_the_bill_comparison():
    """Cost Explorer's figure for today covers only the hours it has ingested, so a
    today row must not enter the diff — on the live table it alone turned seven
    days of agreement into "−8.5%". The bill total keeps it; the comparison and
    the per-agent diff do not."""
    repo = repo_with_everything()
    repo.items[(f"RECON#{MONTH}", f"D#{TODAY}#K#agent_runtime#bap_default")] = {
        "pk": f"RECON#{MONTH}", "sk": f"D#{TODAY}#K#agent_runtime#bap_default",
        "billed_micros": D(118_650), "ours_micros": D(598_021), "diff_micros": D(-479_371),
        "ce_estimated": True, "checked_at": f"{TODAY}T18:00:00Z",
    }
    wire(repo)
    out = insights.summary(days=7, user=admin())
    assert out["cost"]["diff"]["runtime_micros"] == 272
    assert out["cost"]["diff"]["days_compared"] == 1
    assert out["cost"]["diff"]["through"] == SETTLED
    assert out["cost"]["billed"]["runtime_micros"] == 590_900 + 118_650 + 118_650
    assert out["cost"]["billed"]["latest_day"] == TODAY
    default = {row["name"]: row for row in out["agents"]}["bap_default"]
    assert default["billed_runtime_micros"] == 590_900 + 118_650 and default["cost_diff_micros"] == 272


def test_yesterday_enters_the_bill_comparison_only_once_cost_explorer_has_had_a_day():
    """CE publishes a day over the following ~24 hours, and the reconciler reads
    yesterday as early as 04:00. Live 2026-09-24 04:06Z: the 09-23 row read $0.24
    billed against $1.48 of ours while every earlier day agreed to 0.05%, and the
    headline said −3.45%. A row counts once it was checked two calendar days after
    its date; the same row, re-read tomorrow, joins the comparison."""
    repo = repo_with_everything()
    wire(repo)
    assert insights.summary(days=7, user=admin())["cost"]["diff"]["days_compared"] == 1
    tomorrow = (_today + timedelta(days=1)).strftime("%Y-%m-%d")
    repo.items[(f"RECON#{MONTH}", f"D#{YESTERDAY}#K#agent_runtime#bap_default")].update({
        "billed_micros": D(590_700), "diff_micros": D(72), "checked_at": f"{tomorrow}T04:06:00Z",
    })
    cost = insights.summary(days=7, user=admin())["cost"]
    assert cost["diff"]["days_compared"] == 2 and cost["diff"]["through"] == YESTERDAY
    assert cost["diff"]["runtime_micros"] == 272 + 72
    assert insights._bill_settled(YESTERDAY, f"{TODAY}T23:59:59Z") is False
    assert insights._bill_settled(SETTLED, f"{TODAY}T00:00:00Z") is True


def test_runtime_cost_no_leaderboard_row_claims_is_named():
    """The Runtime tile totals every collected runtime; the leaderboard only the
    ones a record owns. Live 2026-09-24 the two differed by $1.22 — an MCP-app
    record's runtime, a redeploy's retired runtime and a deleted harness's
    companion — and nothing on the page said where the money went."""
    repo = repo_with_everything()
    orphan = "arn:aws:bedrock-agentcore:us-east-1:1:runtime/bap_platform_status-QrStU64738"
    repo.items[(f"RESOURCES#{MONTH}", f"D#{YESTERDAY}#R#{orphan}")] = {
        "pk": f"RESOURCES#{MONTH}", "sk": f"D#{YESTERDAY}#R#{orphan}", "runtime_cost_micros": D(1_048_431),
        "vcpu_hours_micro": D(1), "gb_hours_micro": D(1), "complete": True, "collected_at": f"{TODAY}T00:05:00+00:00",
    }
    wire(repo)
    out = insights.summary(days=7, user=admin())
    runtime = out["cost"]["runtime"]
    assert runtime["micros"] == 590_628 + 197_950 + 1_048_431
    assert runtime["unclaimed_micros"] == 1_048_431
    assert runtime["unclaimed_runtimes"] == [{"runtime": "bap_platform_status", "runtime_cost_micros": 1_048_431}]
    assert sum(row["runtime_cost_micros"] or 0 for row in out["agents"]) + runtime["unclaimed_micros"] == runtime["micros"]
    # Nothing unclaimed: absent, not 0.
    wire(repo_with_everything())
    assert insights.summary(days=7, user=admin())["cost"]["runtime"]["unclaimed_micros"] is None


def test_summary_lists_learned_rates_only_for_models_this_platform_ran():
    from data.model_rates import set_learned_rates

    repo = repo_with_everything()
    # The overlay is whatever the table holds: the route loads it, never trusts
    # the process's memory, so the entries go through the repository.
    for sk, entry in {
        "R#claude-opus-5-5|global|output|2026-09-20": {"family": "claude-opus-5-5", "routing": "global", "tier": "output", "effective_from": "2026-09-20", "usd_per_1m": "20"},
        "R#claude-fable-5-1|regional|output|2026-09-01": {"family": "claude-fable-5-1", "routing": "regional", "tier": "output", "effective_from": "2026-09-01", "usd_per_1m": "55"},
    }.items():
        repo.items[("RATES#learned", sk)] = {"pk": "RATES#learned", "sk": sk, **entry}
    try:
        wire(repo)
        learned = insights.summary(days=7, user=admin())["cost"]["rate_card"]["learned"]
        assert [(e["family"], e["tier"], e["usd_per_1m"]) for e in learned] == [("claude-opus-5-5", "output", "20")]
    finally:
        set_learned_rates([])


def test_first_summary_after_start_folds_deployed_arn_ledger_keys_into_their_record():
    """A turn the stream keyed by the harness ARN (`deployed:<arn>`) belongs to
    the record that owns that harness. The fold reads aliases set on the shared
    usage service, and the route used to set them *after* reading the ledger, so
    the first request of a fresh process listed the ARN as its own row (live,
    2026-09-24: `deployed:arn:…:harness/web_harness-…`, 1 turn) and the second
    request did not."""
    repo = repo_with_everything()
    harness = "arn:aws:bedrock-agentcore:us-east-1:1:harness/web_harness-x"
    repo.items[(f"AGENTS#{MONTH}", f"D#{TODAY}#A#deployed:{harness}")] = {
        "pk": f"AGENTS#{MONTH}", "sk": f"D#{TODAY}#A#deployed:{harness}", "turns": D(1), "measured_turns": D(1),
        "input_tokens": D(3_509), "model_cost_micros": D(3_644), "priced_turns": D(1),
    }
    registry = Registry([
        Record("rec-1", "bap_default", runtime_arn=RUNTIME),
        Record("rec-2", "content_creator", runtime_arn=OTHER_RUNTIME),
        Record("rec-3", "web_harness", harness_arn=harness),
    ])
    usage = UsageService(repository=repo, registry=registry, harness=Harness())  # fresh: no aliases yet
    insights._usage_override = usage
    insights._telemetry_override = ExplodingTelemetry()
    insights._billing_override = ExplodingBilling()
    insights._collector_override = CollectorService(
        repository=repo, cloudwatch=None, pricing=None, registry=registry, harness=Harness(), region="us-east-1"
    )
    rows = {row["name"]: row for row in insights.summary(days=7, user=admin())["agents"]}
    assert "web_harness" in rows and rows["web_harness"]["turns"] == 1
    assert not any(name.startswith("deployed:") for name in rows)
