"""The rate card an admin reads into and fills.

The learner waits for two consecutive billed days; an admin may not. This is the
path that closes the gap without loosening the ledger's rule: what the bill or
the Price List can state is offered as a candidate, what neither can is typed in,
and either way the entry lands in the same dated overlay — so the bill can still
correct it — and the turns are repriced at once.
"""
import os
import sys
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import routes.insights as insights  # noqa: E402
from core.auth import AuthUser  # noqa: E402
from data.model_rates import set_learned_rates  # noqa: E402
from services.pricing_service import PricingService, PricingUnavailable  # noqa: E402
from services.rate_card_service import RateCardError, RateCardService, validate_entry  # noqa: E402
from services.usage_service import UsageService  # noqa: E402

from test_usage_ledger import LedgerRepo, turn  # noqa: E402

TODAY = "2026-09-25"
OPUS55 = "global.anthropic.claude-opus-5-5"


class Repo(LedgerRepo):
    def delete(self, pk, sk):
        self.items.pop((pk, sk), None)


class NoPricing:
    def bedrock_model_rates(self):
        raise AssertionError("the overview must not call the Price List")


@pytest.fixture(autouse=True)
def clean_overlay():
    set_learned_rates([])
    yield
    set_learned_rates([])


def service(repo=None, pricing=None):
    repo = repo or Repo()
    usage = UsageService(repo)
    return RateCardService(usage=usage, pricing=pricing or NoPricing()), usage, repo


def recon_rate_row(repo, day, family, routing, tier, usd, *, clean=True, registered=False):
    repo.set_fields(f"RECON#{day[:7]}", f"D#{day}#K#model_rate#{family}|{routing}|{tier}", {
        "billed_usd_per_1m_micro": int(Decimal(usd) * 1_000_000),
        "billed_1k_tokens": 100,
        "clean": clean,
        "registered": registered,
    })


# --- validation ------------------------------------------------------------------


def test_validate_entry_normalises_and_names_the_broken_field():
    ok = validate_entry(
        {"family": " Claude-Opus-5-5 ", "routing": "GLOBAL", "tier": "input", "usd_per_1m": "4.00", "effective_from": "2026-09-23"},
        today=TODAY,
    )
    assert ok == {"family": "claude-opus-5-5", "routing": "global", "tier": "input", "usd_per_1m": "4", "effective_from": "2026-09-23"}
    bad = [
        ({"family": "Claude5.5Opus"}, "family"),
        ({"family": "claude-opus-5-5", "routing": "eu"}, "라우팅"),
        ({"family": "claude-opus-5-5", "routing": "global", "tier": "cached"}, "tier"),
        ({"family": "claude-opus-5-5", "routing": "global", "tier": "input", "usd_per_1m": "four"}, "요율"),
        ({"family": "claude-opus-5-5", "routing": "global", "tier": "input", "usd_per_1m": "-1"}, "요율"),
        ({"family": "claude-opus-5-5", "routing": "global", "tier": "input", "usd_per_1m": "4", "effective_from": "23/09/2026"}, "유효 시작일"),
        ({"family": "claude-opus-5-5", "routing": "global", "tier": "input", "usd_per_1m": "4", "effective_from": "2026-09-26"}, "오늘 이후"),
    ]
    for raw, word in bad:
        with pytest.raises(RateCardError) as excinfo:
            validate_entry(raw, today=TODAY)
        assert word in str(excinfo.value)


# --- overview --------------------------------------------------------------------


def test_overview_shows_an_unregistered_family_with_empty_tiers_and_the_day_it_went_unpriced():
    svc, usage, repo = service()
    turn(usage, model_id=OPUS55, date="2026-09-23", turn_id="t-1:h-1")
    turn(usage, model_id=OPUS55, date="2026-09-24", turn_id="t-1:h-2",
         started_at="2026-09-24T10:00:00Z", ended_at="2026-09-24T10:00:05Z")
    turn(usage, model_id="global.anthropic.claude-sonnet-5", date="2026-09-24", turn_id="t-2:h-1",
         started_at="2026-09-24T10:00:00Z", ended_at="2026-09-24T10:00:05Z", thread_id="t-2")

    view = svc.overview("2026-09-01", TODAY)

    by_key = {(row["family"], row["routing"]): row for row in view["rows"]}
    opus = by_key[("claude-opus-5-5", "global")]
    assert opus["models"] == [OPUS55]
    assert opus["turns"] == 2 and opus["unpriced_turns"] == 2 and opus["unpriced_since"] == "2026-09-23"
    assert opus["tiers"] == {"input": None, "output": None, "cache_read": None, "cache_write": None}
    assert opus["complete"] is False
    sonnet = by_key[("claude-sonnet-5", "global")]
    assert sonnet["unpriced_turns"] == 0 and sonnet["complete"] is True
    assert sonnet["tiers"]["input"] == {"usd_per_1m": "2.00", "source": "card:2026-09-23.1", "effective_from": None}
    assert view["candidates"] == [] and view["entries"] == []


