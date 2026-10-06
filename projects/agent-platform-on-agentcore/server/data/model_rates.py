"""Bedrock Anthropic token tariff, versioned in the repository.

**Why a table and not an API.** The Price List API publishes no rates for
Sonnet 5, Opus 5.x or Opus 4.8, and for Haiku 4.5 only the regional (`standard`)
rows — measured 2026-09-23 against `AmazonBedrock` and `AmazonBedrockService`.
Cost Explorer, on the other hand, shows every one of these models billed at
exact round rates, and the `global-standard` routing is a constant factor of
1.1 below regional `standard` across all of them (Sonnet 5 output $10.00 vs
$11.00, Opus 5 output $25.00 vs $27.50, Haiku 4.5 input $1.00 vs $1.10). So the
rate is a deterministic function of (model family, routing, tier), and the
routing is a function of the model id prefix we invoke with. That function is
this module.

**The bill verifies the table, and extends it under a rule.** The static table
below is the card as committed. `billing_service.reconcile` re-derives every
(family, routing, tier) rate from Cost Explorer **per day** every six hours; a
day's rate is cost ÷ tokens for that one day, so a price change at a day boundary
never blends into a neighbour (measured 2026-09-24: 66 daily lines across six
families, every one an exact 4-decimal value). A rate the table does not have —
a new model, or a changed price — is *learned* only when the same exact value
was billed on two consecutive complete days; it is then stored with the first of
those days as its effective date (`RATES#learned`) and overlaid here by
`set_learned_rates`. Until then a turn on that model is priced at *nothing* — the
caller records it without a cost and the dashboard says "요율 미등록", which is a
true statement where a guess would not be — and the learned rate reprices it.

Every rate therefore has an effective date, and `resolve_rate(model_id, day)`
prices a turn at the rate in force on *its* day, never at today's.

Costs are integer micro-dollars. Tokens × USD-per-million is exact in `Decimal`;
the only rounding is half-up at the micro, so sums over turns never drift.
"""
import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, Iterable, List, Mapping, Optional, TypedDict

RATE_CARD_VERSION = "2026-09-23.1"

TIERS = ("input", "output", "cache_read", "cache_write")
# Counter attribute each tier is stored under in the usage table.
TIER_COUNTER = {tier: f"{tier}_tokens" for tier in TIERS}

_D = Decimal

# family -> routing -> tier -> USD per 1M tokens.
#
# `global` is global-standard. A missing `regional` row is derived as
# global × REGIONAL_MULTIPLIER; a present one (Price List published) wins.
MODEL_RATES: Dict[str, Dict[str, Dict[str, Decimal]]] = {
    "claude-sonnet-5": {
        "global": {
            "input": _D("2.00"), "output": _D("10.00"),
            "cache_read": _D("0.20"), "cache_write": _D("2.50"),
        },
    },
    "claude-opus-5": {
        "global": {
            "input": _D("5.00"), "output": _D("25.00"),
            "cache_read": _D("0.50"), "cache_write": _D("6.25"),
        },
    },
    "claude-opus-4-8": {
        "global": {
            "input": _D("5.00"), "output": _D("25.00"),
            "cache_read": _D("0.50"), "cache_write": _D("6.25"),
        },
    },
    "claude-haiku-4-5": {
        "global": {
            "input": _D("1.00"), "output": _D("5.00"),
            "cache_read": _D("0.10"), "cache_write": _D("1.25"),
        },
        # Price List `AmazonBedrockService`, regionCode us-east-1, read 2026-09-23.
        "regional": {
            "input": _D("1.10"), "output": _D("5.50"),
            "cache_read": _D("0.11"), "cache_write": _D("1.375"),
        },
    },
}

# Where each family's numbers were read, so a reviewer can re-check them.
RATE_SOURCES: Dict[str, str] = {
    "claude-sonnet-5": "Cost Explorer USE1-anthropic.claude-sonnet-5-mantle-* 2026-09-16..23 (exact round rates)",
    "claude-opus-5": "Cost Explorer USE1-anthropic.claude-opus-5-mantle-* 2026-09-16..23",
    "claude-opus-4-8": "Cost Explorer USE1-anthropic.claude-opus-4-8-mantle-* 2026-09-01..23",
    "claude-haiku-4-5": "Price List AmazonBedrockService (regional rows) + CE global-standard rows",
}

REGIONAL_MULTIPLIER = _D("1.1")

