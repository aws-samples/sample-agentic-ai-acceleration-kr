"""A model with a long-context card is priced per call, by that call's prompt.

Haiku 5.5 is billed 0.10/0.50 per 1M tokens when a call's prompt is 100K tokens
or fewer and 0.50/2.50 when it is longer, on its own `…-long-ctx-…` usagetypes
(Cost Explorer, 2026-10-08..09). The ledger keeps each tier's total and records
the part spent in long-context calls as a subset (`long_input_tokens` ⊆
`input_tokens`); the bill keeps the long lines as their own tiers so the learner
can never mistake the long price for the plain one.
"""
import asyncio
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from data import model_rates  # noqa: E402
from data.model_rates import cost_micros, long_context_threshold, resolve_rate, set_learned_rates  # noqa: E402
from models.common import StreamConfig, StreamRequest  # noqa: E402
from services.billing_service import _tier_of, model_key  # noqa: E402
from services.pricing_service import PricingService  # noqa: E402
from services.rate_card_service import RateCardError, validate_entry  # noqa: E402
from services.streaming_service import StreamingService  # noqa: E402
from services.usage_service import UsageService  # noqa: E402
from test_reconciliation import DailyCE, Repo, learned_items, service as billing_service  # noqa: E402
from test_usage_is_recorded_per_turn import (  # noqa: E402
    VALUES,
    RecordingUsage,
    StubRegistry,
    StubThreadService,
)
from test_usage_ledger import LedgerRepo  # noqa: E402
import pytest


@pytest.fixture(autouse=True)
def _allow_every_model_this_file_uses(monkeypatch):
    """The override check reads ALLOWED_MODELS from the environment; a fresh
    checkout has none, and these tests are about pricing, not the allow-list."""
    import core.config as cfg
    monkeypatch.setattr(cfg, "ALLOWED_MODELS", [
        "global.anthropic.claude-haiku-5-5",
        "global.anthropic.claude-sonnet-5-5",
        "global.anthropic.claude-opus-5-5",
    ])


HAIKU55 = "global.anthropic.claude-haiku-5-5"
PLAIN = {"input": "0.10", "output": "0.50", "cache_read": "0.01", "cache_write": "0.125"}
LONG = {"long_input": "0.50", "long_output": "2.50", "long_cache_read": "0.05", "long_cache_write": "0.625"}


def install_haiku55(cards=(PLAIN, LONG)):
    set_learned_rates([
        {"family": "claude-haiku-5-5", "routing": "global", "tier": tier,
         "usd_per_1m": usd, "effective_from": "2026-10-08", "source": "test"}
        for card in cards for tier, usd in card.items()
    ])


def teardown_function(_):
    set_learned_rates([])


# --- the rate card ----------------------------------------------------------


def test_only_a_family_with_a_long_card_has_a_threshold():
    assert long_context_threshold(HAIKU55) == 100_000
    assert long_context_threshold("us.anthropic.claude-haiku-5-5") == 100_000
    assert long_context_threshold("global.anthropic.claude-sonnet-5-5") is None
    assert long_context_threshold("global.anthropic.claude-haiku-4-5-20251001-v1:0") is None
    assert long_context_threshold(None) is None


def test_long_tokens_are_priced_on_the_long_card_and_the_rest_on_the_plain_one():
    install_haiku55()
    rate = resolve_rate(HAIKU55, "2026-10-09")
    # 1M input of which 400K were in long calls; 100K output of which 20K were.
    counters = {
        "input_tokens": 1_000_000, "long_input_tokens": 400_000,
        "output_tokens": 100_000, "long_output_tokens": 20_000,
    }
    # 600K × 0.10 + 400K × 0.50 + 80K × 0.50 + 20K × 2.50 = 0.06 + 0.20 + 0.04 + 0.05
    assert cost_micros(counters, rate) == 350_000


def test_a_turn_without_long_counters_prices_exactly_as_before():
    install_haiku55()
    rate = resolve_rate(HAIKU55, "2026-10-09")
    assert cost_micros({"input_tokens": 1_000_000, "output_tokens": 100_000}, rate) == 150_000


def test_a_long_call_without_a_registered_long_card_leaves_the_turn_unpriced():
    """Pricing it on the plain card would put a floor on the page as a total."""
    install_haiku55(cards=(PLAIN,))
    rate = resolve_rate(HAIKU55, "2026-10-09")
    assert cost_micros({"input_tokens": 200_000, "long_input_tokens": 200_000}, rate) is None
    assert cost_micros({"input_tokens": 200_000}, rate) == 20_000


def test_the_regional_derivation_carries_a_static_long_card_too(monkeypatch):
    monkeypatch.setitem(model_rates.MODEL_RATES, "claude-test-9", {
        "global": {**{t: Decimal(v) for t, v in PLAIN.items()}, **{t: Decimal(v) for t, v in LONG.items()}},
    })
    rate = model_rates.effective_rate("claude-test-9", "regional", "2026-10-09")
    assert rate["usd_per_1m"]["long_input"] == Decimal("0.550")
    assert rate["usd_per_1m"]["input"] == Decimal("0.110")


