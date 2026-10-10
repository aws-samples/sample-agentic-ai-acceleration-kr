"""Cost Explorer reads for the billed tier of the insights dashboard.

Three measurements shape this module, all taken against the live account on
2026-08-16 and recorded as facts 27-31 of the design:

1. **The account is shared.** Month-to-date it held $8,743 of EC2 and $3,519 of
   SageMaker belonging to other workloads. Every read is therefore filtered to
   `SERVICE = "Amazon Bedrock AgentCore"`, and even then the figure is an
   *account* total — other tenants use AgentCore too. The route labels it so.
2. **`USAGE_TYPE` decomposes AgentCore along the same seams as our estimates.**
   `…Runtime:Consumption-based:vCPU` is the billed twin of the
   `CPUUsed-vCPUHours` estimate, which is what makes reconciliation possible
   without cost allocation tags.
3. **Each call costs $0.01** against data that refreshes a few times a day. The
   six-hour cache is the feature, not an optimisation, and it is keyed by window
   so switching 7d/30d does not silently re-bill on every toggle.

**The bill is also the only citable source of per-token model rates**, which is
what `model_rates` is for. The Price List API publishes token rates for Nova and
for Claude 3 and earlier, and nothing at all for the models this platform runs:
measured 2026-08-17, `AmazonBedrock`'s `model` attribute enumerates five values
(Claude 2.0, 2.1, 3 Haiku, 3 Sonnet, Instant) and of 10,175 `usagetype` values
exactly five carry `Claude`, all of them those same pre-3.5 models. That absence
is why a hand-transcribed price table used to sit in `data/model_prices`, and why
it was deleted.

Cost Explorer answers the question the Price List API will not, because
`UnblendedCost / UsageQuantity` on a token line **is** the rate the account was
charged. Measured on this account for 2026-08-01..16:

    USE1-anthropic.claude-opus-5-mantle-output-tokens-global-standard
      $266.6590 / 10,666.36 (1K tokens) = $0.025/1K   -> $25.00 per 1M
    USE1-anthropic.claude-opus-5-mantle-cache-read-tokens-global-standard
      $1,782.1887 / 3,564,377.45        = $0.0005/1K  -> $0.50 per 1M

Both divide to exact round numbers, which is the tell that this is a tariff and
not an artefact of blending. The rate is derived from *other tenants' volume* as
well as ours, and that is fine: what we take from the bill is the **rate**, and
the **quantity** is counted by `usage_service` per agent. Their product is
attributable to us, which the account total never was.

Note which service key. Anthropic models are partner-operated and bill under
`Amazon Bedrock Service` ($5,206.87 this month), not `Amazon Bedrock` ($4.14) —
reading the obvious one finds almost nothing and reports it as "no model spend".

**Two scope rules, both learned the hard way.** The API is not the console: it
includes credits, refunds and tax unless a `RECORD_TYPE` filter says otherwise, so
every read here asks for `Usage` only — a credit on a token line would have
deflated that model's rate and every cost priced from it. And a tariff is per
*region*, while `model_key` deliberately erases the region prefix so the two naming
schemes can meet; `model_rates` therefore groups by REGION as well and prefers our
own region's lines, falling back to the account-wide blend with `scope` saying which.
Refusing the fallback would blank the busiest agents, because a cross-region
inference profile bills where it ran.

Like `telemetry_service`, this module imports nothing from `usage_service`: the
sources fail independently and the merge belongs in the route.
"""
import logging
import re
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import boto3

from core.config import AWS_REGION, PLATFORM

logger = logging.getLogger(__name__)

SERVICE_NAME = "Amazon Bedrock AgentCore"

# Both keys, because Anthropic models bill under the second one. Measured on this
# account: `Amazon Bedrock` held $4.14 (Titan embeddings, a custom-model import and
# web search) while `Amazon Bedrock Service` held $5,206.87 of Claude tokens.
MODEL_SERVICE_NAMES = ("Amazon Bedrock", "Amazon Bedrock Service")

# The cost allocation tag a per-agent breakdown needs. Registered in the billing
# tag registry on this account and **Inactive** as of 2026-08-17, which is why
# `per_agent` still reports nothing: cost data accrues only forward from
# activation, so activating it does not backfill. `scripts/activate_cost_tags.py`
# flips it.
AGENT_TAG_KEY = "AgentName"

# **The API is not the console.** Cost Explorer's console excludes credits, refunds
# and tax by default; `GetCostAndUsage` includes every record type unless filtered.
# This module was written against what the console showed, so a credit landing on
# AgentCore would have pulled the billed total below what the account was charged —
# and a credit on a *token* line would have deflated the derived rate and every model
# cost priced from it. `Usage` is also the record type a tariff is defined on, which
# is what `model_rates` needs.
RECORD_TYPES = ("Usage",)

# Six hours. Cost Explorer publishes a few times a day, so a shorter TTL buys
# staleness that is no fresher and charges $0.01 for the privilege.
_DEFAULT_TTL = 21600

# Usage-type infix -> component. Ordered longest-first so `Knowledge-Base` is
# matched before any shorter substring could claim it.
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

# Quantity units worth keeping separately, because each is the billed twin of a
# metric we already estimate from.
_VCPU_UNITS = ("vcpu-hours", "vcpu hours")
_GB_UNITS = ("gb-hours", "gb hours")

# The only quantity unit a per-token rate may be derived from. Gated rather than
# assumed: the same service bills queries, GB-months and evaluations, and dividing
# dollars by any of those produces a plausible number in the wrong currency of
# measurement. Evaluations under AgentCore are quoted per `1M Input Tokens`, so the
# unit really does vary within one bill.
_TOKEN_UNIT = "1k tokens"

