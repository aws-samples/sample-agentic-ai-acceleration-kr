"""Per-token rates derived from the account's own bill.

**Why the bill and not the Price List API.** Measured 2026-08-17 against
`AmazonBedrock`: the `model` attribute enumerates five values — Claude 2.0, 2.1,
3 Haiku, 3 Sonnet, Instant — and of 10,175 `usagetype` values exactly five carry
`Claude`, all of them those same pre-3.5 models. Nova is published in full, cache
tiers included; Anthropic's current models are simply absent. So the comment that
justified deleting the model cost estimate was right about the API and wrong about
there being no source: `UnblendedCost / UsageQuantity` on a Cost Explorer token line
*is* the rate the account was charged, and on this account it divides to exact round
numbers ($25.00/1M for opus-5 output global-standard, $0.50/1M for its cache reads).

Three things have to hold for that to be trustworthy, and they are what is pinned
here: the model on the bill must match the model on the harness despite two
different naming schemes, a non-token line must never be mistaken for a rate, and
one model billed at several routing rates must come back as a stated blend rather
than as a single tariff.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.billing_service import (  # noqa: E402
    BillingService,
    display_name,
    model_key,
    _model_part,
    _tier_of,
)


class StubCostExplorer:
    """Returns one canned `GetCostAndUsage` page and records what was asked."""

    def __init__(self, groups):
        self.groups = groups
        self.calls = []

    def get_cost_and_usage(self, **params):
        self.calls.append(params)
        return {
            "ResultsByTime": [
                {"TimePeriod": {"Start": "2026-08-01"}, "Groups": self.groups}
            ]
        }


def group(usage_type, cost, quantity, unit="1K tokens", region=None):
    """One group. `region` fills the second `GroupBy` key when the test needs it.

    Rates differ per billing region, so the read groups by REGION as well as by
    USAGE_TYPE — one request, both answers.
    """
    return {
        "Keys": [usage_type] + ([region] if region else []),
        "Metrics": {
            "UnblendedCost": {"Amount": str(cost), "Unit": "USD"},
            "UsageQuantity": {"Amount": str(quantity), "Unit": unit},
        },
    }


def service_with(groups):
    service = BillingService(region_name="us-east-1")
    service._client = StubCostExplorer(groups)
    return service


# --- naming ------------------------------------------------------------------


def test_two_naming_schemes_reduce_to_the_same_model():
    """One bill carries both, and neither is documented as canonical.

    Partner-operated models bill as `anthropic.claude-opus-5-mantle-…` and
    first-party routing as `Claude4.5Haiku-…`, while the harness reports a Bedrock
    model id like `global.anthropic.claude-sonnet-5` or
    `anthropic.claude-3-5-sonnet-20241022-v2:0`. All of them have to meet.
    """
    assert model_key("global.anthropic.claude-sonnet-5") == model_key(
        "anthropic.claude-sonnet-5-mantle-output-tokens-global-standard"
    )
    assert model_key("us.anthropic.claude-haiku-4-5-20251001") == model_key(
        "Claude4.5Haiku-cache-read-input-token-count-cross-region-global"
    )
    assert model_key("anthropic.claude-3-5-sonnet-20241022-v2:0") == model_key(
        "Claude3.5Sonnet-input-tokens"
    )


def test_a_version_digit_still_separates_two_models():
    """The one thing the normaliser must not do is collapse 3 Sonnet into 3.5.

    Their rates differ, so a false match here prices tokens at another model's
    tariff — which is worse than reporting nothing.
    """
    assert model_key("anthropic.claude-3-sonnet-20240229-v1:0") != model_key(
        "Claude3.5Sonnet-input-tokens"
    )
    assert model_key("anthropic.claude-opus-5") != model_key("anthropic.claude-sonnet-5")


def test_a_build_stamp_is_dropped_and_a_revision_suffix_with_it():
    """`v2` must be tested before it is split, and `:0` before anything else.

    Split first, `v2` becomes `v` and `2`, the stamp pattern never sees it, and the
    version digit survives as if it identified the model. The `:0` revision does the
    same one token later. Either one leaves a dated model id exactly one token away
    from its own rate — a near-miss that reads as "this model has no rate".
    """
    assert model_key("anthropic.claude-3-5-sonnet-20241022-v2:0") == frozenset(
        {"claude", "3", "5", "sonnet"}
    )


def test_the_display_name_drops_the_tier_and_the_serving_stack():
    assert display_name(
        "USE1-anthropic.claude-opus-5-mantle-cache-read-tokens-global-standard"
    ) == "claude-opus-5"
    assert display_name(
        "USE1-Claude4.5Haiku-input-tokens-cross-region-global"
    ) == "Claude4.5Haiku"


def test_the_billing_region_prefix_is_stripped_positionally():
    """`USE1` is a billing region code, derivable from neither the region name nor
    the region code, so it cannot be constructed — only removed."""
    assert _model_part("USE1-Runtime:Consumption-based:vCPU") == (
        "Runtime:Consumption-based:vCPU"
    )
    assert _model_part("APN2-anthropic.claude-opus-5-mantle-input-tokens") == (
        "anthropic.claude-opus-5-mantle-input-tokens"
    )


def test_cache_tiers_are_matched_before_the_bare_input_rule():
    """`cache-read-input-token-count` contains `input`, and is not an input line."""
    assert _tier_of("USE1-Claude4.5Haiku-cache-read-input-token-count-global") == (
        "cache_read"
    )
    assert _tier_of("USE1-Claude4.5Haiku-cache-write-input-token-count-global") == (
        "cache_write"
    )
    assert _tier_of("USE1-anthropic.claude-opus-5-mantle-input-tokens") == "input"
    assert _tier_of("USE1-anthropic.claude-opus-5-mantle-output-tokens") == "output"
    assert _tier_of("USE1-Runtime:Consumption-based:vCPU") is None


# --- derivation --------------------------------------------------------------


def test_the_rate_is_cost_over_quantity_per_model_and_tier():
    service = service_with([
        group("USE1-anthropic.claude-opus-5-mantle-output-tokens-global-standard",
              266.6590, 10666.36),
        group("USE1-anthropic.claude-opus-5-mantle-cache-read-tokens-global-standard",
              1782.1887, 3564377.45),
    ])

    table = service.model_rates("2026-08-01", "2026-08-16")

    identity = model_key("global.anthropic.claude-opus-5")
    # $25.00 and $0.50 per million — the measured figures, to the cent.
    assert round(table["rates"][(identity, "output")]["usd_per_1k"] * 1000, 2) == 25.00
    assert round(table["rates"][(identity, "cache_read")]["usd_per_1k"] * 1000, 2) == 0.50
    assert table["names"][identity] == "claude-opus-5"


def test_several_routings_of_one_model_blend_and_state_their_spread():
    """The token count does not say how a request was routed.

    Measured, opus-5 output is $25.00/1M `global-standard` and $27.50/1M `standard`.
    Summing dollars over summed tokens gives the rate the account actually paid, and
    `spread` carries how far apart the routings were so a caller can say the figure is
    a blend rather than implying a single tariff.
    """
    service = service_with([
        # 1,000 (1K-token) units at $0.025/1K, and 1,000 at $0.0275/1K.
        group("USE1-anthropic.claude-opus-5-mantle-output-tokens-global-standard",
              25.0, 1000.0),
        group("USE1-anthropic.claude-opus-5-mantle-output-tokens-standard",
              27.5, 1000.0),
    ])

    table = service.model_rates("2026-08-01", "2026-08-16")
    entry = table["rates"][(model_key("anthropic.claude-opus-5"), "output")]

    assert round(entry["usd_per_1k"], 6) == 0.02625  # the volume-weighted blend
    assert round(entry["spread"], 6) == 0.0025
    assert entry["billed_1k_tokens"] == 2000.0


def test_a_line_whose_quantity_is_not_tokens_is_not_a_rate():
    """The same service bills queries, GB-months and evaluations.

    Dividing dollars by any of those yields a plausible number in the wrong unit of
    measurement — and AgentCore's own evaluation lines really are quoted per
    `1M Input Tokens`, so the unit varies inside one bill.
    """
    service = service_with([
        group("USE1-Bedrock-Websearch-Queries", 0.24, 20.0, unit="Queries"),
        group("USE1-Knowledge-Base:Consumption-based:Storage", 5.0, 1.0,
              unit="GB-Month"),
        group("USE1-Evaluations:Consumption-based:BuiltIn-Input:Tier1", 0.08, 0.05,
              unit="1M Input Tokens"),
    ])

    table = service.model_rates("2026-08-01", "2026-08-16")

    assert table["rates"] == {}


def test_a_zero_quantity_line_is_not_a_free_model():
    """No denominator, so no rate. A zero-quantity line is a rounding artefact."""
    service = service_with([
        group("USE1-anthropic.claude-opus-5-mantle-input-tokens", 0.0, 0.0),
    ])

    assert service.model_rates("2026-08-01", "2026-08-16")["rates"] == {}


def test_both_bedrock_service_keys_are_asked_for():
    """Anthropic models are partner-operated and bill under the second one.

    Measured on this account: `Amazon Bedrock` held $4.14 while
    `Amazon Bedrock Service` held $5,206.87 of Claude tokens. Filtering on the
    obvious key finds almost nothing and reports it as "no model spend".
    """
    service = service_with([])

    service.model_rates("2026-08-01", "2026-08-16")

    filters = service._client.calls[0]["Filter"]["And"]
    values = {
        f["Dimensions"]["Key"]: f["Dimensions"]["Values"] for f in filters
    }["SERVICE"]
    assert "Amazon Bedrock Service" in values
    assert "Amazon Bedrock" in values


def test_the_window_end_is_made_inclusive():
    """Cost Explorer's `End` is exclusive, so the window's own end drops its last day
    — a failure that reads as "billing is lower than we thought"."""
    service = service_with([])

    service.model_rates("2026-08-01", "2026-08-16")

    assert service._client.calls[0]["TimePeriod"]["End"] == "2026-08-17"


def test_the_rate_comes_from_our_own_billing_region_when_that_region_was_billed():
    """A tariff is per region, and the reduction to a model key hides that.

    `_model_part` strips `USE1-`/`APN2-` and `_MODEL_NOISE` drops `use`/`apn`, so
    every region's lines for one model fold into a single bucket. Seoul's tokens
    cost more than Virginia's, so a blend across both prices our tokens — which are
    billed in one region — at a rate the account never charged us.
    """
    service = service_with([
        group("USE1-anthropic.claude-opus-5-mantle-output-tokens", 25.0, 1000.0,
              region="us-east-1"),
        group("APN2-anthropic.claude-opus-5-mantle-output-tokens", 60.0, 1000.0,
              region="ap-northeast-2"),
    ])

    table = service.model_rates("2026-08-01", "2026-08-16")
    entry = table["rates"][(model_key("anthropic.claude-opus-5"), "output")]

    assert round(entry["usd_per_1k"], 6) == 0.025, "our region's rate, not the blend"
    assert entry["scope"] == "region"
    assert table["region"] == "us-east-1"


def test_a_model_billed_only_elsewhere_still_gets_a_rate_that_says_so():
    """Falling back beats refusing: a cross-region inference profile bills where it
    ran, and refusing every such model would put "추정 불가" on the busiest agents.

    So the account-wide blend is used and `scope` says `account`, which is what the
    caption reads off to warn that regional tariffs are mixed in.
    """
    service = service_with([
        group("APN2-anthropic.claude-opus-5-mantle-output-tokens", 60.0, 2000.0,
              region="ap-northeast-2"),
    ])

    table = service.model_rates("2026-08-01", "2026-08-16")
    entry = table["rates"][(model_key("anthropic.claude-opus-5"), "output")]

    assert round(entry["usd_per_1k"], 6) == 0.03
    assert entry["scope"] == "account"


def test_the_region_is_asked_for_alongside_the_usage_type():
    service = service_with([])

    service.model_rates("2026-08-01", "2026-08-16")

    assert service._client.calls[0]["GroupBy"] == [
        {"Type": "DIMENSION", "Key": "USAGE_TYPE"},
        {"Type": "DIMENSION", "Key": "REGION"},
    ]


def test_only_usage_charges_derive_a_rate():
    """A credit is dollars with no tokens behind it, and the API includes credits
    unless told not to. One landing on a token line would deflate that model's rate
    and every cost priced from it."""
    service = service_with([])

    service.model_rates("2026-08-01", "2026-08-16")

    filters = service._client.calls[0]["Filter"]["And"]
    dimensions = {f["Dimensions"]["Key"]: f["Dimensions"]["Values"] for f in filters}
    assert dimensions["RECORD_TYPE"] == ["Usage"]
    assert "Amazon Bedrock Service" in dimensions["SERVICE"]


def test_rates_are_cached_per_window():
    """$0.01 a call against data that refreshes a few times a day."""
    service = service_with([
        group("USE1-anthropic.claude-opus-5-mantle-input-tokens", 5.0, 1000.0),
    ])

    service.model_rates("2026-08-01", "2026-08-16")
    service.model_rates("2026-08-01", "2026-08-16")

    assert len(service._client.calls) == 1

    service.model_rates("2026-07-01", "2026-07-31")
    assert len(service._client.calls) == 2, "a different window is a different read"


# --- per-agent tags ----------------------------------------------------------


def test_an_all_untagged_bill_reports_nothing_rather_than_one_nameless_bar():
    """The state on this account: `AgentName` is registered and Inactive.

    Cost Explorer answers with a single group keyed `"AgentName$"` — the untagged
    bucket — holding the whole bill. That is "집계 중", not a chart.
    """
    service = BillingService(region_name="us-east-1")
    service._client = StubCostExplorer([
        {
            "Keys": ["AgentName$"],
            "Metrics": {"UnblendedCost": {"Amount": "9.4492609334", "Unit": "USD"}},
        }
    ])

    assert service.per_agent_costs("2026-08-01", "2026-08-16") is None


def test_tagged_dollars_come_back_with_the_untagged_remainder_beside_them():
    """Activation is not retroactive, so untagged spend never fully disappears.

    A chart of only the tagged agents would add up to less than the total with
    nothing on screen to explain the difference.
    """
    service = BillingService(region_name="us-east-1")
    service._client = StubCostExplorer([
        {
            "Keys": ["AgentName$writer"],
            "Metrics": {"UnblendedCost": {"Amount": "3.5", "Unit": "USD"}},
        },
        {
            "Keys": ["AgentName$researcher"],
            "Metrics": {"UnblendedCost": {"Amount": "1.25", "Unit": "USD"}},
        },
        {
            "Keys": ["AgentName$"],
            "Metrics": {"UnblendedCost": {"Amount": "4.7", "Unit": "USD"}},
        },
    ])

    result = service.per_agent_costs("2026-08-01", "2026-08-16")

    assert result["by_agent"] == {"writer": 3.5, "researcher": 1.25}
    assert result["untagged"] == 4.7


def test_the_per_agent_breakdown_is_scoped_like_the_total_it_breaks_down():
    """Without the REGION filter the total and its breakdown answer different
    questions, and once the tag is active the bars can sum past the total they sit
    under — with nothing on screen to explain it."""
    service = BillingService(region_name="us-east-1")
    service._client = StubCostExplorer([])

    service.per_agent_costs("2026-08-01", "2026-08-16")

    filters = service._client.calls[0]["Filter"]["And"]
    dimensions = {f["Dimensions"]["Key"]: f["Dimensions"]["Values"] for f in filters}
    assert dimensions["REGION"] == ["us-east-1"]
    assert dimensions["SERVICE"] == ["Amazon Bedrock AgentCore"]
    assert dimensions["RECORD_TYPE"] == ["Usage"]