def test_overview_lists_registered_entries_with_their_source_and_shows_them_in_force():
    svc, usage, repo = service()
    turn(usage, model_id=OPUS55)
    repo.set_fields("RATES#learned", "R#claude-opus-5-5|global|input|2026-09-23", {
        "family": "claude-opus-5-5", "routing": "global", "tier": "input",
        "effective_from": "2026-09-23", "usd_per_1m": "4", "source": "admin:a@b.c", "registered_at": "2026-09-25T00:00:00+00:00",
    })
    view = svc.overview("2026-09-01", TODAY)
    row = next(r for r in view["rows"] if r["family"] == "claude-opus-5-5")
    assert row["tiers"]["input"] == {"usd_per_1m": "4", "source": "admin:a@b.c", "effective_from": "2026-09-23"}
    assert row["tiers"]["output"] is None and row["complete"] is False
    assert view["entries"] == [{
        "key": "R#claude-opus-5-5|global|input|2026-09-23", "family": "claude-opus-5-5", "routing": "global",
        "tier": "input", "effective_from": "2026-09-23", "usd_per_1m": "4", "source": "admin:a@b.c",
        "registered_at": "2026-09-25T00:00:00+00:00",
    }]


def test_overview_offers_the_bill_after_one_clean_day_and_counts_the_run_behind_it():
    svc, usage, repo = service()
    turn(usage, model_id=OPUS55)
    # Three billed days: the first at a different value, then two at $4.00 — the
    # candidate is the trailing run, effective from its first day.
    recon_rate_row(repo, "2026-09-22", "claude-opus-5-5", "global", "input", "4.40")
    recon_rate_row(repo, "2026-09-23", "claude-opus-5-5", "global", "input", "4.00")
    recon_rate_row(repo, "2026-09-24", "claude-opus-5-5", "global", "input", "4.00")
    # One day for output is enough to be offered.
    recon_rate_row(repo, "2026-09-24", "claude-opus-5-5", "global", "output", "20.00")
    # A day that did not divide cleanly is not evidence.
    recon_rate_row(repo, "2026-09-24", "claude-opus-5-5", "global", "cache_read", "0.2001", clean=False)
    # Somebody else's model is not our candidate.
    recon_rate_row(repo, "2026-09-24", "claude-fable-5-1", "global", "input", "9.00")
    # A figure the card already has at the same value is not a candidate either.
    recon_rate_row(repo, "2026-09-24", "claude-sonnet-5", "global", "input", "2.00", registered=True)
    turn(usage, model_id="global.anthropic.claude-sonnet-5", turn_id="t-2:h-1", thread_id="t-2")

    view = svc.overview("2026-09-01", TODAY)

    assert view["candidates"] == [
        {"family": "claude-opus-5-5", "routing": "global", "tier": "input", "usd_per_1m": "4", "source": "bill",
         "effective_from": "2026-09-23", "last_day": "2026-09-24", "days_observed": 2, "current_usd_per_1m": None},
        {"family": "claude-opus-5-5", "routing": "global", "tier": "output", "usd_per_1m": "20", "source": "bill",
         "effective_from": "2026-09-24", "last_day": "2026-09-24", "days_observed": 1, "current_usd_per_1m": None},
    ]


def test_overview_offers_the_bill_when_it_differs_from_the_card():
    svc, usage, repo = service()
    turn(usage, model_id="global.anthropic.claude-sonnet-5")
    recon_rate_row(repo, "2026-09-24", "claude-sonnet-5", "global", "output", "12.00", registered=True)
    view = svc.overview("2026-09-01", TODAY)
    assert view["candidates"] == [{
        "family": "claude-sonnet-5", "routing": "global", "tier": "output", "usd_per_1m": "12", "source": "bill",
        "effective_from": "2026-09-24", "last_day": "2026-09-24", "days_observed": 1, "current_usd_per_1m": "10",
    }]


# --- Price List ------------------------------------------------------------------


class FakePricingClient:
    def __init__(self, rows):
        self.rows = rows

    def get_products(self, **params):
        import json
        return {"PriceList": [json.dumps({
            "product": {"attributes": {"usagetype": usage_type, "model": "Claude"}},
            "terms": {"OnDemand": {"x": {"priceDimensions": {"y": {
                "pricePerUnit": {"USD": usd}, "unit": unit, "description": "",
            }}}}},
        }) for usage_type, usd, unit in self.rows]}