# The four tiers Bedrock bills a prompt at, longest infix first so
# `cache-read-input-token-count` cannot be claimed by the bare `input` rule.
#
# Both naming schemes appear in one bill and neither is documented as canonical:
# partner-operated models use `…-cache-read-tokens-…` while first-party routing
# uses `…-cache-read-input-token-count-…`. Matching on the shared infix covers
# both without guessing which scheme a future model will use.
_TIERS = (
    ("cache-read", "cache_read"),
    ("cache-write", "cache_write"),
    ("output", "output"),
    ("input", "input"),
)

# Words in a usagetype that say nothing about *which model* it is: the tier, the
# unit, the routing profile and the vendor. Removed before a model key is built, so
# that `anthropic.claude-opus-5-mantle-output-tokens-global-standard` and
# `Claude5Opus-output-token-count-cross-region` reduce to the same model.
#
# `mantle` is in here because it is a serving-stack name, not a model name — it
# appears on every partner-operated line and on no first-party one, so leaving it
# in would make the two schemes name different models.
_MODEL_NOISE = frozenset({
    "anthropic", "amazon", "meta", "mistral", "cohere", "ai21", "stability",
    "deepseek", "writer", "luma", "twelvelabs", "qwen", "openai",
    "mantle", "tokens", "token", "count", "text", "input", "output",
    "cache", "read", "write", "creation", "global", "standard", "cross",
    "region", "batch", "flex", "priority", "provisioned", "latency",
    "optimized", "us", "eu", "apac", "apn", "use", "usw", "afs", "ape",
    "v", "on", "demand", "ondemand", "inference", "tier", "long", "ctx",
})

# A build stamp or an SDK version suffix inside a model id: `20241022`, `v2`.
# Dropped because the bill names a model family and a version but never a build
# date, so leaving these in would stop every dated model id from ever matching.
#
# Six digits is the floor deliberately: it catches `20241022` and `20251001` and
# leaves `4`, `5` and `3` alone, which are the digits that distinguish one model
# from another and must survive.
_STAMP = re.compile(r"^(?:\d{6,}|v\d+)$")

# The `:0` a Bedrock model id ends in — a revision, not part of the model's
# identity, and the bill never carries one. Removed before tokenising: split off
# as its own token it made `…-sonnet-20241022-v2:0` carry a stray `0` that no
# usagetype has, so every dated model id failed to match by exactly one token.
_REVISION = re.compile(r":\d+$")


def model_key(name: str) -> frozenset:
    """A model designation reduced to the tokens that identify the model.

    A **set**, not a sequence, because the two sides of the match order their
    words differently and both orders are correct: the bill writes
    `claude-opus-5` and `Claude4.5Haiku`, a Bedrock model id writes
    `global.anthropic.claude-sonnet-5` or `anthropic.claude-3-5-sonnet-20241022-v2:0`.
    Comparing sequences would refuse every match; comparing sets accepts exactly
    the pairs that name the same family and version.

    Version digits are kept and build stamps are dropped, which is the line that
    matters: `{claude,3,5,sonnet}` (3.5 Sonnet) and `{claude,3,sonnet}` (3 Sonnet)
    stay different models with different rates, while
    `claude-haiku-4-5-20251001` and `Claude4.5Haiku` become the same one.
    """
    stripped = _REVISION.sub("", name or "")
    tokens = set()
    for part in re.split(r"[^0-9A-Za-z]+", stripped):
        if not part:
            continue
        lowered = part.lower()
        # The stamp test runs on the whole part, before it is split. `v2` split
        # into `v` and `2` never matches the pattern, so the version digit
        # survived as if it identified the model — which is how a dated id ended
        # up one token away from its own rate.
        if _STAMP.match(lowered):
            continue
        # Split a run-together designation: `Claude2` -> `claude`, `2`.
        for piece in re.findall(r"\d+|[A-Za-z]+", lowered):
            if piece in _MODEL_NOISE:
                continue
            tokens.add(piece)
    return frozenset(tokens)


# The marker Bedrock puts on the usagetypes of calls billed on a family's long
# card (`…-input-tokens-long-ctx-global-standard`).
_LONG_CONTEXT_INFIX = "long-ctx"


def _tier_of(usage_type: str) -> Optional[str]:
    """`input`, `output`, `cache_read` or `cache_write` — `long_`-prefixed for a
    long-context line — or None if not a token line.

    The long lines are their own tiers because they are their own price. Folded
    into the plain tier, a day that billed only long-context calls (2026-10-08:
    Haiku 5.5 at 0.50 against a 0.10 card) read as a clean new input rate, and
    two such days in a row would have taught the learner to price every Haiku 5.5
    call five times over; a day with both kinds summed to a rate that is neither."""
    lowered = usage_type.lower()
    for infix, tier in _TIERS:
        if infix in lowered:
            return f"long_{tier}" if _LONG_CONTEXT_INFIX in lowered else tier
    return None


def _model_part(usage_type: str) -> str:
    """The usagetype with its billing region prefix removed.

    The prefix (`USE1-`, `APN2-`) is a billing region code, not an AWS region code,
    and it is not derivable from either — so it is stripped positionally. Only a
    leading all-caps-and-digits run followed by `-` qualifies, which no model
    designation is.
    """
    return re.sub(r"^[A-Z0-9]{3,5}-", "", usage_type or "")


