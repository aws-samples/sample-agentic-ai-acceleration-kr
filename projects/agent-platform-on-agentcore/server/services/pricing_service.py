"""AgentCore consumption rates, read from the AWS Price List API.

**Two hardcoded constants used to stand in for this whole module.**
`data/model_prices` held `RUNTIME_VCPU_HOUR_USD` and `RUNTIME_GB_HOUR_USD`,
transcribed from a measurement taken by hand on 2026-08-01, with a comment saying
where they came from. They were right — verified again below — but a transcribed
rate is a rate that goes stale silently, and it covered two of the twenty-five
rates this service is billed at.

**One call gets all of them.** Filtering `AmazonBedrockAgentCore` on
`regionCode` + `type=Consumption-based` returns 25 products for us-east-1 with no
pagination (measured 2026-08-17), covering Runtime, BrowserTool, CodeInterpreter,
Memory, Gateway, WebSearchTool, Knowledge-Base and Evaluations. So the rate card
is a single request, cached for a day, and `runtime_rates` reads the live runtime
pair out of it instead of trusting the two pinned constants.

**Filter on `regionCode` and `type`, never on `usagetype`.** The `usagetype`
attribute carries a billing region *prefix* (`USE1-Runtime:Consumption-based:vCPU`)
and `get_products` only does exact matches, so a filter written as
`Runtime:Consumption-based:vCPU` returns zero products in every region — a query
that looks correct, answers 200, and yields an empty rate card. Measured: the
prefixed form returns 1 product, the unprefixed form returns 0. The prefix is
derived from the region name and is not the region code, so it is not
constructible; the component is parsed out of the returned usagetype instead.

**The endpoint is us-east-1 regardless of which region's prices are wanted.** The
Price List API is served from two regions only; the region being priced is a
filter, not the client's region.

Rates are facts about a tariff, not about a window, so this caches for 24h and is
never on the request's critical path: `runtime_rates` falls back to the pinned
constants rather than making the caller wait or fail. A published rate we could
not read is not a reason to show no estimate — it is a reason to say which of the
two sources the number came from, which `source` does.
"""
import logging
import re
import threading
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional

import boto3

from core.config import AWS_REGION
from data.model_prices import RUNTIME_GB_HOUR_USD, RUNTIME_VCPU_HOUR_USD

logger = logging.getLogger(__name__)

SERVICE_CODE = "AmazonBedrockAgentCore"

# The Price List API lives in us-east-1 and ap-south-1 only. The region whose
# prices we want is a filter on the query, not the endpoint.
_PRICING_ENDPOINT_REGION = "us-east-1"

# A day. A tariff changes on the order of quarters, and the fallback below means a
# miss costs provenance rather than the figure.
_DEFAULT_TTL = 86400

# usagetype infix -> the component key `billing_service.component_of` produces, so
# a rate can be shown beside the line item it prices. Ordered longest-first for the
# same reason it is there: `Knowledge-Base` must match before any shorter infix.
_COMPONENTS = (
    ("CodeInterpreter", "code_interpreter"),
    ("Knowledge-Base", "knowledge_base"),
    ("WebSearchTool", "web_search"),
    ("BrowserTool", "browser"),
    ("Evaluations", "evaluations"),
    ("Runtime", "runtime"),
    ("Gateway", "gateway"),
    ("Memory", "memory"),
    ("Policy", "policy"),
)


class PricingUnavailable(Exception):
    """The Price List API could not be read.

    Callers that need a runtime rate do not see this — `runtime_rates` catches it
    and reports the pinned constants with `source: "pinned"`. It surfaces only to
    `rate_card`, whose whole purpose is to show what the API published.
    """


def _component_of(usage_type: str) -> str:
    """`"USE1-Runtime:Consumption-based:vCPU"` -> `"runtime"`.

    Deliberately the same mapping as `billing_service.component_of`, because the
    point of this module is to put a published rate next to a billed line item and
    they have to agree on what a component is. Unrecognised falls through to
    `other`, never dropped: a dropped rate reads as "AWS does not price this".
    """
    for infix, component in _COMPONENTS:
        if infix in usage_type:
            return component
    return "other"