def test_price_list_rows_are_parsed_into_family_routing_tier_per_million():
    pricing = PricingService(region_name="us-east-1")
    pricing._client = FakePricingClient([
        ("USE1-anthropic.claude-haiku-4-5-mantle-input-tokens-standard", "0.0011000000", "1K tokens"),
        ("USE1-anthropic.claude-haiku-4-5-mantle-cache-write-tokens-1h-standard", "0.0022", "1K tokens"),
        ("USE1-anthropic.claude-opus-5-5-mantle-output-tokens-global-standard", "0.02", "1K tokens"),
        ("USE1-Claude3Haiku-input-tokens", "0.00025", "1K tokens"),
        ("USE1-anthropic.claude-opus-5-5-reserved-1-month-input-tokens-per-minute", "0.18", "1K TPM Hour"),
    ])
    rows = pricing.bedrock_model_rates()
    assert [(r["family"], r["routing"], r["tier"], r["usd_per_1m"]) for r in rows] == [
        ("claude-haiku-4-5", "regional", "input", "1.1"),
        ("claude-opus-5-5", "global", "output", "20"),
    ]


def test_published_returns_only_what_the_card_lacks_or_disputes():
    class Pricing:
        def bedrock_model_rates(self):
            return [
                {"family": "claude-haiku-4-5", "routing": "regional", "tier": "input", "usd_per_1m": "1.1", "usage_type": "u1", "model": "m"},
                {"family": "claude-opus-5-5", "routing": "global", "tier": "output", "usd_per_1m": "20", "usage_type": "u2", "model": "m"},
                {"family": "claude-sonnet-4-5", "routing": "global", "tier": "input", "usd_per_1m": "3", "usage_type": "u3", "model": "m"},
            ]
    svc, usage, repo = service(pricing=Pricing())
    out = svc.published(["claude-haiku-4-5", "claude-opus-5-5"])
    assert out["published"] == 3
    assert out["candidates"] == [{
        "family": "claude-opus-5-5", "routing": "global", "tier": "output", "usd_per_1m": "20",
        "usage_type": "u2", "model": "m", "source": "price-list", "current_usd_per_1m": None,
    }]


# --- register / remove -----------------------------------------------------------


def test_register_stores_the_entries_under_the_learner_key_and_reprices_at_once():
    svc, usage, repo = service()
    turn(usage, model_id=OPUS55, input_tokens=1_000_000, output_tokens=100_000)
    assert repo.get("AGENTS#2026-09", "D#2026-09-23#A#rec-1")["unpriced_turns"] == 1

    report = svc.register([
        {"family": "claude-opus-5-5", "routing": "global", "tier": "input", "usd_per_1m": "4", "effective_from": "2026-09-23", "source": "bill"},
        {"family": "claude-opus-5-5", "routing": "global", "tier": "output", "usd_per_1m": "20", "effective_from": "2026-09-23"},
    ], by="admin@example.com", today=TODAY)

    assert report["saved"] == ["R#claude-opus-5-5|global|input|2026-09-23", "R#claude-opus-5-5|global|output|2026-09-23"]
    assert report["repriced"] == {"repriced": 1, "newly_priced": 1, "still_unpriced": 0, "unpriced": 0}
    stored_in = repo.get("RATES#learned", "R#claude-opus-5-5|global|input|2026-09-23")
    stored_out = repo.get("RATES#learned", "R#claude-opus-5-5|global|output|2026-09-23")
    assert stored_in["source"] == "bill" and stored_in["registered_by"] == "admin@example.com"
    assert stored_out["source"] == "admin:admin@example.com" and stored_out["usd_per_1m"] == "20"
    event = repo.get("TURNS#2026-09", "T#t-1:h-1")
    assert event["model_cost_micros"] == 4_000_000 + 2_000_000
    day = repo.get("AGENTS#2026-09", "D#2026-09-23#A#rec-1")
    assert day["priced_turns"] == 1 and day["unpriced_turns"] == 0 and day["model_cost_micros"] == 6_000_000
    # And the next turn on that model is priced as it is recorded.
    turn(usage, model_id=OPUS55, turn_id="t-1:h-2", input_tokens=500_000, output_tokens=0)
    assert repo.get("TURNS#2026-09", "T#t-1:h-2")["model_cost_micros"] == 2_000_000


def test_register_writes_nothing_when_one_row_is_invalid():
    svc, usage, repo = service()
    with pytest.raises(RateCardError):
        svc.register([
            {"family": "claude-opus-5-5", "routing": "global", "tier": "input", "usd_per_1m": "4", "effective_from": "2026-09-23"},
            {"family": "claude-opus-5-5", "routing": "global", "tier": "output", "usd_per_1m": "twenty", "effective_from": "2026-09-23"},
        ], by="a", today=TODAY)
    assert repo.query_prefix("RATES#learned", "R#") == []