def test_long_tiers_are_accepted_by_the_overlay_and_the_admin_form():
    set_learned_rates([{"family": "claude-haiku-5-5", "routing": "global", "tier": "long_output",
                        "usd_per_1m": "2.5", "effective_from": "2026-10-08"}])
    assert resolve_rate(HAIKU55, "2026-10-09")["usd_per_1m"] == {"long_output": Decimal("2.5")}
    entry = validate_entry({"family": "claude-haiku-5-5", "routing": "global", "tier": "long_input",
                            "usd_per_1m": "0.5", "effective_from": "2026-10-08"}, today="2026-10-10")
    assert entry["tier"] == "long_input"
    try:
        validate_entry({"family": "claude-haiku-5-5", "routing": "global", "tier": "long_ctx_input",
                        "usd_per_1m": "0.5", "effective_from": "2026-10-08"}, today="2026-10-10")
    except RateCardError:
        pass
    else:
        raise AssertionError("an unknown tier was accepted")


# --- the bill ---------------------------------------------------------------

H55_IN = "USE1-anthropic.claude-haiku-5-5-mantle-input-tokens-global-standard"
H55_OUT = "USE1-anthropic.claude-haiku-5-5-mantle-output-tokens-global-standard"
H55_LONG_IN = "USE1-anthropic.claude-haiku-5-5-mantle-input-tokens-long-ctx-global-standard"
H55_LONG_OUT = "USE1-anthropic.claude-haiku-5-5-mantle-output-tokens-long-ctx-global-standard"


def test_a_long_context_line_is_its_own_tier_of_the_same_model():
    assert _tier_of(H55_LONG_IN) == "long_input"
    assert _tier_of(H55_LONG_OUT) == "long_output"
    assert _tier_of(H55_IN) == "input"
    assert model_key(H55_LONG_IN) == model_key(H55_IN)


def test_two_days_billed_only_long_teach_the_long_card_not_the_plain_one():
    """2026-10-08 billed Haiku 5.5 on long-ctx lines only. Folded into `input`,
    two such days in a row would have taught 0.50 as the plain input rate."""
    repo = Repo()
    billing_service(DailyCE([
        ("2026-10-08", [(H55_LONG_IN, 0.0005, 400.0), (H55_LONG_OUT, 0.0025, 4.0)]),
        ("2026-10-09", [(H55_LONG_IN, 0.0005, 300.0), (H55_LONG_OUT, 0.0025, 2.0)]),
        ("2026-10-10", []),
    ])).reconcile("2026-10-01", "2026-10-10", repository=repo, ledger_agent_days={}, ledger_components={})
    learned = learned_items(repo)
    assert set(learned) == {
        "R#claude-haiku-5-5|global|long_input|2026-10-08",
        "R#claude-haiku-5-5|global|long_output|2026-10-08",
    }
    assert learned["R#claude-haiku-5-5|global|long_input|2026-10-08"]["usd_per_1m"] == "0.5000"


def test_a_day_with_both_cards_keeps_each_rate_clean():
    """Summed into one tier, 0.10 and 0.50 lines divide to a rate that is neither."""
    repo = Repo()
    billing_service(DailyCE([
        ("2026-10-08", [(H55_IN, 0.0001, 100.0), (H55_LONG_IN, 0.0005, 300.0)]),
        ("2026-10-09", [(H55_IN, 0.0001, 200.0), (H55_LONG_IN, 0.0005, 300.0)]),
        ("2026-10-10", []),
    ])).reconcile("2026-10-01", "2026-10-10", repository=repo, ledger_agent_days={}, ledger_components={})
    learned = learned_items(repo)
    assert learned["R#claude-haiku-5-5|global|input|2026-10-08"]["usd_per_1m"] == "0.1000"
    assert learned["R#claude-haiku-5-5|global|long_input|2026-10-08"]["usd_per_1m"] == "0.5000"


class _PriceList:
    def __init__(self, rows):
        self.rows = rows

    def get_products(self, **_params):
        import json

        return {"PriceList": [json.dumps({
            "product": {"attributes": {"usagetype": usage_type, "model": "m"}},
            "terms": {"OnDemand": {"t": {"priceDimensions": {"d": {
                "unit": "1K tokens", "pricePerUnit": {"USD": usd}}}}}},
        }) for usage_type, usd in self.rows]}


def test_the_price_list_reads_a_long_context_row_as_its_long_tier():
    svc = PricingService(region_name="us-east-1")
    svc._client = _PriceList([
        ("USE1-anthropic.claude-haiku-5-5-mantle-input-tokens-long-ctx-global-standard", "0.0005000000"),
        ("USE1-anthropic.claude-haiku-5-5-mantle-input-tokens-global-standard", "0.0001000000"),
    ])
    rows = {(r["tier"], r["usd_per_1m"]) for r in svc.bedrock_model_rates()}
    assert rows == {("long_input", "0.5"), ("input", "0.1")}


