"""The model rate card as something an admin can read, fetch into and fill.

**Why this exists.** The ledger prices a turn only with a rate it can stand
behind, so a model the committed card lacks is "요율 미등록" until the bill has
shown the same value on two consecutive days (`billing_service._learn_rates`).
That rule is right and it is slow: a new model's turns sit unpriced for two
billing days, and a model whose global routing we alone use may never reach two
observations. The admin can see the figure on the AWS pricing page today. This
service lets them put it in — and, wherever a source can be read, reads it for
them so the number is typed by a machine rather than a person.

**Three sources, one overlay, one precedence.** Every rate an admin registers
lands in the same `RATES#learned` partition the bill's learner writes to, under
the same `(family, routing, tier, effective_from)` key, with `source` saying
which of the three it was:

* `bill` — the bill's own daily rate, offered as a candidate the moment a
  single clean day exists (the learner would wait for a second);
* `price-list` — a row the Price List API publishes for the region;
* `admin:<email>` — typed in.

The bill stays the authority: if it later shows two consecutive days at a
different value, the learner writes over the same key and the page shows the
correction. Nothing here derives one routing from another (see
`data/model_rates`: the global/regional factor is an observation, not a law).

**Repricing is immediate.** Saving or removing an entry reprices every measured
turn from its effective date to today (`UsageService.reprice_events`), so the
figure on the dashboard changes when the admin presses the button, not at the
next six-hourly reconciliation.
"""
import logging
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from data.model_rates import (
    RATE_CARD_VERSION,
    TIERS,
    effective_rate,
    family_name_of,
    family_of,
    routing_of,
)

logger = logging.getLogger(__name__)

ROUTINGS = ("global", "regional")
_FAMILY = re.compile(r"^claude-[a-z]+-\d{1,2}(?:-\d{1,2})?$")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# A per-million figure above this is a typo, not a tariff (Opus 4 output is $75).
_MAX_USD_PER_1M = Decimal("1000")
_MICRO = Decimal(1_000_000)


def plain(value: Decimal) -> str:
    """`Decimal("4.00")` -> `"4"`, `Decimal("20")` -> `"20"` — never `"2E+1"`,
    which is what `normalize()` alone prints and what an admin would read as a
    typo in their own entry."""
    text = format(value.normalize(), "f")
    return text if text != "-0" else "0"


class RateCardError(ValueError):
    """A request the admin can fix: the message is shown as-is."""


def rate_key(family: str, routing: str, tier: str, effective_from: str) -> str:
    """The overlay sort key, shared with `billing_service._learn_rates`."""
    return f"R#{family}|{routing}|{tier}|{effective_from}"


def parse_rate_key(sort_key: str) -> Tuple[str, str, str, str]:
    """`R#family|routing|tier|day` -> its four parts, or `RateCardError`."""
    if not sort_key.startswith("R#"):
        raise RateCardError("잘못된 요율 키입니다.")
    parts = sort_key[2:].split("|")
    if len(parts) != 4:
        raise RateCardError("잘못된 요율 키입니다.")
    return parts[0], parts[1], parts[2], parts[3]


def validate_entry(raw: Dict[str, Any], *, today: str) -> Dict[str, Any]:
    """One admin-submitted rate, normalised, or `RateCardError` naming the field.

    The family must be spelled the way a model id spells it (`claude-opus-5-5`),
    because that is what `family_of` matches turns against; a bill-style
    `Claude5.5Opus` would be stored and never used.
    """
    family = str(raw.get("family") or "").strip().lower()
    if not _FAMILY.match(family):
        raise RateCardError("모델 family 는 claude-opus-5-5 처럼 모델 ID 표기여야 합니다.")
    routing = str(raw.get("routing") or "").strip().lower()
    if routing not in ROUTINGS:
        raise RateCardError("라우팅은 global 또는 regional 이어야 합니다.")
    tier = str(raw.get("tier") or "").strip().lower()
    if tier not in TIERS:
        raise RateCardError(f"tier 는 {', '.join(TIERS)} 중 하나여야 합니다.")
    try:
        usd = Decimal(str(raw.get("usd_per_1m", "")).strip())
    except (InvalidOperation, ValueError):
        raise RateCardError("요율은 1M 토큰당 달러 숫자여야 합니다.") from None
    if not usd.is_finite() or usd < 0 or usd > _MAX_USD_PER_1M:
        raise RateCardError("요율은 0 이상 1,000 이하의 1M 토큰당 달러여야 합니다.")
    effective_from = str(raw.get("effective_from") or "").strip()
    if not _DAY.match(effective_from):
        raise RateCardError("유효 시작일은 YYYY-MM-DD 형식이어야 합니다.")
    try:
        datetime.strptime(effective_from, "%Y-%m-%d")
    except ValueError:
        raise RateCardError("유효 시작일이 실제 날짜가 아닙니다.") from None
    if effective_from > today:
        raise RateCardError("유효 시작일은 오늘 이후일 수 없습니다.")
    return {
        "family": family,
        "routing": routing,
        "tier": tier,
        "usd_per_1m": plain(usd),
        "effective_from": effective_from,
    }