def display_name(usage_type: str) -> str:
    """A usagetype reduced to something worth printing as a model name.

    `anthropic.claude-opus-5-mantle-cache-read-tokens-global-standard` ->
    `claude-opus-5`. Cut at the tier infix rather than reassembled from
    `model_key`, because that returns a set and a model name read back out of a set
    is in arbitrary word order.

    The vendor prefix and the serving-stack suffix go too: they are constant across
    every line from one scheme, so they carry no information in a list where every
    row has them, and dropping them is what lets a partner-operated name sit in the
    same column as a first-party one without looking like a different kind of thing.
    """
    part = _model_part(usage_type)
    lowered = part.lower()
    cut = len(part)
    for infix, _ in _TIERS:
        index = lowered.find(infix)
        if index > 0:
            cut = min(cut, index)
    name = part[:cut].strip("-.").strip()
    name = re.sub(r"^(?:anthropic|amazon|meta|mistral|cohere|ai21)\.", "", name)
    return re.sub(r"[-.]mantle$", "", name) or part


class BillingUnavailable(Exception):
    """Cost Explorer could not be read.

    The routes turn this into `sources.billing: false` and a collapsed card,
    never a 5xx. Every other figure on the page is still correct.
    """


def component_of(usage_type: str) -> str:
    """`"USE1-Runtime:Consumption-based:vCPU"` -> `"runtime"`.

    Region prefixes vary (`USE1`, `APN2`, `USW2`) and AWS adds usage types
    without warning, so this matches on an infix and funnels the unrecognised
    into `other` rather than dropping them. A dropped line item would make the
    component breakdown quietly disagree with its own total.
    """
    if "DataTransfer" in usage_type or "CloudFront" in usage_type:
        return "data_transfer"
    for infix, component in _COMPONENTS:
        if infix in usage_type:
            return component
    return "other"


# The first-party usage-type name: `Claude4.6Sonnet`, `Claude4Opus`.
_FIRST_PARTY_NAME = re.compile(r"^Claude(\d+)(?:\.(\d+))?([A-Za-z]+)$")

# A billed rate is "clean" when it is an exact 4-decimal USD-per-1M figure —
# every Anthropic tariff on this account is (5.00, 27.50, 0.275, 1.375; measured
# over 66 daily lines 2026-09-10..24, all exact to the micro). A day whose rate
# is not clean is a day two prices were mixed, or a rounding artefact on a tiny
# volume; either way it is not evidence and is never learned from.
_RATE_QUANTUM = Decimal("0.0001")
_RATE_CLEAN_TOLERANCE = Decimal("0.000001")
# Thousands of tokens a day must carry before its rate counts: CE rounds the
# dollar amount, and on a handful of tokens that rounding is most of the rate.
_RATE_MIN_1K_TOKENS = 1.0


def _next_day(date: str) -> str:
    """`"2026-08-16"` -> `"2026-08-17"`.

    Cost Explorer's `TimePeriod.End` is exclusive, so the window's own end date
    would drop its last day — a failure that reads as "billing is lower than we
    thought" rather than as a bug.
    """
    from datetime import datetime, timedelta

    parsed = datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)
    return parsed.strftime("%Y-%m-%d")


