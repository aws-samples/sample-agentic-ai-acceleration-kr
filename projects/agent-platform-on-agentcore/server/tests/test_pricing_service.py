"""The published AgentCore rate card.

Two hardcoded constants used to stand in for this whole module, and they were
*right* — verified again against the live bill on 2026-08-17 — which is the reason
they survived so long. A transcribed rate is a rate that goes stale silently, and
those two covered two of the twenty-five rates the service is billed at.

The one thing that is easy to get wrong here is the filter, and getting it wrong
produces an empty rate card rather than an error: `usagetype` carries a billing
region prefix (`USE1-`) that is derivable from neither the region name nor the
region code, and `get_products` only does exact matches. So a filter written as
`Runtime:Consumption-based:vCPU` returns zero products in every region — measured,
against 1 for the prefixed form. Filtering on `regionCode` + `type` sidesteps the
prefix entirely and returns all 25 in one page.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.model_prices import (  # noqa: E402
    RUNTIME_GB_HOUR_USD,
    RUNTIME_VCPU_HOUR_USD,
)
from services.pricing_service import PricingService, PricingUnavailable  # noqa: E402


def product(usage_type, resource, usd, unit):
    """One `PriceList` entry, in the shape the API returns it: a JSON *string*."""
    return json.dumps({
        "product": {
            "attributes": {
                "regionCode": "us-east-1",
                "servicecode": "AmazonBedrockAgentCore",
                "resource": resource,
                "usagetype": usage_type,
                "type": "Consumption-based",
            }
        },
        "terms": {
            "OnDemand": {
                "x.y": {
                    "priceDimensions": {
                        "x.y.z": {
                            "unit": unit,
                            "pricePerUnit": {"USD": f"{usd:.10f}"},
                            "description": f"${usd} per {unit} for {usage_type}",
                        }
                    }
                }
            }
        },
    })


class StubPricing:
    def __init__(self, pages, error=None):
        self.pages = pages
        self.error = error
        self.calls = []

    def get_products(self, **params):
        self.calls.append(params)
        if self.error:
            raise self.error
        page = self.pages[len(self.calls) - 1]
        response = {"PriceList": page}
        if len(self.calls) < len(self.pages):
            response["NextToken"] = "more"
        return response


MEASURED = [
    product("USE1-Runtime:Consumption-based:vCPU", "CPU", 0.0895, "vCPU-Hours"),
    product("USE1-Runtime:Consumption-based:Memory", "Memory", 0.00945, "GB-Hours"),
    product("USE1-BrowserTool:Consumption-based:vCPU", "CPU", 0.0895, "vCPU-Hours"),
    product("USE1-Gateway:Consumption-based:API-Invocations", "API-Invocations",
            0.000005, "Invocations"),
    product("USE1-Knowledge-Base:Consumption-based:Retrieval", "", 0.001, "Queries"),
]


def service_with(pages, error=None):
    service = PricingService(region_name="us-east-1")
    service._client = StubPricing(pages, error=error)
    return service


def test_the_filter_never_mentions_usagetype():
    """The prefix is not constructible, so a usagetype filter silently matches nothing.

    Pinned as a test because the failure has no symptom: the call succeeds, the
    dashboard shows no rates, and nothing anywhere says why.
    """
    service = service_with([MEASURED])

    service.rate_card()

    fields = {f["Field"] for f in service._client.calls[0]["Filters"]}
    assert fields == {"regionCode", "type"}
    assert "usagetype" not in fields


def test_the_runtime_pair_matches_the_measured_bill():
    """$5.2727 / 557.96 GB-hours = $0.009450 and $0.6054 / 6.76 = $0.08956 on the
    live bill, against these published rates. The estimate's arithmetic is sound;
    what the dashboard needed was for the rates to stop being transcribed."""
    service = service_with([MEASURED])

    rates = service.runtime_rates()

    assert rates == {"vcpu_hour": 0.0895, "gb_hour": 0.00945, "source": "price_list"}


def test_the_rate_card_carries_the_unit_with_every_rate():
    """A rate without its unit is a number.

    $0.00945 per GB-hour and $0.00945 per GB-month are the same digits and a factor
    of 720 apart, and this service really does bill both shapes.
    """
    service = service_with([MEASURED])

    card = service.rate_card()

    assert card["USE1-Runtime:Consumption-based:Memory"]["unit"] == "GB-Hours"
    assert card["USE1-Gateway:Consumption-based:API-Invocations"]["unit"] == (
        "Invocations"
    )


def test_components_agree_with_the_billing_module():
    """The point of the card is to put a published rate next to a billed line item,
    so the two have to agree on what a component is."""
    from services.billing_service import component_of

    service = service_with([MEASURED])
    card = service.rate_card()

    for usage_type, entry in card.items():
        assert entry["component"] == component_of(usage_type), usage_type


def test_pagination_is_followed_to_exhaustion():
    service = service_with([[MEASURED[0]], [MEASURED[1]]])

    card = service.rate_card()

    assert len(service._client.calls) == 2
    assert len(card) == 2


def test_a_read_failure_falls_back_to_the_pinned_pair_and_says_so():
    """A published rate we could not read is not a reason to show no estimate.

    It is a reason to say which of the two sources the number came from, which is
    what `source` is for — and what the dashboard renders as a warning.
    """
    service = service_with([], error=RuntimeError("AccessDeniedException"))

    rates = service.runtime_rates()

    assert rates["vcpu_hour"] == RUNTIME_VCPU_HOUR_USD
    assert rates["gb_hour"] == RUNTIME_GB_HOUR_USD
    assert rates["source"] == "pinned"


def test_half_a_pair_is_refused_rather_than_mixed():
    """Mixing one live rate with one pinned rate and labelling the result as read
    from the API is a provenance claim that is not true of half the figure."""
    service = service_with([[MEASURED[0]]])  # vCPU only

    rates = service.runtime_rates()

    assert rates["source"] == "pinned"
    assert rates["gb_hour"] == RUNTIME_GB_HOUR_USD


def test_an_empty_card_is_an_error_and_is_never_cached_as_success():
    """An empty result is the exact symptom of a filter that matches nothing."""
    service = service_with([[]])

    try:
        service.rate_card()
    except PricingUnavailable:
        pass
    else:
        raise AssertionError("expected PricingUnavailable")

    # Not cached, so a fixed filter takes effect on the next read rather than after
    # a day.
    service._client.pages = [MEASURED]
    service._client.calls = []
    assert service.rate_card()


def test_the_card_is_cached_between_reads():
    # One page, so a second `get_products` can only mean the cache was missed —
    # with two pages the stub would page within a single read and prove nothing.
    service = service_with([MEASURED])

    service.rate_card()
    service.rate_card()

    assert len(service._client.calls) == 1