def test_remove_deletes_the_entry_and_un_prices_the_turns_it_covered():
    svc, usage, repo = service()
    svc.register([
        {"family": "claude-opus-5-5", "routing": "global", "tier": "input", "usd_per_1m": "4", "effective_from": "2026-09-23"},
    ], by="a", today=TODAY)
    turn(usage, model_id=OPUS55, input_tokens=1_000_000, output_tokens=0)
    assert repo.get("TURNS#2026-09", "T#t-1:h-1")["model_cost_micros"] == 4_000_000
    day_before = dict(repo.get("AGENTS#2026-09", "D#2026-09-23#A#rec-1"))
    assert day_before["priced_turns"] == 1 and day_before["model_cost_micros"] == 4_000_000

    report = svc.remove("R#claude-opus-5-5|global|input|2026-09-23", today=TODAY)

    assert report["repriced"] == {"repriced": 1, "newly_priced": 0, "still_unpriced": 0, "unpriced": 1}
    assert repo.get("RATES#learned", "R#claude-opus-5-5|global|input|2026-09-23") is None
    assert repo.get("TURNS#2026-09", "T#t-1:h-1")["model_cost_micros"] is None
    day = repo.get("AGENTS#2026-09", "D#2026-09-23#A#rec-1")
    assert day["priced_turns"] == 0 and day["unpriced_turns"] == 1 and day["model_cost_micros"] == 0
    with pytest.raises(RateCardError):
        svc.remove("R#claude-opus-5-5|global|input|2026-09-23", today=TODAY)
    with pytest.raises(RateCardError):
        svc.remove("garbage", today=TODAY)


# --- routes ----------------------------------------------------------------------


def _admin():
    return AuthUser(sub="sub-admin", username="uuid-admin", groups=["admin"])


def _plain():
    return AuthUser(sub="sub-1", username="uuid-1", groups=[])


def test_rate_routes_are_admin_only_and_translate_service_errors(monkeypatch):
    from fastapi import HTTPException

    class Directory:
        def emails(self, subs):
            return {"sub-admin": "admin@example.com"}

    svc, usage, repo = service()
    monkeypatch.setattr(insights, "_rate_card_override", svc)
    monkeypatch.setattr(insights, "_usage_override", usage)
    monkeypatch.setattr(insights, "_directory_override", Directory())

    for call in (
        lambda: insights.get_rate_card(days=30, user=_plain()),
        lambda: insights.fetch_published_rates(user=_plain()),
        lambda: insights.register_rates(body=insights.RateRegisterRequest(entries=[]), user=_plain()),
        lambda: insights.delete_rate(key="R#x|global|input|2026-09-01", user=_plain()),
    ):
        with pytest.raises(HTTPException) as excinfo:
            call()
        assert excinfo.value.status_code == 403

    with pytest.raises(HTTPException) as excinfo:
        insights.register_rates(body=insights.RateRegisterRequest(entries=[]), user=_admin())
    assert excinfo.value.status_code == 400

    body = insights.RateRegisterRequest(entries=[insights.RateEntryRequest(
        family="claude-opus-5-5", routing="global", tier="input", usd_per_1m="4", effective_from="2026-09-01",
    )])
    out = insights.register_rates(body=body, user=_admin())
    assert out["saved"] == ["R#claude-opus-5-5|global|input|2026-09-01"]
    assert repo.get("RATES#learned", "R#claude-opus-5-5|global|input|2026-09-01")["source"] == "admin:admin@example.com"

    view = insights.get_rate_card(days=30, user=_admin())
    assert view["entries"][0]["source"] == "admin:admin@example.com" and "window" in view

    with pytest.raises(HTTPException) as excinfo:
        insights.delete_rate(key="R#claude-opus-5-5|global|input|2026-01-01", user=_admin())
    assert excinfo.value.status_code == 404
    assert insights.delete_rate(key="R#claude-opus-5-5|global|input|2026-09-01", user=_admin())["removed"]


def test_fetch_route_reports_an_unreachable_price_list_as_503(monkeypatch):
    from fastapi import HTTPException

    class Pricing:
        def bedrock_model_rates(self):
            raise PricingUnavailable("boom")

    svc, usage, repo = service(pricing=Pricing())
    monkeypatch.setattr(insights, "_rate_card_override", svc)
    monkeypatch.setattr(insights, "_usage_override", usage)
    with pytest.raises(HTTPException) as excinfo:
        insights.fetch_published_rates(user=_admin())
    assert excinfo.value.status_code == 503