# --- the ledger -------------------------------------------------------------


def _turn(svc, **overrides):
    base = dict(
        agent_record_id="rec-1", owner_sub="sub-1", input_tokens=0, output_tokens=0,
        tool_calls={}, date="2026-10-09", thread_id="t-1", turn_id="t-1:h-1", model_id=HAIKU55,
        started_at="2026-10-09T10:00:00Z", ended_at="2026-10-09T10:00:05Z",
    )
    base.update(overrides)
    return svc.record_turn(**base)


EVENT = ("TURNS#2026-10", "T#t-1:h-1")


def test_a_turn_records_its_long_subset_and_is_priced_on_both_cards():
    install_haiku55()
    repo = LedgerRepo()
    svc = UsageService(repository=repo)
    svc.ensure_learned_rates = lambda: None  # the overlay is installed by the test

    out = _turn(svc, input_tokens=1_000_000, output_tokens=100_000,
                long_tokens={"long_input_tokens": 400_000, "long_output_tokens": 20_000,
                             "long_cache_read_tokens": 0, "long_cache_write_tokens": 0})

    assert out["cost_micros"] == 350_000
    event = repo.get(*EVENT)
    assert event["input_tokens"] == 1_000_000 and event["long_input_tokens"] == 400_000
    assert "long_cache_read_tokens" not in event  # zeros are not written
    models = repo.get("AGENT_MODELS#2026-10", f"D#2026-10-09#A#rec-1#M#{HAIKU55}")
    assert models["long_input_tokens"] == 400_000 and models["model_cost_micros"] == 350_000


def test_a_second_flush_keeps_the_first_flushs_long_subset():
    install_haiku55()
    repo = LedgerRepo()
    svc = UsageService(repository=repo)
    svc.ensure_learned_rates = lambda: None
    _turn(svc, input_tokens=200_000, long_tokens={"long_input_tokens": 200_000})

    _turn(svc, input_tokens=0, output_tokens=10_000, turns=0)

    event = repo.get(*EVENT)
    assert event["long_input_tokens"] == 200_000
    # 200K × 0.50 long input + 10K × 0.50 plain output
    assert event["model_cost_micros"] == 105_000


def test_repricing_reads_the_long_subset_back():
    repo = LedgerRepo()
    svc = UsageService(repository=repo)
    svc.ensure_learned_rates = lambda: None
    # Written before any Haiku 5.5 rate existed: unpriced, but the split is kept.
    _turn(svc, input_tokens=200_000, long_tokens={"long_input_tokens": 200_000})
    assert "model_cost_micros" not in repo.get(*EVENT)

    install_haiku55()
    svc.load_learned_rates = lambda: None
    report = svc.reprice_events("2026-10-09", "2026-10-09")

    assert report["newly_priced"] == 1
    assert repo.get(*EVENT)["model_cost_micros"] == 100_000


# --- the stream -------------------------------------------------------------


class TwoCallClient:
    """One turn, two model calls: a 150K-token prompt, then a 20K one."""

    async def execute_stream(self, thread_id, values, config=None, actor_id=None):
        yield {"event": {"messageStart": {"id": "m-1"}}}
        yield {"event": {"metadata": {"usage": {
            "inputTokens": 50_000, "outputTokens": 300, "cacheReadInputTokens": 100_000}}}}
        yield {"event": {"metadata": {"usage": {"inputTokens": 20_000, "outputTokens": 700}}}}
        yield {"event": {"messageStop": {"messageId": "m-1", "fullText": "Done"}}}


def _drain(model_id):
    threads = StubThreadService()
    usage = RecordingUsage()
    client = TwoCallClient()
    service = StreamingService(thread_service=threads, agentcore_client=client,
                               usage_service=usage, registry_service=StubRegistry())
    service._get_agent_client = lambda config=None: client
    request = StreamRequest(values=VALUES, config=StreamConfig(registry_record_id="rec-1", model_id=model_id))

    async def scenario():
        response = await service.stream_thread_execution("t-1", request, owner_sub="sub-1")
        async for _ in response.body_iterator:
            pass

    asyncio.run(scenario())
    assert len(usage.turns) == 1
    return usage.turns[0]


def test_the_stream_counts_only_the_calls_whose_prompt_crossed_the_threshold():
    turn = _drain(HAIKU55)
    assert turn["input_tokens"] == 70_000 and turn["cache_read_tokens"] == 100_000
    assert turn["long_tokens"] == {
        "long_input_tokens": 50_000, "long_output_tokens": 300,
        "long_cache_read_tokens": 100_000, "long_cache_write_tokens": 0,
    }


def test_a_model_with_one_card_never_records_a_long_subset():
    turn = _drain("global.anthropic.claude-sonnet-5-5")
    assert not any(turn["long_tokens"].values())