class RateCardService:
    """Reads the card with provenance, offers candidates, stores what the admin
    accepts, and reprices. Holds no state of its own: the overlay lives in the
    usage table and in `data.model_rates`."""

    def __init__(self, usage: Any, pricing: Any):
        self.usage = usage
        self.pricing = pricing

    # --- reading -----------------------------------------------------------------

    def overview(self, start_date: str, end_date: str) -> Dict[str, Any]:
        """The card for the models this platform ran in the window.

        One row per (family, routing) we invoked, each tier with its figure and
        where the figure came from; the bill's candidates for anything missing or
        different; and every stored overlay entry for those families, so an admin
        can see — and remove — what was registered and by whom.
        """
        # A fresh read, not the five-minute `ensure_`: this page is where an admin
        # has just written, and it must show what another replica wrote too.
        self.usage.load_learned_rates()
        by_model = self.usage.model_days(start_date, end_date)

        rows: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for model_id, days in by_model.items():
            family = family_of(model_id) or family_name_of(model_id)
            if family is None:
                continue
            routing = routing_of(model_id)
            row = rows.setdefault((family, routing), {
                "family": family,
                "routing": routing,
                "models": [],
                "turns": 0,
                "unpriced_turns": 0,
                "unpriced_since": None,
            })
            row["models"].append(model_id)
            for day, counters in days.items():
                row["turns"] += counters.get("turns", 0)
                unpriced = counters.get("unpriced_turns", 0)
                if unpriced > 0:
                    row["unpriced_turns"] += unpriced
                    if row["unpriced_since"] is None or day < row["unpriced_since"]:
                        row["unpriced_since"] = day

        # Entries for the families we ran, plus anything a person (or the Price
        # List) registered for a family we have not run yet — an admin who
        # registers a model ahead of its first turn must be able to see and undo
        # it. The bill's own learner writes for every tenant on the account, and
        # those stay out unless the family is ours.
        families = {family for family, _ in rows}
        entries = [
            entry for entry in self.usage.rate_entries()
            if entry.get("family") in families or not str(entry.get("source") or "").startswith("Cost Explorer")
        ]
        for entry in entries:
            key = (str(entry["family"]), str(entry["routing"]))
            if key not in rows:
                rows[key] = {
                    "family": key[0], "routing": key[1], "models": [],
                    "turns": 0, "unpriced_turns": 0, "unpriced_since": None,
                }

        for row in rows.values():
            in_force = effective_rate(row["family"], row["routing"], end_date)
            row["models"] = sorted(row["models"])
            row["tiers"] = {
                tier: (
                    {
                        "usd_per_1m": str(in_force["usd_per_1m"][tier]),
                        "source": in_force["provenance"].get(tier, {}).get("source", ""),
                        "effective_from": in_force["provenance"].get(tier, {}).get("effective_from"),
                    }
                    if in_force and tier in in_force["usd_per_1m"]
                    else None
                )
                for tier in TIERS
            }
            row["complete"] = all(row["tiers"][tier] is not None for tier in TIERS)

        candidates = self._bill_candidates(start_date, end_date, {key for key in rows})
        return {
            "version": RATE_CARD_VERSION,
            "window": {"start_date": start_date, "end_date": end_date},
            "rows": sorted(rows.values(), key=lambda row: (row["family"], row["routing"])),
            "candidates": candidates,
            "entries": sorted(
                (self._public_entry(entry) for entry in entries),
                key=lambda entry: (entry["family"], entry["routing"], entry["tier"], entry["effective_from"]),
            ),
        }

    @staticmethod
    def _public_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "key": str(entry.get("sk") or rate_key(
                str(entry["family"]), str(entry["routing"]), str(entry["tier"]), str(entry["effective_from"])
            )),
            "family": str(entry["family"]),
            "routing": str(entry["routing"]),
            "tier": str(entry["tier"]),
            "effective_from": str(entry["effective_from"]),
            "usd_per_1m": str(entry["usd_per_1m"]),
            "source": str(entry.get("source") or ""),
            "registered_at": entry.get("registered_at") or entry.get("learned_at"),
        }

    def _bill_candidates(
        self, start_date: str, end_date: str, pairs: set
    ) -> List[Dict[str, Any]]:
        """What the bill has shown for our (family, routing) pairs that the card
        does not say — on as little as one clean day.

        Reads the reconciliation's daily `model_rate` rows. For each tier the
        latest billed day is judged against the rate in force that day; when they
        differ (or none is in force) the trailing run of days billed at that same
        value becomes the candidate, its first day the suggested effective date.
        The learner needs two such days; the admin may accept one, and the page
        says how many there were.
        """
        from services.usage_service import month_shards

        repository = getattr(self.usage, "repository", None)
        if repository is None or not hasattr(repository, "query_prefix"):
            return []
        by_key: Dict[Tuple[str, str, str], Dict[str, Dict[str, Any]]] = {}
        for shard in month_shards(start_date, end_date):
            try:
                items = repository.query_prefix(f"RECON#{shard}", "D#")
            except Exception:
                logger.warning("RECON read failed for %s", shard, exc_info=True)
                continue
            for item in items:
                sort_key = str(item.get("sk", ""))
                head, sep, rest = sort_key.partition("#K#model_rate#")
                if not sep or not head.startswith("D#"):
                    continue
                day = head[2:]
                if not (start_date <= day <= end_date):
                    continue
                family, _, tail = rest.partition("|")
                routing, _, tier = tail.partition("|")
                if (family, routing) not in pairs or tier not in TIERS:
                    continue
                by_key.setdefault((family, routing, tier), {})[day] = item

        candidates: List[Dict[str, Any]] = []
        for (family, routing, tier), days in sorted(by_key.items()):
            ordered = sorted(days)
            latest = ordered[-1]
            row = days[latest]
            if not row.get("clean"):
                continue
            billed_micro = row.get("billed_usd_per_1m_micro")
            if billed_micro is None:
                continue
            value = Decimal(int(billed_micro)) / _MICRO
            in_force = effective_rate(family, routing, latest)
            current = (in_force or {}).get("usd_per_1m", {}).get(tier) if in_force else None
            if current is not None and current == value:
                continue
            first = latest
            observed = 0
            for day in reversed(ordered):
                item = days[day]
                same = item.get("clean") and item.get("billed_usd_per_1m_micro") is not None and int(
                    item["billed_usd_per_1m_micro"]
                ) == int(billed_micro)
                if not same:
                    break
                first = day
                observed += 1
            candidates.append({
                "family": family,
                "routing": routing,
                "tier": tier,
                "usd_per_1m": plain(value),
                "source": "bill",
                "effective_from": first,
                "last_day": latest,
                "days_observed": observed,
                "current_usd_per_1m": plain(current) if current is not None else None,
            })
        return candidates

    def published(self, families: Optional[List[str]] = None) -> Dict[str, Any]:
        """Price List rows for our families that the card lacks or differs from.

        A separate call, made on a button, because it is a live API round-trip;
        the overview never waits on it. Never a candidate for a figure the card
        already has at the same value.
        """
        rows = self.pricing.bedrock_model_rates()
        wanted = set(families or [])
        out: List[Dict[str, Any]] = []
        for row in rows:
            if wanted and row["family"] not in wanted:
                continue
            in_force = effective_rate(row["family"], row["routing"])
            current = (in_force or {}).get("usd_per_1m", {}).get(row["tier"]) if in_force else None
            value = Decimal(row["usd_per_1m"])
            if current is not None and current == value:
                continue
            out.append({
                **row,
                "source": "price-list",
                "current_usd_per_1m": plain(current) if current is not None else None,
            })
        return {"published": len(rows), "candidates": out}

    # --- writing -----------------------------------------------------------------

    def register(
        self, raw_entries: List[Dict[str, Any]], *, by: str, today: str
    ) -> Dict[str, Any]:
        """Validate, store, reload and reprice. All-or-nothing on validation: a
        batch with one bad row writes nothing, so the admin fixes it and resends."""
        if not raw_entries:
            raise RateCardError("등록할 요율이 없습니다.")
        entries = [validate_entry(raw, today=today) for raw in raw_entries]
        registered_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        saved: List[str] = []
        for raw, entry in zip(raw_entries, entries):
            source = str(raw.get("source") or "").strip()
            if source not in ("bill", "price-list"):
                source = f"admin:{by}"
            record = {
                **entry,
                "source": source,
                "registered_by": by,
                "registered_at": registered_at,
            }
            saved.append(self.usage.put_rate_entry(record))
        since = min(entry["effective_from"] for entry in entries)
        repriced = self.usage.reprice_events(since, today)
        return {"saved": saved, "repriced": repriced}

    def remove(self, sort_key: str, *, today: str) -> Dict[str, Any]:
        """Delete one entry and reprice from its effective date — the turns it
        priced go back to unpriced unless another rate covers them."""
        _, _, _, effective_from = parse_rate_key(sort_key)
        if not self.usage.delete_rate_entry(sort_key):
            raise RateCardError("그 요율 항목이 없습니다.")
        repriced = self.usage.reprice_events(effective_from, today)
        return {"removed": sort_key, "repriced": repriced}