def _dimension(product: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The one on-demand price dimension of a product, or None.

    Every consumption product measured carries exactly one term with exactly one
    dimension. Written as a search rather than an index so a product that ever
    carries two does not raise inside a rate card that is meant to degrade.
    """
    for term in (product.get("terms") or {}).get("OnDemand", {}).values():
        for dimension in (term.get("priceDimensions") or {}).values():
            price = (dimension.get("pricePerUnit") or {}).get("USD")
            if price is None:
                continue
            try:
                return {
                    "usd": float(price),
                    "unit": dimension.get("unit") or "",
                    "description": dimension.get("description") or "",
                }
            except (TypeError, ValueError):
                continue
    return None


class PricingService:
    """The published AgentCore consumption rate card for one region."""

    def __init__(
        self,
        region_name: Optional[str] = None,
        cache_seconds: int = _DEFAULT_TTL,
    ):
        # The region being *priced*, not the endpoint being called.
        self.region_name = region_name or AWS_REGION
        self._client = None
        self._cache_seconds = cache_seconds
        self._cache: Dict[str, Any] = {}
        self._cached_at = 0.0
        self._lock = threading.Lock()

    @property
    def client(self):
        if self._client is None:
            with self._lock:
                if self._client is None:
                    self._client = boto3.client(
                        "pricing", region_name=_PRICING_ENDPOINT_REGION
                    )
        return self._client

    def rate_card(self) -> Dict[str, Dict[str, Any]]:
        """Every published consumption rate for the region, keyed by usagetype.

        Raises `PricingUnavailable`. Each entry carries the component it belongs
        to and the unit it is charged in, because a rate without its unit is a
        number: $0.00945 per GB-hour and $0.00945 per GB-month are the same digits
        and a factor of 720 apart.
        """
        # The client is resolved before the lock: the `client` property takes the
        # same non-reentrant lock, and taking it twice deadlocks the first call of
        # a fresh instance. `billing_service` shipped exactly this bug once.
        client = self.client

        with self._lock:
            fresh = (
                self._cache
                and time.monotonic() - self._cached_at < self._cache_seconds
            )
            if fresh:
                return dict(self._cache)

        params: Dict[str, Any] = {
            "ServiceCode": SERVICE_CODE,
            "Filters": [
                {
                    "Type": "TERM_MATCH",
                    "Field": "regionCode",
                    "Value": self.region_name,
                },
                {
                    "Type": "TERM_MATCH",
                    "Field": "type",
                    "Value": "Consumption-based",
                },
            ],
            "MaxResults": 100,
        }

        import json

        rates: Dict[str, Dict[str, Any]] = {}
        while True:
            try:
                response = client.get_products(**params)
            except Exception as exc:
                raise PricingUnavailable(str(exc)) from exc
            for raw in response.get("PriceList", []):
                # PriceList entries are JSON *strings*, one per product.
                try:
                    product = json.loads(raw) if isinstance(raw, str) else raw
                except (TypeError, ValueError):
                    continue
                attributes = (product.get("product") or {}).get("attributes") or {}
                usage_type = attributes.get("usagetype")
                if not usage_type:
                    continue
                dimension = _dimension(product)
                if dimension is None:
                    continue
                rates[usage_type] = {
                    "component": _component_of(usage_type),
                    "resource": attributes.get("resource") or "",
                    **dimension,
                }
            token = response.get("NextToken")
            if not token:
                break
            params["NextToken"] = token

        if not rates:
            # An empty card is the exact symptom of a filter that matches nothing,
            # which is how the `usagetype` filter failed. Never cached as success.
            raise PricingUnavailable(
                f"No Consumption-based products for {self.region_name}"
            )

        with self._lock:
            self._cache = rates
            self._cached_at = time.monotonic()
        return dict(rates)

    # --- Bedrock model tariff (what the Price List publishes of it) --------------

    # `USE1-anthropic.claude-haiku-4-5-mantle-cache-read-tokens-standard`:
    # family, then any qualifiers, then the tier, an optional cache TTL, then the
    # routing. Measured 2026-09-24: `AmazonBedrockService` publishes exactly these
    # five rows for Haiku 4.5 (input, output, cache-read, cache-write, cache-write-1h)
    # and nothing for Sonnet 5 or Opus 5.x. The 1h cache-write row is a different
    # product from the 5-minute one our ledger meters, and is skipped.
    _MODEL_USAGE_TYPE = re.compile(
        r"anthropic\.(?P<family>claude-[a-z]+-\d{1,2}(?:-\d{1,2})?)"
        r"(?:-[a-z0-9]+)*?-(?P<tier>input|output|cache-read|cache-write)-tokens"
        r"(?:-(?P<ttl>\d+h))?(?P<long>-long-ctx)?-(?P<routing>(?:global-)?standard)$"
    )
    _MODEL_SERVICE_CODE = "AmazonBedrockService"

    def bedrock_model_rates(self) -> List[Dict[str, Any]]:
        """Every Anthropic token rate the Price List publishes for this region,
        as (family, routing, tier, USD per 1M tokens) rows. Raises
        `PricingUnavailable`; returns an empty list when the service publishes
        nothing, because "nothing published" is a true answer here — the static
        card exists precisely because it usually is."""
        import json

        client = self.client
        params: Dict[str, Any] = {
            "ServiceCode": self._MODEL_SERVICE_CODE,
            "Filters": [{"Type": "TERM_MATCH", "Field": "regionCode", "Value": self.region_name}],
            "MaxResults": 100,
        }
        rows: List[Dict[str, Any]] = []
        while True:
            try:
                response = client.get_products(**params)
            except Exception as exc:
                raise PricingUnavailable(str(exc)) from exc
            for raw in response.get("PriceList", []):
                try:
                    product = json.loads(raw) if isinstance(raw, str) else raw
                except (TypeError, ValueError):
                    continue
                attributes = (product.get("product") or {}).get("attributes") or {}
                usage_type = str(attributes.get("usagetype") or "")
                match = self._MODEL_USAGE_TYPE.search(usage_type)
                if not match or match.group("ttl"):
                    continue
                dimension = _dimension(product)
                if dimension is None:
                    continue
                unit = dimension["unit"].lower()
                if unit == "1k tokens":
                    per_1m = Decimal(str(dimension["usd"])) * 1000
                elif unit in ("1m tokens", "1 million tokens"):
                    per_1m = Decimal(str(dimension["usd"]))
                else:
                    continue
                rows.append({
                    "family": match.group("family"),
                    "routing": "global" if match.group("routing").startswith("global") else "regional",
                    # A long-context line is its own tier: the price of a call
                    # whose prompt crossed the family's threshold.
                    "tier": ("long_" if match.group("long") else "") + match.group("tier").replace("-", "_"),
                    "usd_per_1m": format(per_1m.normalize(), "f"),
                    "usage_type": usage_type,
                    "model": attributes.get("model") or "",
                })
            token = response.get("NextToken")
            if not token:
                break
            params["NextToken"] = token
        return rows

    def _rate_for(self, rates: Dict[str, Dict[str, Any]], suffix: str) -> Optional[float]:
        """The rate whose usagetype ends in `suffix`, matched on the suffix.

        Suffix-matched because the region prefix is not constructible from the
        region code — see the module docstring.
        """
        for usage_type, entry in rates.items():
            if usage_type.endswith(suffix):
                return entry["usd"]
        return None

    def runtime_rates(self) -> Dict[str, Any]:
        """The two rates the runtime estimate multiplies, and where they came from.

        Never raises and never returns None for either rate: the pinned constants
        in `data/model_prices` are the fallback, and `source` says which was used
        so the dashboard can state the provenance instead of implying the API was
        read when it was not.
        """
        try:
            rates = self.rate_card()
        except PricingUnavailable as exc:
            logger.info("Price List unavailable, using pinned rates: %s", exc)
            return {
                "vcpu_hour": RUNTIME_VCPU_HOUR_USD,
                "gb_hour": RUNTIME_GB_HOUR_USD,
                "source": "pinned",
            }
        except Exception:
            logger.warning("Price List read failed", exc_info=True)
            return {
                "vcpu_hour": RUNTIME_VCPU_HOUR_USD,
                "gb_hour": RUNTIME_GB_HOUR_USD,
                "source": "pinned",
            }

        vcpu = self._rate_for(rates, "Runtime:Consumption-based:vCPU")
        gb = self._rate_for(rates, "Runtime:Consumption-based:Memory")
        if vcpu is None or gb is None:
            # Half a rate card prices half an estimate. Fall back to the pair that
            # is known to be consistent rather than mixing a live rate with a
            # pinned one and labelling the result as read from the API.
            logger.info("Price List has no runtime pair for %s", self.region_name)
            return {
                "vcpu_hour": RUNTIME_VCPU_HOUR_USD,
                "gb_hour": RUNTIME_GB_HOUR_USD,
                "source": "pinned",
            }
        return {"vcpu_hour": vcpu, "gb_hour": gb, "source": "price_list"}