class BillingService:
    """Billed AgentCore cost, filtered to this service and cached by window."""

    def __init__(
        self,
        region_name: Optional[str] = None,
        cache_seconds: int = _DEFAULT_TTL,
        platform: Optional[str] = None,
    ):
        self.region_name = region_name or AWS_REGION
        self.platform = platform if platform is not None else PLATFORM
        self._client = None
        self._cache_seconds = cache_seconds
        self._cache: Dict[Any, Any] = {}
        self._lock = threading.Lock()

    @property
    def client(self):
        # Cost Explorer is a global endpoint served out of us-east-1; the region
        # here only selects credentials and signing.
        if self._client is None:
            with self._lock:
                if self._client is None:
                    self._client = boto3.client("ce", region_name="us-east-1")
        return self._client

    def _dimension(self, key: str, values: List[str]) -> Dict[str, Any]:
        return {"Dimensions": {"Key": key, "Values": values}}

    def _scope(
        self,
        service_names: Tuple[str, ...],
        region: bool = True,
        platform: Optional[str] = None,
    ) -> Dict[str, Any]:
        """The filter every read shares: this service, this region, usage only —
        and, for the reads that are ours to claim, our Platform tag.

        `model_rates` passes no platform on purpose: a per-token tariff is not
        tenant-specific, so it is derived from the whole account's volume. The
        total and the per-agent breakdown pass it, because a shared account books
        other tenants' AgentCore usage under the same service and only the tag
        separates ours.
        """
        clauses = [self._dimension("SERVICE", list(service_names))]
        if region:
            clauses.append(self._dimension("REGION", [self.region_name]))
        clauses.append(self._dimension("RECORD_TYPE", list(RECORD_TYPES)))
        if platform:
            clauses.append({"Tags": {"Key": "Platform", "Values": [platform]}})
        return {"And": clauses}

    def agentcore_costs(self, start_date: str, end_date: str) -> Dict[str, Any]:
        """Billed cost for the window, grouped by component.

        Cached per window for `cache_seconds`. A failure is never cached: the
        usual cause is a permission someone is in the middle of granting.

        `region` and `record_types` travel with the figure. It is one region's usage
        charges, not the account's whole AgentCore bill, and a card that cannot state
        its own scope invites the reader to reconcile it against something else.
        """
        key = (start_date, end_date)
        # The client is resolved before the lock. Doing it inside would take the
        # same non-reentrant lock the `client` property takes and deadlock on the
        # first call of a fresh instance — a bug this feature already shipped
        # once, invisible because every test pre-set `_client`.
        client = self.client

        with self._lock:
            cached = self._cache.get(key)
            if cached and time.monotonic() - cached[0] < self._cache_seconds:
                return cached[1]

            results: List[Dict[str, Any]] = []
            params: Dict[str, Any] = {
                "TimePeriod": {"Start": start_date, "End": _next_day(end_date)},
                "Granularity": "DAILY",
                "Metrics": ["UnblendedCost", "UsageQuantity"],
                "Filter": self._scope((SERVICE_NAME,), platform=self.platform),
                "GroupBy": [{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
            }
            while True:
                try:
                    response = client.get_cost_and_usage(**params)
                except Exception as exc:
                    raise BillingUnavailable(str(exc)) from exc
                results.extend(response.get("ResultsByTime", []))
                token = response.get("NextPageToken")
                if not token:
                    break
                params["NextPageToken"] = token

            value = self._fold(results)
            value["region"] = self.region_name
            value["record_types"] = list(RECORD_TYPES)
            self._cache[key] = (time.monotonic(), value)
            return value

    def per_agent_costs(self, start_date: str, end_date: str) -> Optional[Dict[str, Any]]:
        """Billed AgentCore cost per agent, from the `AgentName` cost allocation tag.

        `None` when the tag carries no dollars yet, which is the state to expect
        and not an error: the key is registered in this account's billing tag
        registry and **Inactive** as of 2026-08-17, so Cost Explorer answers with
        one group — `"AgentName$"`, the untagged bucket — holding the whole
        $9.45. Activation is not retroactive either, so even afterwards the days
        before it stay untagged. Returning `None` is what renders "집계 중"
        instead of a chart of one nameless bar.

        When values do appear, `untagged` travels with them. A per-agent chart that
        silently omits the majority of the bill is worse than no chart: the reader
        sums the bars, gets a number far below the total, and has no way to see why.
        """
        client = self.client
        key = ("per_agent", start_date, end_date)

        with self._lock:
            cached = self._cache.get(key)
            if cached and time.monotonic() - cached[0] < self._cache_seconds:
                return cached[1]

            params: Dict[str, Any] = {
                "TimePeriod": {"Start": start_date, "End": _next_day(end_date)},
                "Granularity": "MONTHLY",
                "Metrics": ["UnblendedCost"],
                # The same scope as the total this breaks down, region included.
                "Filter": self._scope((SERVICE_NAME,), platform=self.platform),
                "GroupBy": [{"Type": "TAG", "Key": AGENT_TAG_KEY}],
            }
            try:
                response = client.get_cost_and_usage(**params)
            except Exception as exc:
                raise BillingUnavailable(str(exc)) from exc

            by_agent: Dict[str, float] = {}
            untagged = 0.0
            for period in response.get("ResultsByTime", []):
                for entry in period.get("Groups") or []:
                    raw = (entry.get("Keys") or [""])[0]
                    # `"AgentName$writer"` -> `"writer"`; `"AgentName$"` -> untagged.
                    _, _, name = raw.partition("$")
                    cost = float(
                        (entry.get("Metrics") or {})
                        .get("UnblendedCost", {})
                        .get("Amount", 0)
                        or 0
                    )
                    if name:
                        by_agent[name] = by_agent.get(name, 0.0) + cost
                    else:
                        untagged += cost

            value: Optional[Dict[str, Any]] = None
            if by_agent:
                value = {
                    "by_agent": {
                        name: round(cost, 6) for name, cost in by_agent.items()
                    },
                    "untagged": round(untagged, 6),
                }
            self._cache[key] = (time.monotonic(), value)
            return value

    def model_costs_by_agent(self, start_date: str, end_date: str) -> Optional[Dict[str, float]]:
        """Billed Bedrock model spend per agent, from the AgentName tag.

        Populated only when harnesses route through per-agent application
        inference profiles (spec G) and the tag has accrued dollars — else the
        untagged bucket holds it all and this returns None, and the caller keeps
        the token×rate estimate. Not region- or Platform-filtered: the AgentName
        tag is ours by construction, and a model may bill cross-region.
        """
        client = self.client
        key = ("model_by_agent", start_date, end_date)
        with self._lock:
            cached = self._cache.get(key)
            if cached and time.monotonic() - cached[0] < self._cache_seconds:
                return cached[1]
            params = {
                "TimePeriod": {"Start": start_date, "End": _next_day(end_date)},
                "Granularity": "MONTHLY",
                "Metrics": ["UnblendedCost"],
                "Filter": self._scope(MODEL_SERVICE_NAMES, region=False),
                "GroupBy": [{"Type": "TAG", "Key": AGENT_TAG_KEY}],
            }
            try:
                response = client.get_cost_and_usage(**params)
            except Exception as exc:
                raise BillingUnavailable(str(exc)) from exc
            by_agent = {}
            for period in response.get("ResultsByTime", []):
                for entry in period.get("Groups") or []:
                    _, _, name = (entry.get("Keys") or [""])[0].partition("$")
                    if not name:
                        continue
                    cost = float((entry.get("Metrics") or {}).get("UnblendedCost", {}).get("Amount", 0) or 0)
                    by_agent[name] = by_agent.get(name, 0.0) + cost
            value = {n: round(c, 6) for n, c in by_agent.items()} or None
            self._cache[key] = (time.monotonic(), value)
            return value

    def model_rates(self, start_date: str, end_date: str) -> Dict[str, Any]:
        """Per-1K-token rates for every model the account was billed for.

        Keyed by `(model_key, tier)` so a caller holding a Bedrock model id and a
        token count can price it. The rate is **volume-weighted across routing
        profiles**: one model and tier is billed at several rates depending on how
        the request was routed — measured, opus-5 output is $25.00/1M
        `global-standard` and $27.50/1M `standard` — and the token count we hold
        does not say which. Summing dollars and dividing by summed tokens gives
        the rate the account actually paid, which is the only one that can be
        cited. `spread` carries how far apart the routings were, so a caller can
        state the uncertainty rather than implying there is none.

        Rates come from the whole account's volume, including other tenants'. That
        is the point: a **rate** is not tenant-specific, and pairing it with our own
        per-agent token counts is what makes the product attributable — which the
        account total on the billed card never was.

        Never raises for "no model spend": an account that ran no models returns an
        empty table, and the caller renders "추정 불가" rather than $0.
        """
        client = self.client
        key = ("model_rates", start_date, end_date)

        with self._lock:
            cached = self._cache.get(key)
            if cached and time.monotonic() - cached[0] < self._cache_seconds:
                return cached[1]

            params: Dict[str, Any] = {
                "TimePeriod": {"Start": start_date, "End": _next_day(end_date)},
                "Granularity": "MONTHLY",
                "Metrics": ["UnblendedCost", "UsageQuantity"],
                # Not region-filtered, because a model billed only in another region
                # must still get a rate — see the scope preference below. Usage
                # charges only, so a credit cannot masquerade as a cheaper tariff.
                "Filter": self._scope(MODEL_SERVICE_NAMES, region=False),
                "GroupBy": [
                    {"Type": "DIMENSION", "Key": "USAGE_TYPE"},
                    {"Type": "DIMENSION", "Key": "REGION"},
                ],
            }

            # (model_key, tier) -> [cost, quantity, min rate, max rate], once for the
            # whole account and once for our own billing region.
            folded: Dict[Tuple[frozenset, str], List[float]] = {}
            regional: Dict[Tuple[frozenset, str], List[float]] = {}
            names: Dict[frozenset, str] = {}
            while True:
                try:
                    response = client.get_cost_and_usage(**params)
                except Exception as exc:
                    raise BillingUnavailable(str(exc)) from exc
                for period in response.get("ResultsByTime", []):
                    for entry in period.get("Groups") or []:
                        keys = entry.get("Keys") or [""]
                        usage_type = keys[0]
                        # The second group key when REGION was asked for. Absent only
                        # in tests written before the region split.
                        region = keys[1] if len(keys) > 1 else None
                        tier = _tier_of(usage_type)
                        if tier is None:
                            continue
                        metrics = entry.get("Metrics") or {}
                        quantity_block = metrics.get("UsageQuantity", {})
                        if str(quantity_block.get("Unit", "")).lower() != _TOKEN_UNIT:
                            continue
                        quantity = float(quantity_block.get("Amount", 0) or 0)
                        if quantity <= 0:
                            # No denominator, so no rate. A zero-quantity line is
                            # a rounding artefact, not a free model.
                            continue
                        cost = float(
                            metrics.get("UnblendedCost", {}).get("Amount", 0) or 0
                        )
                        identity = model_key(_model_part(usage_type))
                        if not identity:
                            continue
                        # Shortest wins rather than first-seen: several usagetypes
                        # reduce to one model and the shortest is the one carrying
                        # the fewest routing words.
                        pretty = display_name(usage_type)
                        if len(pretty) < len(names.get(identity, pretty + "x")):
                            names[identity] = pretty
                        rate = cost / quantity
                        for target in (
                            folded,
                            regional if region == self.region_name else None,
                        ):
                            if target is None:
                                continue
                            bucket = target.setdefault(
                                (identity, tier), [0.0, 0.0, rate, rate]
                            )
                            bucket[0] += cost
                            bucket[1] += quantity
                            bucket[2] = min(bucket[2], rate)
                            bucket[3] = max(bucket[3], rate)
                token = response.get("NextPageToken")
                if not token:
                    break
                params["NextPageToken"] = token

            # **Our own billing region wins where it was billed at all.** A tariff is
            # per region — Seoul's tokens cost more than Virginia's — and the
            # reduction to a model key deliberately erases the region prefix, so
            # folding every region into one bucket prices our tokens at a rate the
            # account never charged for them. Falling back to the account-wide blend
            # rather than refusing, because a cross-region inference profile bills
            # where it ran: refusing would put "추정 불가" on exactly the busiest
            # agents. `scope` is what lets the caption say which happened.
            rates: Dict[Tuple[frozenset, str], Dict[str, Any]] = {}
            for rate_key in folded:
                scoped = regional.get(rate_key)
                cost, quantity, low, high = scoped or folded[rate_key]
                rates[rate_key] = {
                    "usd_per_1k": cost / quantity,
                    "spread": round(high - low, 10),
                    "billed_1k_tokens": round(quantity, 4),
                    "scope": "region" if scoped else "account",
                }

            value = {
                "rates": rates,
                "names": names,
                "start_date": start_date,
                "end_date": end_date,
                "region": self.region_name,
            }
            self._cache[key] = (time.monotonic(), value)
            return value

    # --- reconciliation ------------------------------------------------------

    def _routing_of(self, usage_type: str) -> str:
        """Partner lines say `global-standard`; first-party lines say `-global`.
        Anything else is the invoking region's `standard` tariff."""
        return "global" if "global" in usage_type.lower() else "regional"

    @staticmethod
    def _family_of_usage_type(usage_type: str) -> str:
        """The rate-card family a billed line belongs to, by token identity.

        Both naming schemes appear on one bill — `anthropic.claude-haiku-4-5-mantle-…`
        and `Claude4.5Haiku-…` — and `model_key` already reduces both to one set of
        tokens. A line whose tokens match no family keeps its display name so the
        page can list what the bill carries that the card does not.
        """
        from data.model_rates import MODEL_RATES, family_of

        pretty = display_name(usage_type)
        # Partner scheme first, exactly: `claude-opus-5-5` must not become
        # `claude-opus-5` — `model_key` is a *set* of tokens and collapses the
        # repeated 5, so it is only safe for the first-party scheme, whose names
        # (`Claude4.5Haiku`) carry no hyphenated version to be exact about.
        exact = family_of(pretty)
        if exact:
            return exact
        if "-" not in pretty:
            identity = model_key(pretty)
            for family in MODEL_RATES:
                if model_key(family) == identity:
                    return family
            # An unregistered first-party name, written the way a model id spells
            # it (`Claude4.6Sonnet` -> `claude-sonnet-4-6`), so a rate learned
            # from this line can price the id we invoke with.
            spelled = _FIRST_PARTY_NAME.match(pretty)
            if spelled:
                major, minor, name = spelled.groups()
                return f"claude-{name.lower()}-{major}" + (f"-{minor}" if minor else "")
        return pretty

    def reconcile(
        self,
        start_date: str,
        end_date: str,
        *,
        repository: Any,
        ledger_agent_days: Dict[Tuple[str, str], Dict[str, int]],
        ledger_components: Dict[str, int],
    ) -> Dict[str, Any]:
        """Snapshot the bill against the ledger into `RECON#` items.

        Three comparisons, each written as its own item so the page can read them
        without touching Cost Explorer:

        * **agent × day runtime** — CE grouped by the `AgentName` tag and usage
          type (DAILY) against the collector's `RESOURCES#` day items, keyed by
          registry agent name. `diff_micros` is billed minus ours; the untagged
          bucket is reported separately, never attributed.
        * **component** — `agentcore_costs` per component against our summed
          component costs for the window.
        * **model rate** — the rate the account was actually charged per
          (family, routing, tier) in *our* region against the repository rate
          card. A mismatch is a finding: the card is code and is fixed in code.
          One-hour cache writes are a different tier and are skipped; other
          regions' lines are not our tariff and are skipped.

        Never cached: this is called by the collector on its own six-hour cadence.
        Three Cost Explorer reads at $0.01 each.
        """
        client = self.client
        checked_at = datetime.utcnow().isoformat() + "Z"
        micro = Decimal(1_000_000)

        def to_micro(value: Any) -> int:
            return int((Decimal(str(value)) * micro).quantize(Decimal(1), rounding=ROUND_HALF_UP))

        def write(day: str, kind: str, key: str, fields: Dict[str, Any]) -> None:
            shard = day[:7]
            try:
                repository.set_fields(f"RECON#{shard}", f"D#{day}#K#{kind}#{key}", fields)
            except Exception:
                logger.warning("RECON write failed: %s/%s", kind, key, exc_info=True)

        # 1. agent × day
        params: Dict[str, Any] = {
            "TimePeriod": {"Start": start_date, "End": _next_day(end_date)},
            "Granularity": "DAILY",
            "Metrics": ["UnblendedCost", "UsageQuantity"],
            "Filter": self._scope((SERVICE_NAME,), platform=self.platform),
            "GroupBy": [
                {"Type": "TAG", "Key": AGENT_TAG_KEY},
                {"Type": "DIMENSION", "Key": "USAGE_TYPE"},
            ],
        }
        agent_days: Dict[Tuple[str, str], Dict[str, Any]] = {}
        untagged = 0
        while True:
            try:
                response = client.get_cost_and_usage(**params)
            except Exception as exc:
                raise BillingUnavailable(str(exc)) from exc
            for period in response.get("ResultsByTime", []):
                day = (period.get("TimePeriod") or {}).get("Start") or ""
                estimated = bool(period.get("Estimated"))
                for entry in period.get("Groups") or []:
                    keys = entry.get("Keys") or ["", ""]
                    _, _, agent = str(keys[0]).partition("$")
                    usage_type = keys[1] if len(keys) > 1 else ""
                    metrics = entry.get("Metrics") or {}
                    cost = float(metrics.get("UnblendedCost", {}).get("Amount", 0) or 0)
                    quantity_block = metrics.get("UsageQuantity", {})
                    quantity = float(quantity_block.get("Amount", 0) or 0)
                    unit = str(quantity_block.get("Unit", "")).lower()
                    if not agent:
                        untagged += to_micro(cost)
                        continue
                    if component_of(usage_type) != "runtime":
                        continue
                    bucket = agent_days.setdefault(
                        (agent, day),
                        {"billed_micros": 0, "billed_gb_hours_micro": 0,
                         "billed_vcpu_hours_micro": 0, "ce_estimated": estimated},
                    )
                    bucket["billed_micros"] += to_micro(cost)
                    if unit in _GB_UNITS:
                        bucket["billed_gb_hours_micro"] += to_micro(quantity)
                    elif unit in _VCPU_UNITS:
                        bucket["billed_vcpu_hours_micro"] += to_micro(quantity)
                    bucket["ce_estimated"] = bucket["ce_estimated"] or estimated
            token = response.get("NextPageToken")
            if not token:
                break
            params["NextPageToken"] = token

        agent_rows: List[Dict[str, Any]] = []
        for (agent, day), billed in sorted(agent_days.items()):
            ours = ledger_agent_days.get((agent, day)) or {}
            row = {
                "agent": agent,
                "day": day,
                **billed,
                "ours_micros": ours.get("runtime_cost_micros"),
                "ours_gb_hours_micro": ours.get("gb_hours_micro"),
                "ours_vcpu_hours_micro": ours.get("vcpu_hours_micro"),
                "checked_at": checked_at,
            }
            if row["ours_micros"] is not None:
                row["diff_micros"] = row["billed_micros"] - int(row["ours_micros"])
            write(day, "agent_runtime", agent, {k: v for k, v in row.items() if v is not None and k not in ("agent", "day")})
            agent_rows.append(row)

        # 2. components
        component_rows: List[Dict[str, Any]] = []
        try:
            costs = self.agentcore_costs(start_date, end_date)
        except BillingUnavailable:
            costs = None
        if costs:
            for component, bucket in sorted(costs["by_component"].items()):
                billed_micros = to_micro(bucket["cost"])
                ours_micros = ledger_components.get(component)
                row = {
                    "component": component,
                    "billed_micros": billed_micros,
                    "ours_micros": ours_micros,
                    "ce_estimated": bool(costs.get("estimated")),
                    "checked_at": checked_at,
                }
                if ours_micros is not None:
                    row["diff_micros"] = billed_micros - int(ours_micros)
                write(end_date, "component", component, {k: v for k, v in row.items() if v is not None and k != "component"})
                component_rows.append(row)

        # 3. model rates, our region only — one row per (family, routing, tier, day).
        #
        # DAILY, not MONTHLY: cost ÷ tokens over a month is one number for a
        # tariff that may have changed inside it, and that blend would then be
        # the figure the whole window was judged by. Per day, every rate is the
        # unit price in force that day (a change at a day boundary never leaks),
        # and the learning rule below can ask for the same value on two
        # consecutive days before trusting it.
        from data.model_rates import effective_rate

        params = {
            "TimePeriod": {"Start": start_date, "End": _next_day(end_date)},
            "Granularity": "DAILY",
            "Metrics": ["UnblendedCost", "UsageQuantity"],
            "Filter": self._scope(MODEL_SERVICE_NAMES, region=False),
            "GroupBy": [
                {"Type": "DIMENSION", "Key": "USAGE_TYPE"},
                {"Type": "DIMENSION", "Key": "REGION"},
            ],
        }
        daily: Dict[Tuple[str, str, str, str], List[float]] = {}
        while True:
            try:
                response = client.get_cost_and_usage(**params)
            except Exception as exc:
                raise BillingUnavailable(str(exc)) from exc
            for period in response.get("ResultsByTime", []):
                day = str((period.get("TimePeriod") or {}).get("Start") or end_date)
                for entry in period.get("Groups") or []:
                    keys = entry.get("Keys") or [""]
                    usage_type = str(keys[0])
                    region = keys[1] if len(keys) > 1 else None
                    if region != self.region_name:
                        continue
                    if "-1h-" in usage_type.lower():
                        continue
                    tier = _tier_of(usage_type)
                    if tier is None:
                        continue
                    metrics = entry.get("Metrics") or {}
                    quantity_block = metrics.get("UsageQuantity", {})
                    if str(quantity_block.get("Unit", "")).lower() != _TOKEN_UNIT:
                        continue
                    quantity = float(quantity_block.get("Amount", 0) or 0)
                    if quantity <= 0:
                        continue
                    cost = float(metrics.get("UnblendedCost", {}).get("Amount", 0) or 0)
                    family = self._family_of_usage_type(usage_type)
                    routing = self._routing_of(usage_type)
                    bucket = daily.setdefault((family, routing, tier, day), [0.0, 0.0])
                    bucket[0] += cost
                    bucket[1] += quantity
            token = response.get("NextPageToken")
            if not token:
                break
            params["NextPageToken"] = token

        rate_rows: List[Dict[str, Any]] = []
        clean_days: Dict[Tuple[str, str, str], Dict[str, Decimal]] = {}
        for (family, routing, tier, day), (cost, quantity) in sorted(daily.items(), key=lambda kv: (kv[0][3], kv[0][:3])):
            # CE quantity is in thousands of tokens: USD per 1M = cost / quantity × 1000.
            billed_per_1m = Decimal(str(cost)) / Decimal(str(quantity)) * Decimal(1000)
            clean_value = billed_per_1m.quantize(_RATE_QUANTUM, rounding=ROUND_HALF_UP)
            is_clean = abs(billed_per_1m - clean_value) <= _RATE_CLEAN_TOLERANCE and quantity >= _RATE_MIN_1K_TOKENS
            in_force = effective_rate(family, routing, day)
            card_rate = (in_force or {}).get("usd_per_1m", {}).get(tier) if in_force else None
            row: Dict[str, Any] = {
                "family": family,
                "routing": routing,
                "tier": tier,
                "day": day,
                "region": self.region_name,
                "billed_usd_per_1m_micro": to_micro(billed_per_1m),
                "billed_1k_tokens": round(quantity, 4),
                "clean": is_clean,
                "registered": card_rate is not None,
                "checked_at": checked_at,
            }
            if card_rate is not None:
                row["card_usd_per_1m_micro"] = to_micro(card_rate)
                row["rate_version"] = in_force["version"]
                # Exact to the micro-dollar per million tokens, with one micro of
                # slack for CE's own rounding of the dollar amount.
                row["match"] = abs(row["card_usd_per_1m_micro"] - row["billed_usd_per_1m_micro"]) <= 1
            write(day, "model_rate", f"{family}|{routing}|{tier}", {k: v for k, v in row.items() if k not in ("family", "routing", "tier", "day")})
            rate_rows.append(row)
            if is_clean:
                clean_days.setdefault((family, routing, tier), {})[day] = clean_value

        learned = self._learn_rates(clean_days, complete_before=end_date, repository=repository, checked_at=checked_at)

        # Findings are judged on the latest day each (family, routing, tier) was
        # billed: an older day's mismatch that a learned entry has since explained
        # is history, not a finding.
        latest_rows: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        for row in rate_rows:
            latest_rows[(row["family"], row["routing"], row["tier"])] = row
        mismatches = [row for row in latest_rows.values() if row.get("registered") and row.get("match") is False]
        unregistered = sorted({row["family"] for row in latest_rows.values() if not row["registered"]})
        summary = {
            "checked_at": checked_at,
            "window": {"start_date": start_date, "end_date": end_date},
            "agent_runtime": agent_rows,
            "untagged_micros": untagged,
            "component": component_rows,
            "model_rate": rate_rows,
            "mismatches": mismatches,
            "unregistered_families": unregistered,
            "learned": learned,
        }
        write(end_date, "summary", "latest", {
            "checked_at": checked_at,
            "start_date": start_date,
            "end_date": end_date,
            "mismatch_count": len(mismatches),
            "unregistered_families": unregistered,
            "untagged_micros": untagged,
            "learned_count": len(learned),
        })
        return summary

    @staticmethod
    def _learn_rates(
        clean_days: Dict[Tuple[str, str, str], Dict[str, Decimal]],
        *,
        complete_before: str,
        repository: Any,
        checked_at: str,
    ) -> List[Dict[str, Any]]:
        """Turn the bill's daily rates into dated `RATES#learned` entries — under
        one rule, never by averaging.

        For each (family, routing, tier): walk the complete days (strictly before
        `complete_before`, the day still being billed) in order and find runs of
        **consecutive observations billed at the same exact value**. A run of two
        or more days whose value differs from the rate in force on its first day
        is a rate the card is missing — a new model, or a price change — and is
        written with that first day as `effective_from`. One day is not enough:
        a single clean day could be the day of a change (its own rate is exact,
        but whether it holds is unknown), so it waits for the next. Days whose
        rate did not divide cleanly never enter a run.

        Consecutive *observations*, not calendar days: a model used every other
        day is billed every other day, and a price cannot change between two
        observations without the second one showing a different value — which
        ends the run. A day nobody used the model is no evidence either way.

        Idempotent: the sort key is the (family, routing, tier, effective_from)
        identity, and an entry already stored is rewritten with the same fields.
        A value learned earlier in this pass counts as in force for the days after
        it, so a gap in usage does not re-learn the same rate under a later date.
        Returns the entries written or confirmed on this pass.
        """
        from data.model_rates import effective_rate

        learned: List[Dict[str, Any]] = []
        for (family, routing, tier), by_day in sorted(clean_days.items()):
            days = sorted(day for day in by_day if day < complete_before)
            pending: Optional[Tuple[str, Decimal]] = None  # (effective_from, value) learned this pass
            index = 0
            while index < len(days):
                start = days[index]
                value = by_day[start]
                run_end = index
                while run_end + 1 < len(days) and by_day[days[run_end + 1]] == value:
                    run_end += 1
                length = run_end - index + 1
                if pending is not None and pending[0] <= start:
                    current: Optional[Decimal] = pending[1]
                else:
                    in_force = effective_rate(family, routing, start)
                    current = (in_force or {}).get("usd_per_1m", {}).get(tier) if in_force else None
                if length >= 2 and current != value:
                    pending = (start, value)
                    entry = {
                        "family": family,
                        "routing": routing,
                        "tier": tier,
                        "effective_from": start,
                        "usd_per_1m": str(value),
                        "days_observed": length,
                        "through": days[run_end],
                        "source": f"Cost Explorer daily lines {start}..{days[run_end]}",
                        "learned_at": checked_at,
                    }
                    try:
                        repository.set_fields(
                            "RATES#learned", f"R#{family}|{routing}|{tier}|{start}", entry
                        )
                    except Exception:
                        logger.warning("Learned rate write failed: %s/%s/%s", family, routing, tier, exc_info=True)
                    learned.append(entry)
                index = run_end + 1
        return learned

    @staticmethod
    def _fold(results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Fold `ResultsByTime` into a total, a component split and a daily series.

        **The daily series is keyed by date, not appended per period.** Cost Explorer
        paginates *groups*, so one `TimePeriod` legitimately arrives on two pages —
        and appending a row per occurrence drew that date as two columns, each
        holding a fraction of the day. The total was right either way, which is what
        kept it invisible.
        """
        total = 0.0
        estimated = False
        by_component: Dict[str, Dict[str, float]] = {}
        by_date: Dict[str, float] = {}

        for period in results:
            estimated = estimated or bool(period.get("Estimated"))
            day_cost = 0.0
            for entry in period.get("Groups") or []:
                usage_type = (entry.get("Keys") or [""])[0]
                metrics = entry.get("Metrics") or {}
                cost = float(metrics.get("UnblendedCost", {}).get("Amount", 0) or 0)
                quantity_block = metrics.get("UsageQuantity", {})
                quantity = float(quantity_block.get("Amount", 0) or 0)
                unit = str(quantity_block.get("Unit", "")).lower()

                bucket = by_component.setdefault(
                    component_of(usage_type),
                    {"cost": 0.0, "vcpu_hours": 0.0, "gb_hours": 0.0},
                )
                bucket["cost"] += cost
                if unit in _VCPU_UNITS:
                    bucket["vcpu_hours"] += quantity
                elif unit in _GB_UNITS:
                    bucket["gb_hours"] += quantity

                total += cost
                day_cost += cost

            date = (period.get("TimePeriod") or {}).get("Start")
            by_date[date] = by_date.get(date, 0.0) + day_cost

        return {
            "total": round(total, 6),
            "by_component": {
                name: {field: round(value, 6) for field, value in bucket.items()}
                for name, bucket in by_component.items()
            },
            "daily": [
                {"date": date, "cost": round(cost, 6)}
                for date, cost in sorted(by_date.items(), key=lambda pair: pair[0] or "")
            ],
            "estimated": estimated,
        }
