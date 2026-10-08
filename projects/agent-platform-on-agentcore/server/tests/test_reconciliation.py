"""The bill checks the ledger, and extends the rate card under one rule.

`reconcile` snapshots three comparisons into `RECON#` items: billed runtime per
(agent, day) against the collector's day items, billed cost per component against
our summed components, and the per-token rate the account was actually charged
— **per day** — against the rate in force that day. A mismatch is written and
reported, never smoothed over. A rate the card lacks is learned only when the
same exact value was billed on two consecutive complete days, and then dated.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.billing_service import BillingService  # noqa: E402


def group(keys, cost, quantity=None, unit=None):
    metrics = {"UnblendedCost": {"Amount": str(cost), "Unit": "USD"}}
    if quantity is not None:
        metrics["UsageQuantity"] = {"Amount": str(quantity), "Unit": unit}
    return {"Keys": list(keys), "Metrics": metrics}


class StubCE:
    """Answers by the shape of the GroupBy it is asked for."""

    def __init__(self, sonnet_output_usd_per_1k="0.010"):
        self.requests = []
        self.sonnet_output = sonnet_output_usd_per_1k

    def get_cost_and_usage(self, **params):
        self.requests.append(params)
        keys = [g.get("Key") for g in params.get("GroupBy", [])]
        if keys == ["AgentName", "USAGE_TYPE"]:
            return {"ResultsByTime": [{
                "TimePeriod": {"Start": "2026-08-25", "End": "2026-08-26"},
                "Estimated": False,
                "Groups": [
                    group(["AgentName$bap_default", "USE1-Runtime:Consumption-based:Memory"], 0.564, 59.678, "GB-Hours"),
                    group(["AgentName$bap_default", "USE1-Runtime:Consumption-based:vCPU"], 0.0269, 0.3, "vCPU-Hours"),
                    group(["AgentName$", "USE1-Runtime:Consumption-based:Memory"], 0.01, 1.0, "GB-Hours"),
                ],
            }]}
        if keys == ["USAGE_TYPE", "REGION"]:
            q = 1000.0
            return {"ResultsByTime": [{
                "TimePeriod": {"Start": "2026-08-01", "End": "2026-09-01"},
                "Groups": [
                    group(["USE1-anthropic.claude-sonnet-5-mantle-output-tokens-global-standard", "us-east-1"],
                          float(self.sonnet_output) * q, q, "1K tokens"),
                    group(["USE1-anthropic.claude-sonnet-5-mantle-input-tokens-standard", "us-east-1"],
                          0.0022 * q, q, "1K tokens"),
                    group(["USE1-anthropic.claude-opus-5-5-mantle-output-tokens-global-standard", "us-east-1"],
                          0.030 * q, q, "1K tokens"),
                    group(["USE1-anthropic.claude-sonnet-5-mantle-cache-write-tokens-1h-standard", "us-east-1"],
                          0.0055 * q, q, "1K tokens"),
                    group(["USW2-anthropic.claude-sonnet-5-mantle-output-tokens-global-standard", "us-west-2"],
                          0.0 * q, q, "1K tokens"),
                ],
            }]}
        if keys == ["USAGE_TYPE"]:
            return {"ResultsByTime": [{
                "TimePeriod": {"Start": "2026-08-25", "End": "2026-08-26"},
                "Estimated": True,
                "Groups": [
                    group(["USE1-Runtime:Consumption-based:Memory"], 0.574, 60.678, "GB-Hours"),
                    group(["USE1-Gateway:Consumption-based:API-Invocations"], 0.001, 200, "Invocations"),
                ],
            }]}
        raise AssertionError(f"unexpected GroupBy {keys}")


class Repo:
    def __init__(self):
        self.items = {}

    def set_fields(self, pk, sk, fields):
        self.items.setdefault((pk, sk), {}).update(fields)


def service(stub):
    svc = BillingService(region_name="us-east-1", platform="bap")
    svc._client = stub
    return svc


LEDGER_AGENT_DAYS = {
    ("bap_default", "2026-08-25"): {
        "runtime_cost_micros": 590_628, "gb_hours_micro": 59_659_000, "vcpu_hours_micro": 300_000,
    }
}
LEDGER_COMPONENTS = {"runtime": 590_628, "gateway": 1_000}


def test_agent_day_diff_is_billed_minus_ours():
    repo = Repo()
    result = service(StubCE()).reconcile(
        "2026-08-25", "2026-08-25", repository=repo,
        ledger_agent_days=LEDGER_AGENT_DAYS, ledger_components=LEDGER_COMPONENTS,
    )
    row = repo.items[("RECON#2026-08", "D#2026-08-25#K#agent_runtime#bap_default")]
    assert row["billed_micros"] == 590_900
    assert row["ours_micros"] == 590_628
    assert row["diff_micros"] == 272
    assert row["billed_gb_hours_micro"] == 59_678_000
    assert row["ours_gb_hours_micro"] == 59_659_000
    assert row["ce_estimated"] is False
    assert [r["agent"] for r in result["agent_runtime"]] == ["bap_default"]
    # The untagged bucket is reported, not folded into an agent.
    assert result["untagged_micros"] == 10_000


def test_component_snapshot_carries_ce_estimated_flag():
    repo = Repo()
    service(StubCE()).reconcile(
        "2026-08-25", "2026-08-25", repository=repo,
        ledger_agent_days=LEDGER_AGENT_DAYS, ledger_components=LEDGER_COMPONENTS,
    )
    row = repo.items[("RECON#2026-08", "D#2026-08-25#K#component#gateway")]
    assert row["billed_micros"] == 1_000
    assert row["ours_micros"] == 1_000
    assert row["diff_micros"] == 0
    assert row["ce_estimated"] is True


def test_rate_mismatch_is_reported_not_hidden():
    repo = Repo()
    result = service(StubCE(sonnet_output_usd_per_1k="0.012")).reconcile(
        "2026-08-01", "2026-08-25", repository=repo,
        ledger_agent_days={}, ledger_components={},
    )
    rows = {(r["family"], r["routing"], r["tier"]): r for r in result["model_rate"]}
    bad = rows[("claude-sonnet-5", "global", "output")]
    assert bad["match"] is False
    assert bad["billed_usd_per_1m_micro"] == 12_000_000
    assert bad["card_usd_per_1m_micro"] == 10_000_000
    good = rows[("claude-sonnet-5", "regional", "input")]
    assert good["match"] is True and good["card_usd_per_1m_micro"] == 2_200_000
    assert result["mismatches"] == [bad]
    # One row per billed day, keyed by that day — the stub bills one period.
    stored = repo.items[("RECON#2026-08", "D#2026-08-01#K#model_rate#claude-sonnet-5|global|output")]
    assert stored["match"] is False and stored["clean"] is True


def test_unregistered_family_on_the_bill_is_surfaced():
    result = service(StubCE()).reconcile(
        "2026-08-01", "2026-08-25", repository=Repo(), ledger_agent_days={}, ledger_components={},
    )
    unregistered = [r for r in result["model_rate"] if r["registered"] is False]
    assert [(r["family"], r["routing"], r["tier"]) for r in unregistered] == [("claude-opus-5-5", "global", "output")]
    assert result["unregistered_families"] == ["claude-opus-5-5"]


def test_one_hour_cache_writes_and_other_regions_are_not_compared():
    result = service(StubCE()).reconcile(
        "2026-08-01", "2026-08-25", repository=Repo(), ledger_agent_days={}, ledger_components={},
    )
    keys = {(r["family"], r["routing"], r["tier"]) for r in result["model_rate"]}
    assert ("claude-sonnet-5", "regional", "cache_write") not in keys
    assert all(r["region"] == "us-east-1" for r in result["model_rate"])


class FirstPartyCE(StubCE):
    """The first-party usage-type scheme: `Claude4.5Haiku-input-token-count-global`."""

    def get_cost_and_usage(self, **params):
        keys = [g.get("Key") for g in params.get("GroupBy", [])]
        if keys == ["USAGE_TYPE", "REGION"]:
            q = 1000.0
            return {"ResultsByTime": [{
                "TimePeriod": {"Start": "2026-08-01", "End": "2026-09-01"},
                "Groups": [
                    group(["USE1-Claude4.5Haiku-input-token-count-global", "us-east-1"], 0.001 * q, q, "1K tokens"),
                    group(["USE1-Claude4.5Haiku-output-token-count", "us-east-1"], 0.0055 * q, q, "1K tokens"),
                ],
            }]}
        return super().get_cost_and_usage(**params)


def test_first_party_usage_types_map_onto_rate_card_families():
    result = service(FirstPartyCE()).reconcile(
        "2026-08-01", "2026-08-25", repository=Repo(), ledger_agent_days={}, ledger_components={},
    )
    rows = {(r["family"], r["routing"], r["tier"]): r for r in result["model_rate"]}
    assert rows[("claude-haiku-4-5", "global", "input")]["match"] is True
    assert rows[("claude-haiku-4-5", "regional", "output")]["match"] is True
    assert result["unregistered_families"] == []


def test_model_rates_are_asked_for_per_day_not_per_month():
    stub = StubCE()
    service(stub).reconcile("2026-08-01", "2026-08-25", repository=Repo(), ledger_agent_days={}, ledger_components={})
    model_calls = [r for r in stub.requests if [g.get("Key") for g in r.get("GroupBy", [])] == ["USAGE_TYPE", "REGION"]]
    assert model_calls and all(r["Granularity"] == "DAILY" for r in model_calls)


class DailyCE(StubCE):
    """The model-rate query answered day by day, with a value per day per line."""

    def __init__(self, days):
        super().__init__()
        self.days = days  # [(day, [(usage_type, usd_per_1k, quantity_1k)])]

    def get_cost_and_usage(self, **params):
        self.requests.append(params)
        keys = [g.get("Key") for g in params.get("GroupBy", [])]
        if keys == ["USAGE_TYPE", "REGION"]:
            return {"ResultsByTime": [
                {"TimePeriod": {"Start": day, "End": day}, "Groups": [
                    group([usage_type, "us-east-1"], rate * qty, qty, "1K tokens") for usage_type, rate, qty in lines
                ]}
                for day, lines in self.days
            ]}
        return {"ResultsByTime": []}


OPUS55_OUT = "USE1-anthropic.claude-opus-5-5-mantle-output-tokens-global-standard"
OPUS55_IN = "USE1-anthropic.claude-opus-5-5-mantle-input-tokens-global-standard"
SONNET_OUT = "USE1-anthropic.claude-sonnet-5-mantle-output-tokens-global-standard"


def learned_items(repo):
    return {sk: row for (pk, sk), row in repo.items.items() if pk == "RATES#learned"}


def test_an_unregistered_rate_billed_on_two_consecutive_days_is_learned_and_dated():
    repo = Repo()
    result = service(DailyCE([
        ("2026-09-24", [(OPUS55_OUT, 0.0375, 1000.0), (OPUS55_IN, 0.0075, 1000.0)]),
        ("2026-09-25", [(OPUS55_OUT, 0.0375, 1000.0), (OPUS55_IN, 0.0075, 1000.0)]),
        ("2026-09-26", [(OPUS55_OUT, 0.0375, 500.0)]),  # today: still being billed, not evidence
    ])).reconcile("2026-09-20", "2026-09-26", repository=repo, ledger_agent_days={}, ledger_components={})
    learned = learned_items(repo)
    assert set(learned) == {
        "R#claude-opus-5-5|global|output|2026-09-24",
        "R#claude-opus-5-5|global|input|2026-09-24",
    }
    out = learned["R#claude-opus-5-5|global|output|2026-09-24"]
    assert out["usd_per_1m"] == "37.5000" and out["days_observed"] == 2 and out["effective_from"] == "2026-09-24"
    assert sorted(e["tier"] for e in result["learned"]) == ["input", "output"]
    # Before the overlay is installed the rows still say unregistered; the
    # collector reloads and the next pass judges them against the learned rate.
    assert result["unregistered_families"] == ["claude-opus-5-5"]


def test_one_day_is_not_enough_to_learn_a_rate():
    repo = Repo()
    service(DailyCE([
        ("2026-09-25", [(OPUS55_OUT, 0.0375, 1000.0)]),
        ("2026-09-26", [(OPUS55_OUT, 0.0375, 1000.0)]),  # today, incomplete
    ])).reconcile("2026-09-20", "2026-09-26", repository=repo, ledger_agent_days={}, ledger_components={})
    assert learned_items(repo) == {}


def test_two_days_that_disagree_or_do_not_divide_cleanly_teach_nothing():
    repo = Repo()
    service(DailyCE([
        ("2026-09-23", [(OPUS55_OUT, 0.0375, 1000.0)]),
        ("2026-09-24", [(OPUS55_OUT, 0.0300, 1000.0)]),        # a different value: no run
        ("2026-09-25", [(OPUS55_OUT, 0.0301234567, 1000.0)]),  # two prices mixed inside one day
    ])).reconcile("2026-09-20", "2026-09-26", repository=repo, ledger_agent_days={}, ledger_components={})
    assert learned_items(repo) == {}
    stored = repo.items[("RECON#2026-09", "D#2026-09-25#K#model_rate#claude-opus-5-5|global|output")]
    assert stored["clean"] is False


def test_a_price_change_on_a_registered_family_becomes_a_dated_entry_not_a_permanent_mismatch():
    repo = Repo()
    result = service(DailyCE([
        ("2026-09-23", [(SONNET_OUT, 0.010, 1000.0)]),  # the card's $10.00
        ("2026-09-24", [(SONNET_OUT, 0.008, 1000.0)]),  # AWS lowered the price
        ("2026-09-25", [(SONNET_OUT, 0.008, 1000.0)]),
        ("2026-09-26", [(SONNET_OUT, 0.008, 300.0)]),
    ])).reconcile("2026-09-20", "2026-09-26", repository=repo, ledger_agent_days={}, ledger_components={})
    learned = learned_items(repo)
    assert list(learned) == ["R#claude-sonnet-5|global|output|2026-09-24"]
    assert learned["R#claude-sonnet-5|global|output|2026-09-24"]["usd_per_1m"] == "8.0000"
    # The day before the change still matched the card.
    assert repo.items[("RECON#2026-09", "D#2026-09-23#K#model_rate#claude-sonnet-5|global|output")]["match"] is True
    # The finding is judged on the latest billed day, so it is one mismatch, not three.
    assert len(result["mismatches"]) == 1 and result["mismatches"][0]["day"] == "2026-09-26"


def test_a_learned_rate_is_judged_matching_once_installed():
    from data.model_rates import set_learned_rates

    repo = Repo()
    try:
        set_learned_rates([{"family": "claude-sonnet-5", "routing": "global", "tier": "output",
                            "effective_from": "2026-09-24", "usd_per_1m": "8"}])
        result = service(DailyCE([
            ("2026-09-23", [(SONNET_OUT, 0.010, 1000.0)]),
            ("2026-09-25", [(SONNET_OUT, 0.008, 1000.0)]),
        ])).reconcile("2026-09-20", "2026-09-26", repository=repo, ledger_agent_days={}, ledger_components={})
        rows = {r["day"]: r for r in result["model_rate"]}
        assert rows["2026-09-23"]["match"] is True and rows["2026-09-23"]["card_usd_per_1m_micro"] == 10_000_000
        assert rows["2026-09-25"]["match"] is True and rows["2026-09-25"]["card_usd_per_1m_micro"] == 8_000_000
        assert result["mismatches"] == [] and learned_items(repo) == {}
    finally:
        set_learned_rates([])


def test_a_model_used_every_other_day_is_still_learned_and_a_usage_gap_does_not_relearn_it():
    """Consecutive observations, not calendar days; and one entry per value."""
    repo = Repo()
    result = service(DailyCE([
        ("2026-09-10", [(OPUS55_OUT, 0.0200, 1000.0)]),
        ("2026-09-12", [(OPUS55_OUT, 0.0200, 1000.0)]),
        ("2026-09-20", [(OPUS55_OUT, 0.0200, 1000.0)]),
        ("2026-09-21", [(OPUS55_OUT, 0.0200, 1000.0)]),
    ])).reconcile("2026-09-01", "2026-09-26", repository=repo, ledger_agent_days={}, ledger_components={})
    learned = learned_items(repo)
    assert list(learned) == ["R#claude-opus-5-5|global|output|2026-09-10"]
    assert learned["R#claude-opus-5-5|global|output|2026-09-10"]["days_observed"] == 4
    assert len(result["learned"]) == 1


def test_a_first_party_usage_type_for_an_unregistered_model_is_named_like_its_model_id():
    assert BillingService._family_of_usage_type("USE1-Claude4.6Sonnet-output-tokens-cross-region-global") == "claude-sonnet-4-6"
    assert BillingService._family_of_usage_type("USE1-Claude4Opus-input-tokens") == "claude-opus-4"
    assert BillingService._family_of_usage_type("USE1-Claude4.5Haiku-input-token-count-global") == "claude-haiku-4-5"