_MICRO = _D(1_000_000)
_ONE_MILLION_TOKENS = _D(1_000_000)

class ResolvedRate(TypedDict):
    family: str
    routing: str
    # Only the tiers a rate exists for. The static card always has all four; a
    # learned family has the tiers the bill has shown so far, and `cost_micros`
    # refuses to price a turn that used a tier this lacks.
    usd_per_1m: Dict[str, Decimal]
    version: str
    source: str
    # tier -> where that one figure came from: the static card, or a dated
    # overlay entry (learned from the bill, fetched from the Price List, or
    # entered by an admin). The rate manager shows this beside each number.
    provenance: Dict[str, "TierProvenance"]


class TierProvenance(TypedDict):
    source: str
    effective_from: Optional[str]


class LearnedRate(TypedDict):
    family: str
    routing: str
    tier: str
    effective_from: str
    usd_per_1m: Decimal
    source: str


# The overlay the bill taught us, process-wide. Sorted by effective date so the
# last entry at or before a day is the one in force. Loaded from `RATES#learned`
# by `UsageService.load_learned_rates` and refreshed by the collector.
_LEARNED: List[LearnedRate] = []
# Families the static card lacks but the overlay has, longest first like
# `_FAMILIES`, so a learned `claude-opus-5-5` beats the static `claude-opus-5`.
_LEARNED_FAMILIES: List[str] = []
# The day used when none is given: "now", which every learned entry precedes.
_LATEST_DAY = "9999-12-31"


def set_learned_rates(entries: Iterable[Mapping[str, Any]]) -> None:
    """Replace the learned overlay. Entries need family, routing, tier,
    effective_from and usd_per_1m (any numeric form); anything malformed is
    dropped rather than allowed to price a turn."""
    global _LEARNED, _LEARNED_FAMILIES
    parsed: List[LearnedRate] = []
    for entry in entries or []:
        try:
            family = str(entry["family"])
            routing = str(entry["routing"])
            tier = str(entry["tier"])
            effective_from = str(entry["effective_from"])
            usd = Decimal(str(entry["usd_per_1m"]))
        except (KeyError, TypeError, ValueError, ArithmeticError):
            continue
        if tier not in TIERS or routing not in ("global", "regional") or len(effective_from) != 10 or usd < 0:
            continue
        parsed.append({
            "family": family, "routing": routing, "tier": tier,
            "effective_from": effective_from, "usd_per_1m": usd,
            "source": str(entry.get("source") or "Cost Explorer daily lines"),
        })
    parsed.sort(key=lambda e: (e["effective_from"], e["family"], e["routing"], e["tier"]))
    _LEARNED = parsed
    _LEARNED_FAMILIES = sorted({e["family"] for e in parsed if e["family"] not in MODEL_RATES}, key=len, reverse=True)


def learned_rates() -> List[LearnedRate]:
    """The overlay as loaded, oldest effective date first."""
    return list(_LEARNED)


def known_families() -> List[str]:
    """Every family a rate exists for, longest first so `claude-opus-5-5` cannot
    be claimed by `claude-opus-5`."""
    return sorted(set(MODEL_RATES) | set(_LEARNED_FAMILIES), key=len, reverse=True)


def routing_of(model_id: str) -> str:
    """`global.` bills global-standard; `us.`/`eu.`/`apac.` profiles and bare
    ids bill the invoking region's `standard` tariff."""
    return "global" if model_id.startswith("global.") else "regional"


def _model_body(model_id: str) -> str:
    """`global.anthropic.claude-sonnet-5` -> `claude-sonnet-5`;
    `anthropic.claude-haiku-4-5-20251001-v1:0` -> `claude-haiku-4-5-20251001-v1:0`."""
    body = model_id
    marker = "anthropic."
    index = body.find(marker)
    if index >= 0:
        body = body[index + len(marker):]
    return body


_FAMILY_NAME = re.compile(r"^(claude-[a-z]+-\d{1,2}(?:-\d{1,2})?)(?=$|[-:])")


def family_name_of(model_id: Optional[str]) -> Optional[str]:
    """The family a model id *spells*, whether or not any rate exists for it.

    `family_of` answers "which registered tariff prices this"; this answers
    "what would the tariff be called" — so the rate manager can show an
    unregistered model (`global.anthropic.claude-opus-5-5`) as the family an
    admin registers a rate under (`claude-opus-5-5`). Legacy ids that put the
    version before the name (`claude-3-5-sonnet-…`) have no such spelling."""
    if not model_id:
        return None
    match = _FAMILY_NAME.match(_model_body(model_id))
    return match.group(1) if match else None


