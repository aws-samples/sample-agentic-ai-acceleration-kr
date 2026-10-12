"""The repository rate card prices tokens deterministically.

The model id we invoke with names the routing (`global.` = global-standard,
anything else = the regional `standard` tariff), and Cost Explorer shows every
Anthropic model on this account billed at exact round rates with regional a
constant 1.1× global. So a (model, routing, tier) rate is a lookup, never a
blend, and the cost of a turn is an integer number of micro-dollars.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from decimal import Decimal  # noqa: E402

from data.model_rates import RATE_CARD_VERSION, cost_micros, resolve_rate  # noqa: E402


def test_global_prefix_resolves_global_routing():
    rate = resolve_rate("global.anthropic.claude-sonnet-5")
    assert rate["family"] == "claude-sonnet-5"
    assert rate["routing"] == "global"
    assert rate["usd_per_1m"]["input"] == Decimal("2.00")
    assert rate["version"] == RATE_CARD_VERSION


def test_regional_prefix_is_ten_percent_more():
    rate = resolve_rate("us.anthropic.claude-sonnet-5")
    assert rate["routing"] == "regional"
    assert rate["usd_per_1m"]["output"] == Decimal("11.00")


def test_bare_model_id_is_regional_and_dated_suffix_matches_family():
    rate = resolve_rate("anthropic.claude-haiku-4-5-20251001-v1:0")
    assert rate["routing"] == "regional"
    assert rate["family"] == "claude-haiku-4-5"
    # Price List publishes the regional Haiku 4.5 row; it wins over ×1.1.
    assert rate["usd_per_1m"]["cache_write"] == Decimal("1.375")


def test_family_match_does_not_swallow_a_longer_version():
    # opus-5-5 is not opus-5; unregistered means None, never a wrong price.
    assert resolve_rate("global.anthropic.claude-opus-5-5") is None
    assert resolve_rate("global.anthropic.claude-opus-5")["family"] == "claude-opus-5"


def test_unknown_or_missing_model_returns_none():
    assert resolve_rate(None) is None
    assert resolve_rate("") is None
    assert resolve_rate("amazon.nova-pro-v1:0") is None


def test_cost_is_integer_micros_and_exact():
    rate = resolve_rate("global.anthropic.claude-sonnet-5")
    counters = {
        "input_tokens": 1_000_000,
        "output_tokens": 100_000,
        "cache_read_tokens": 500_000,
        "cache_write_tokens": 10_000,
    }
    # 2.00 + 1.00 + 0.10 + 0.025 = 3.125 USD
    assert cost_micros(counters, rate) == 3_125_000


def test_cost_rounds_half_up_at_the_micro():
    rate = resolve_rate("global.anthropic.claude-haiku-4-5-20251001-v1:0")
    # 1 cache-read token at $0.10/1M = 0.1 micro -> 0
    assert cost_micros({"cache_read_tokens": 1}, rate) == 0
    # 7 input tokens at $1.00/1M = 7 micro
    assert cost_micros({"input_tokens": 7}, rate) == 7
    # 5 cache-read tokens = 0.5 micro -> rounds up to 1
    assert cost_micros({"cache_read_tokens": 5}, rate) == 1


def test_a_learned_family_resolves_from_its_effective_date_and_only_its_tiers():
    from data.model_rates import learned_rates, set_learned_rates

    try:
        set_learned_rates([
            {"family": "claude-opus-5-5", "routing": "global", "tier": "input", "effective_from": "2026-09-25", "usd_per_1m": "7.5"},
            {"family": "claude-opus-5-5", "routing": "global", "tier": "output", "effective_from": "2026-09-25", "usd_per_1m": "37.5"},
        ])
        assert resolve_rate("global.anthropic.claude-opus-5-5", "2026-09-24") is None
        rate = resolve_rate("global.anthropic.claude-opus-5-5", "2026-09-25")
        assert rate["family"] == "claude-opus-5-5" and rate["version"].endswith("+ce:2026-09-25")
        assert rate["usd_per_1m"] == {"input": Decimal("7.5"), "output": Decimal("37.5")}
        # A regional id is a different tariff the bill has not shown: nothing is derived.
        assert resolve_rate("us.anthropic.claude-opus-5-5", "2026-09-25") is None
        # The longer learned family still is not swallowed by the static claude-opus-5.
        assert resolve_rate("global.anthropic.claude-opus-5", "2026-09-25")["family"] == "claude-opus-5"
        assert cost_micros({"input_tokens": 1_000_000, "output_tokens": 0}, rate) == 7_500_000
        # A tier the bill has not shown makes the turn unpriceable, not cheaper.
        assert cost_micros({"input_tokens": 10, "cache_read_tokens": 5}, rate) is None
        assert len(learned_rates()) == 2
    finally:
        set_learned_rates([])


def test_a_learned_price_change_applies_from_its_day_and_the_static_rate_before():
    from data.model_rates import set_learned_rates

    try:
        set_learned_rates([
            {"family": "claude-sonnet-5", "routing": "global", "tier": "output", "effective_from": "2026-10-01", "usd_per_1m": "8"},
        ])
        before = resolve_rate("global.anthropic.claude-sonnet-5", "2026-09-30")
        after = resolve_rate("global.anthropic.claude-sonnet-5", "2026-10-01")
        latest = resolve_rate("global.anthropic.claude-sonnet-5")
        assert before["usd_per_1m"]["output"] == Decimal("10.00") and before["version"] == RATE_CARD_VERSION
        assert after["usd_per_1m"]["output"] == Decimal("8") and after["usd_per_1m"]["input"] == Decimal("2.00")
        assert latest["usd_per_1m"]["output"] == Decimal("8")
    finally:
        set_learned_rates([])


def test_malformed_learned_entries_are_dropped():
    from data.model_rates import learned_rates, set_learned_rates

    try:
        set_learned_rates([
            {"family": "x", "routing": "global", "tier": "input", "effective_from": "2026-09-25", "usd_per_1m": "abc"},
            {"family": "x", "routing": "sideways", "tier": "input", "effective_from": "2026-09-25", "usd_per_1m": "1"},
            {"family": "x", "routing": "global", "tier": "input", "effective_from": "bad", "usd_per_1m": "1"},
        ])
        assert learned_rates() == []
    finally:
        set_learned_rates([])