def family_of(model_id: str) -> Optional[str]:
    """The rate-card family a model id belongs to, or None.

    A family matches when the body *is* the family, or continues with a
    version/date suffix that is not another version digit: `claude-haiku-4-5`
    followed by `-20251001-v1:0` matches; `claude-opus-5` followed by `-5` does
    not — that is `claude-opus-5-5`, a different model with its own tariff.
    """
    body = _model_body(model_id)
    for family in known_families():
        if body == family:
            return family
        if not body.startswith(family):
            continue
        rest = body[len(family):]
        if rest[:1] == ":":
            return family
        if rest[:1] == "-":
            # A date stamp is 8 digits; a version bump is 1-2. Only the stamp
            # (or a `-v1` style suffix) belongs to this family.
            token = rest[1:].split("-", 1)[0].split(":", 1)[0]
            if token.isdigit() and len(token) >= 8:
                return family
            if token.startswith("v") and token[1:].isdigit():
                return family
    return None


def effective_rate(family: str, routing: str, day: Optional[str] = None) -> Optional[ResolvedRate]:
    """The tariff in force for (family, routing) on `day` (`YYYY-MM-DD`), or None.

    The static card is the base — for a family it knows, all four tiers, with
    the regional row derived as global × 1.1 when the Price List has not
    published one. Learned entries whose effective date is on or before `day`
    are laid over it tier by tier, latest date winning. A learned-only family
    has exactly the tiers the bill has shown; nothing is derived for it.

    `day` defaults to "now" — the latest rate — which is right for a turn being
    recorded and wrong for repricing history, so the ledger always passes one.
    """
    day = day or _LATEST_DAY
    usd: Dict[str, Decimal] = {}
    provenance: Dict[str, TierProvenance] = {}
    version = RATE_CARD_VERSION
    source = RATE_SOURCES.get(family, "")
    table = MODEL_RATES.get(family)
    if table is not None:
        if routing in table:
            usd = dict(table[routing])
        else:
            usd = {tier: table["global"][tier] * REGIONAL_MULTIPLIER for tier in TIERS}
        provenance = {tier: {"source": f"card:{RATE_CARD_VERSION}", "effective_from": None} for tier in usd}
    learned_from: Optional[str] = None
    for entry in _LEARNED:
        if entry["family"] != family or entry["routing"] != routing or entry["effective_from"] > day:
            continue
        # `_LEARNED` is sorted by effective date, so a later entry overrides.
        usd[entry["tier"]] = entry["usd_per_1m"]
        provenance[entry["tier"]] = {"source": entry["source"], "effective_from": entry["effective_from"]}
        learned_from = max(learned_from or "", entry["effective_from"])
        if not source:
            source = entry["source"]
    if not usd:
        return None
    if learned_from:
        version = f"{RATE_CARD_VERSION}+ce:{learned_from}"
    return {
        "family": family,
        "routing": routing,
        "usd_per_1m": usd,
        "version": version,
        "source": source,
        "provenance": provenance,
    }


def resolve_rate(model_id: Optional[str], day: Optional[str] = None) -> Optional[ResolvedRate]:
    """The tariff a model id was billed at on `day`, or None when unregistered.

    `day` is the turn's business date. Left out, it means "now" — see
    `effective_rate` for why the ledger never leaves it out when repricing."""
    if not model_id:
        return None
    family = family_of(model_id)
    if family is None:
        return None
    return effective_rate(family, routing_of(model_id), day)


def cost_micros(counters: Mapping[str, int], rate: ResolvedRate) -> Optional[int]:
    """Integer micro-dollars for the four tiers, rounded half-up at the micro.

    None — not 0, not a partial sum — when the turn used a tier the rate has no
    figure for: a learned family whose cache tiers the bill has not shown yet
    cannot price a turn that read from cache, and a figure missing one tier
    would be a floor presented as a total."""
    usd = _D(0)
    for tier in TIERS:
        tokens = int(counters.get(TIER_COUNTER[tier]) or 0)
        if not tokens:
            continue
        per_1m = rate["usd_per_1m"].get(tier)
        if per_1m is None:
            return None
        usd += _D(tokens) * per_1m / _ONE_MILLION_TOKENS
    return int((usd * _MICRO).quantize(_D(1), rounding=ROUND_HALF_UP))
