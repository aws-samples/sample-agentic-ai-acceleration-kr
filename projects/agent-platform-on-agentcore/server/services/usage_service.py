"""Per-turn usage recording — an event ledger with derived counters.

Every turn is one `TURNS#` item, written once (conditional put) and priced when
it ends from the repository rate card (`data/model_rates`). The day counters the
dashboard reads — per agent, per user, per (agent, user), per (agent, model),
per tool — are ADDed only after that event landed, so the three ledgers cannot
disagree about how many turns there were, and a turn that flushes twice is one
turn. Every failure is swallowed and logged: this runs inside the chat stream,
and losing a turn's answer to protect a counter is the wrong trade — the same
judgement AgentCore Memory failures already get.

The model is resolved **at write time** (`resolve_model_for`, cached per ARN):
a harness-backed record's model is a field on the harness and a runtime-backed
record's model is its runtime's `MODEL_ID` environment variable. Pricing at write
time is what keeps a redeploy from re-pricing history — the old read-time
`model_map` lookup re-priced every past token at whatever the agent runs *now*.

The four token tiers are dimensions here. Bedrock bills input, output, cache
reads and cache writes at four different rates — a cache read is a tenth of an
uncached input token — so a single undifferentiated input counter cannot be
priced at all.
"""
import logging
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from core import clock
from repositories.usage_repository import UsageRepository, month_shard
from data.model_rates import (
    LONG_COUNTERS,
    cost_micros as _cost_micros,
    learned_rates,
    resolve_rate,
    set_learned_rates,
)

logger = logging.getLogger(__name__)

# An AgentCore gateway prefixes every tool it serves with its target name, as
# `<target>___<tool>`. The prefix is what lets a recorded tool call be traced
# back to a gateway (`mcp_gateway_usage`) without confusing it with an agent's
# built-in tools, which carry bare names. Mirrors `mcp_apps_service`.
_GATEWAY_TOOL_SEPARATOR = "___"

# Every counter a read path reports, so a missing attribute becomes 0 in one
# place rather than at each call site.
COUNTER_NAMES = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "turns",
    "measured_turns",
    "tool_calls",
    "interrupted_turns",
    "failed_turns",
    "threads_started",
    # Write-time cost accounting. `model_cost_micros` is integer micro-dollars
    # priced from the repository rate card when the turn ended; `priced_turns`
    # and `unpriced_turns` say how many turns that figure does and does not
    # cover, so a total can state its own completeness.
    "model_cost_micros",
    "priced_turns",
    "unpriced_turns",
    "model_calls",
)

# A day is "measured" if it carried either token attribute. `add` drops zero
# counters, so absence means the stream never reported usage for that day —
# which is what a backfilled day looks like.
#
# Kept only as the **fallback** for items written before `measured_turns` existed.
# Reading measurement off the token attributes is necessarily day-granular: a
# stored item is one whole day for one agent, accumulated with `ADD`, so a single
# turn that reported tokens creates the attribute and every unmeasured turn
# sharing that day then disappears from `unmeasured_turns`. Two real paths hit
# that — the backfill's `--before` boundary day, and a harness turn whose metadata
# never arrived landing beside one whose did — and the symptom is the "+" floor
# marker and the backfill notice both silently switching off while the total is
# still a floor. `measured_turns` counts the turns themselves, so new days are
# exact.
_TOKEN_NAMES = ("input_tokens", "output_tokens")

# Guardrail event counters: counts only (no content, no PII values) for admin
# governance view, not an audit log of what was said.
GUARDRAIL_COUNTER_NAMES = (
    "interventions",
    "blocked_input", "blocked_output",
    "anonymized_input", "anonymized_output",
    "policy_content", "policy_pii", "policy_topic", "policy_word",
    # Turns on which a guardrail trace was present at all, intervention or not.
    # Zero interventions on a guarded agent and zero on an unguarded one are the
    # same number; this is the counter that tells them apart.
    "scanned_turns",
)
_GUARDRAIL_POLICIES = frozenset({"content", "pii", "topic", "word"})
# Open-ended breakdowns stored as prefixed attributes on the same item
# (`filter_INSULTS`, `confidence_LOW`) and read back as dicts (`by_filter`,
# `by_confidence`). Prefixed rather than enumerated because the filter set is the
# guardrail's configuration, not this code's.
_GUARDRAIL_BREAKDOWNS = (("by_filter", "filter_"), ("by_confidence", "confidence_"))


def guardrail_sum(aggregates) -> Dict[str, Any]:
    """Fold per-agent (or per-user) guardrail aggregates into one total."""
    total: Dict[str, Any] = {name: 0 for name in GUARDRAIL_COUNTER_NAMES}
    for key, _ in _GUARDRAIL_BREAKDOWNS:
        total[key] = {}
    for agg in aggregates:
        for name in GUARDRAIL_COUNTER_NAMES:
            total[name] += int(agg.get(name) or 0)
        for key, _ in _GUARDRAIL_BREAKDOWNS:
            for label, count in (agg.get(key) or {}).items():
                total[key][label] = total[key].get(label, 0) + int(count or 0)
    return total


def month_shards(start_date: str, end_date: str) -> List[str]:
    """Every `YYYY-MM` partition suffix a date window touches, in order.

    A 30-day window spans at most two, which is the reason the partition key is
    sharded by month: the read stays a bounded number of keyed queries instead
    of a Scan.
    """
    shards = [start_date[:7]]
    if end_date[:7] != start_date[:7]:
        year, month = int(start_date[:4]), int(start_date[5:7])
        while f"{year:04d}-{month:02d}" != end_date[:7]:
            month += 1
            if month > 12:
                year, month = year + 1, 1
            shards.append(f"{year:04d}-{month:02d}")
    return shards


class UsageService:
    """Records what a turn consumed."""

    def __init__(
        self,
        repository: Optional[UsageRepository],
        registry: Optional[Any] = None,
        harness: Optional[Any] = None,
        cache_seconds: int = 300,
        bedrock: Optional[Any] = None,
    ):
        self.repository = repository
        # For resolving an application inference profile ARN to the model it
        # routes to. Lazily constructed; tests inject a stub.
        self._bedrock = bedrock
        # Injected rather than constructed so a registry or harness failure
        # cannot break counter reads, and so tests never reach AWS. Same shape
        # as KnowledgeService's `_harness`.
        self._registry = registry
        self._harness = harness
        self._cache_seconds = cache_seconds
        self._model_cache: Optional[Dict[str, str]] = None
        self._model_cache_at = 0.0
        # arn -> (resolved at, model id). The write path asks per turn, so a
        # miss must not cost every turn a control-plane round trip.
        self._model_by_arn: Dict[str, Any] = {}
        self._lock = threading.Lock()
        # Per-thread read completeness. FastAPI runs this feature's `sync def`
        # handlers in a threadpool thread, and every read below happens inside the
        # one that is serving the request — so a thread-local flag is exactly
        # request-scoped without a context variable. Threads are reused, hence
        # `begin_read` resets it rather than trusting the default.
        self._reads = threading.local()
        # `deployed:<arn>` -> registry record id. A turn served through the
        # deployed-resource fallback (the registry listing unavailable at the
        # moment the chat began) is written under the synthetic id, and read back
        # here as the record that owns that ARN — otherwise the same agent shows
        # up twice on the leaderboard, once under a name nobody recognises.
        self._aliases: Dict[str, str] = {}

    @property
    def configured(self) -> bool:
        return self.repository is not None

    def set_record_aliases(self, records: Iterable[Any]) -> None:
        """Map every record's deployed ARNs to its id, for `canonical`."""
        aliases: Dict[str, str] = {}
        for record in records or []:
            record_id = getattr(record, "record_id", None)
            if not record_id:
                continue
            for attr in ("harness_arn", "agent_runtime_arn"):
                arn = getattr(record, attr, None)
                if arn:
                    aliases[f"deployed:{arn}"] = record_id
        self._aliases = aliases

    def canonical(self, record_id: str) -> str:
        """The registry id a ledger key belongs to; itself when not an alias."""
        return self._aliases.get(record_id, record_id)

    def _ledger_ids(self, record_id: str) -> List[str]:
        """This record's own id plus every alias that folds into it."""
        return [record_id] + [alias for alias, target in self._aliases.items() if target == record_id]

    def record_turn(
        self,
        *,
        agent_record_id: str,
        owner_sub: str,
        input_tokens: int,
        output_tokens: int,
        tool_calls: Dict[str, int],
        turns: int = 1,
        interrupted: bool = False,
        failed: bool = False,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        thread_started: bool = False,
        date: Optional[str] = None,
        thread_id: str = "",
        turn_id: str = "",
        model_id: Optional[str] = None,
        started_at: Optional[str] = None,
        ended_at: Optional[str] = None,
        model_calls: int = 0,
        usage_reported: bool = False,
        blocked: bool = False,
        team: Optional[str] = None,
        long_tokens: Optional[Dict[str, int]] = None,
    ) -> Dict[str, Any]:
        """Write one turn's event, then derive the day's counters from it.

        **The event is the source of truth and the counters are its projection.**
        `TURNS#{month}` gets one item per `turn_id` via a conditional put. Only
        when that put succeeds are the four counter partitions ADDed — so a turn
        that flushes twice (once at `messageStop`, once post-loop) is counted once,
        and its second flush folds tokens onto the existing event and onto the
        counters *without* a second `turns`. The old counter-only design lost
        eight turns from one ledger and none from another with no way to tell
        which was right; the event is what makes that answerable.

        **Cost is decided here, not at read time.** `model_id` is what the agent
        was running when this turn ended, resolved by the caller from the harness
        or the runtime's environment. It is priced against the repository rate
        card and stored as integer micro-dollars; a model the card does not know
        leaves the cost *absent* and bumps `unpriced_turns`, never writes 0. A
        redeploy that changes the model therefore never re-prices history.

        `usage_reported` says the runtime *did* report this turn's usage, even if
        every tier in it is zero. A turn the guardrail blocked before the model ran
        arrives exactly like that — a metadata event with a trace and no tokens —
        and its zero is the true figure, not a missing one. Without the flag it
        was written unmeasured and sat in the "토큰 기록 없음" count beside the
        pre-ledger history it has nothing in common with. `blocked` names that
        ending as its own status.

        `interrupted` and `failed` are separate endings, not one "did not finish":
        the client hanging up is a fact about a reader and a model or runtime error
        is a fact about the platform. Both are still turns — a turn that spent
        tokens and failed cost real money.

        Returns `{"event": "created" | "updated" | "skipped", "cost_micros"}`.
        """
        if not self.configured or not agent_record_id:
            return {"event": "skipped", "cost_micros": None}

        ended_at = ended_at or clock.now().isoformat()
        # The turn's day — and therefore its partition — follows `started_at`,
        # which every flush of one turn shares. Keying on `ended_at` split a turn
        # straddling midnight (or month end) into two events in two partitions.
        anchor = started_at or ended_at
        if date:
            day = date
        else:
            try:
                day = clock.date_of(datetime.fromisoformat(str(anchor).replace("Z", "+00:00")))
            except ValueError:
                day = clock.today()
        shard = month_shard(day)
        turn_id = turn_id or f"{thread_id or 'thread'}:{uuid.uuid4()}"

        tiers = {
            "input_tokens": int(input_tokens or 0),
            "output_tokens": int(output_tokens or 0),
            "cache_read_tokens": int(cache_read_tokens or 0),
            "cache_write_tokens": int(cache_write_tokens or 0),
        }
        # The part of each tier spent in long-context calls (`long_input_tokens`
        # ⊆ `input_tokens`), keyed by counter name. Written only when nonzero, so a
        # turn on a one-card model carries exactly the attributes it always did.
        tiers.update({
            name: int((long_tokens or {}).get(name) or 0)
            for name in LONG_COUNTERS
            if int((long_tokens or {}).get(name) or 0)
        })
        self.ensure_learned_rates()
        # Priced at the rate in force on the turn's own day, so a price change
        # learned later with an earlier effective date is a repricing, never a
        # silent re-reading of history at today's tariff.
        rate = resolve_rate(model_id, day)
        cost = _cost_micros(tiers, rate) if rate else None
        # Any tier reported means the runtime told us what the turn cost. A turn
        # measured at genuinely zero output (an immediate refusal) still counts as
        # measured — this asks whether a report arrived, not whether it was nonzero.
        measured = 1 if (any(tiers.values()) or usage_reported) else 0
        if failed:
            status = "failed"
        elif interrupted:
            status = "interrupted"
        elif blocked:
            status = "blocked"
        else:
            status = "completed"

        # Keyed by the turn id alone. The two flushes of one turn (messageStop and
        # post-loop) carry different `ended_at` values; a key that embedded the
        # time made them two events — the first unmeasured with zero tokens — and
        # the turn read as unmeasured beside a cost. Time ordering is done on read.
        event_pk, event_sk = f"TURNS#{shard}", f"T#{turn_id}"
        event: Dict[str, Any] = {
            "turn_id": turn_id,
            "thread_id": thread_id,
            "agent_record_id": agent_record_id,
            "owner_sub": owner_sub,
            "team": team or None,
            "model_id": model_id,
            "routing": rate["routing"] if rate else None,
            "rate_card_version": rate["version"] if rate else None,
            "started_at": started_at,
            "ended_at": ended_at,
            **tiers,
            "model_calls": int(model_calls or 0),
            "tool_calls": dict(tool_calls),
            "status": status,
            "measured": bool(measured),
            "source": "stream",
        }
        if cost is not None:
            event["model_cost_micros"] = cost
        event = {key: value for key, value in event.items() if value is not None}

        try:
            created = self.repository.put_if_absent(event_pk, event_sk, event)
        except Exception:
            # No event, no counters: a counter without its event is exactly the
            # unexplainable drift this design exists to remove. The turn is
            # logged for a repair pass rather than half-recorded.
            logger.warning("Turn event lost: %s / %s", event_pk, event_sk, exc_info=True)
            return {"event": "skipped", "cost_micros": cost}

        if created:
            delta_cost = cost or 0
            measured_delta = measured * turns
            # A turn is *priced* only when it reported tokens: a registered model
            # with no tokens has no figure yet, not a figure of $0. A rate that
            # lacks a tier the turn used leaves it unpriced the same as no rate.
            priced_delta = turns if (rate and measured and cost is not None) else 0
            unpriced_delta = turns if (not rate or (measured and cost is None)) else 0
            interrupted_delta = 1 if interrupted else 0
            failed_delta = 1 if failed else 0
        else:
            # Same turn again. Fold the new tokens onto the event and price the
            # merged total, so the counter delta is exactly the cost the extra
            # tokens added — and never a second turn, thread start or ending.
            existing = self.repository.get(event_pk, event_sk) or {}
            # Read every prior figure before the update: a repository that hands
            # back its own live object would otherwise show the new value here.
            previous_cost = int(existing.get("model_cost_micros") or 0)
            previously_measured = bool(existing.get("measured"))
            previous_status = str(existing.get("status") or "completed")
            merged = {
                key: int(existing.get(key) or 0) + int(tiers.get(key) or 0)
                for key in (*tiers, *(name for name in LONG_COUNTERS if name in existing and name not in tiers))
            }
            new_cost = _cost_micros(merged, rate) if rate else None
            fields: Dict[str, Any] = {
                **merged,
                "model_calls": int(existing.get("model_calls") or 0) + int(model_calls or 0),
                "status": status,
                "ended_at": ended_at,
                "measured": bool(any(merged.values()) or previously_measured or usage_reported),
            }
            if new_cost is not None:
                fields["model_cost_micros"] = new_cost
            elif "model_cost_micros" in existing:
                # The first flush priced nothing (0, unmeasured) and this one
                # cannot price what it added — a tier the rate lacks. Left in
                # place, that 0 reads as a figure: the repricer takes a stored
                # cost as "already priced" and moves only the dollars, so the
                # turn stays under 미가격 forever once the rate does arrive.
                fields["model_cost_micros"] = None
            try:
                self.repository.set_fields(event_pk, event_sk, fields)
            except Exception:
                logger.warning("Turn event update lost: %s / %s", event_pk, event_sk, exc_info=True)
            delta_cost = (new_cost or 0) - previous_cost
            # A turn that reported nothing on its first flush and tokens on its
            # second becomes measured — and, with a rate, priced — now.
            measured_delta = 1 if (measured and not previously_measured) else 0
            priced_delta = 1 if (rate and measured and new_cost is not None and not previously_measured) else 0
            unpriced_delta = 1 if (rate and measured and new_cost is None and not previously_measured) else 0
            # An ending that arrives after the first flush (a cancel or a runtime
            # error mid tool loop) is still this turn's ending: counted once, on
            # the transition away from `completed`.
            interrupted_delta = 1 if (status == "interrupted" and previous_status in ("completed", "blocked")) else 0
            failed_delta = 1 if (status == "failed" and previous_status in ("completed", "blocked")) else 0
            turns = 0
            thread_started = False

        counters = {
            **tiers,
            "turns": turns,
            "measured_turns": measured_delta,
            "tool_calls": sum(tool_calls.values()),
            "model_calls": int(model_calls or 0),
            "interrupted_turns": interrupted_delta,
            "failed_turns": failed_delta,
            "threads_started": 1 if thread_started else 0,
            "model_cost_micros": delta_cost,
            "priced_turns": priced_delta,
            "unpriced_turns": unpriced_delta,
        }

        writes = [(f"AGENTS#{shard}", f"D#{day}#A#{agent_record_id}", counters)]
        if owner_sub:
            writes.append((f"USERS#{shard}", f"D#{day}#U#{owner_sub}", counters))
            # Per (agent, user) so a person's spend can be split by the agent that
            # served them, and `distinct_users` can count items rather than sums.
            writes.append((f"AGENT#{agent_record_id}#USERS#{shard}", f"D#{day}#U#{owner_sub}", counters))
        if team:
            # The chargeback axis the platform owner asks about first; one row per
            # (day, team), summed like the per-user rows.
            writes.append((f"TEAMS#{shard}", f"D#{day}#G#{team}", counters))
        if model_id:
            # The model mix. Keyed by the model this turn actually ran on, so a
            # window spanning a redeploy shows both models with their own tokens
            # and their own cost.
            writes.append((f"AGENT_MODELS#{shard}", f"D#{day}#A#{agent_record_id}#M#{model_id}", counters))
        for name, count in tool_calls.items():
            writes.append((
                f"AGENT#{agent_record_id}#TOOLS#{shard}",
                f"D#{day}#T#{name}",
                {"tool_calls": count},
            ))

        for pk, sk, values in writes:
            try:
                self.repository.add(pk, sk, values)
            except Exception:
                logger.warning("Usage counter lost: %s / %s", pk, sk, exc_info=True)
                # The event survived, so this day can be rebuilt from it. Leave
                # the marker a repair pass looks for.
                try:
                    self.repository.set_fields(
                        f"LEDGER_REPAIR#{shard}",
                        f"D#{day}#A#{agent_record_id}",
                        {"needs_rebuild": True, "at": ended_at},
                    )
                except Exception:
                    logger.warning("Repair marker lost for %s", agent_record_id, exc_info=True)

        return {"event": "created" if created else "updated", "cost_micros": cost}

    def record_guardrail_event(
        self,
        agent_record_id: str,
        owner_sub: str,
        action: str,
        stage: str,
        policies: List[str],
        date: Optional[str] = None,
        filter_types: Optional[List[str]] = None,
        confidences: Optional[List[str]] = None,
        thread_id: str = "",
        turn_id: str = "",
        at: Optional[str] = None,
    ) -> None:
        """Record one Guardrail intervention: an event row, then the counters.

        Labels only — no matched text, no PII values — because this feeds an
        admin governance view, not an audit log of what was said. Silent on bad
        input rather than raising into the stream.

        `filter_types` are the filters that fired (`INSULTS`, `PROMPT_ATTACK`, a
        PII type, a topic name) and `confidences` the levels content filters
        reported; both are configuration labels the trace carries, stored as
        prefixed counters so the read side can list them without knowing the set.

        The event (`GUARDRAIL_EVENTS#{month}`) is what makes a count answerable:
        it names the thread and the turn, so the admin who sees "5 interventions"
        can open the five conversations and read what was said there — the
        conversation store already holds the message; this row only points at
        it. Keyed by time plus a nonce rather than by turn: input and output can
        each intervene on one turn, and both are interventions."""
        if not self.configured or not agent_record_id:
            return
        act = (action or "").upper()
        stg = (stage or "").lower()
        if act not in ("BLOCKED", "ANONYMIZED") or stg not in ("input", "output"):
            return
        day = date or clock.today()
        shard = month_shard(day)
        at = at or clock.now().isoformat()
        labels = [x for x in (filter_types or []) if isinstance(x, str) and x]
        levels = [x.upper() for x in (confidences or []) if isinstance(x, str) and x]
        event = {
            "at": at,
            "day": day,
            "agent_record_id": agent_record_id,
            "owner_sub": owner_sub or "",
            "thread_id": thread_id or "",
            "turn_id": turn_id or "",
            "action": act,
            "stage": stg,
            "policies": sorted(p for p in set(policies or []) if p in _GUARDRAIL_POLICIES),
            "filter_types": labels,
            "confidences": levels,
        }
        try:
            self.repository.put_if_absent(
                f"GUARDRAIL_EVENTS#{shard}", f"E#{at}#{uuid.uuid4().hex[:8]}", event
            )
        except Exception:
            logger.warning("Guardrail event lost: GUARDRAIL_EVENTS#%s", shard, exc_info=True)

        counters = {"interventions": 1, f"{act.lower()}_{stg}": 1}
        for p in set(policies or []):
            if p in _GUARDRAIL_POLICIES:
                counters[f"policy_{p}"] = counters.get(f"policy_{p}", 0) + 1
        for label in filter_types or []:
            if isinstance(label, str) and label:
                counters[f"filter_{label}"] = counters.get(f"filter_{label}", 0) + 1
        for level in confidences or []:
            if isinstance(level, str) and level:
                counters[f"confidence_{level.upper()}"] = counters.get(f"confidence_{level.upper()}", 0) + 1
        writes = [(f"GUARDRAIL#{shard}", f"D#{day}#A#{agent_record_id}", counters)]
        if owner_sub:
            writes.append((f"GUARDRAIL_USERS#{shard}", f"D#{day}#U#{owner_sub}", dict(counters)))
        for pk, sk, c in writes:
            try:
                self.repository.add(pk, sk, c)
            except Exception:
                logger.warning("Guardrail counter lost: %s / %s", pk, sk, exc_info=True)

    def guardrail_events(self, start_date: str, end_date: str, limit: int = 20) -> List[Dict[str, Any]]:
        """The interventions in a window, newest first, at most `limit`.

        The month partition is read whole and filtered on the event's own `day`,
        like `turn_events`; the sort key carries the raw timestamp so ordering
        is by when it happened, not by when the write landed."""
        events: List[Dict[str, Any]] = []
        for shard in month_shards(start_date, end_date):
            try:
                items = self.repository.query_prefix(f"GUARDRAIL_EVENTS#{shard}", "E#")
            except Exception:
                self._reads.complete = False
                logger.warning("Guardrail events read failed: GUARDRAIL_EVENTS#%s", shard, exc_info=True)
                continue
            for item in items:
                day = str(item.get("day") or "")
                if start_date <= day <= end_date:
                    events.append(item)
        events.sort(key=lambda event: str(event.get("at", "")), reverse=True)
        return events[:limit]

    def record_policy_denial(
        self,
        *,
        agent_record_id: str,
        owner_sub: str,
        team: Optional[str],
        tool_name: str,
        thread_id: str = "",
        turn_id: str = "",
        date: Optional[str] = None,
        at: Optional[str] = None,
    ) -> None:
        """One gateway policy denial seen in the stream (services/policy_denial.py).

        Same shape as a guardrail intervention: an event that names the
        conversation, then counters per agent and per team. `team` None is
        stored under `-` so the unattributed count is a row, not a hole."""
        if not self.configured or not agent_record_id:
            return
        day = date or clock.today()
        shard = month_shard(day)
        at = at or clock.now().isoformat()
        tool = tool_name or "unknown"
        event = {
            "at": at, "day": day, "agent_record_id": agent_record_id, "owner_sub": owner_sub or "",
            "team": team or "", "tool_name": tool, "thread_id": thread_id or "", "turn_id": turn_id or "",
        }
        try:
            self.repository.put_if_absent(f"POLICY_EVENTS#{shard}", f"E#{at}#{uuid.uuid4().hex[:8]}", event)
        except Exception:
            logger.warning("Policy denial event lost: POLICY_EVENTS#%s", shard, exc_info=True)
        counters = {"denials": 1, f"tool_{tool}": 1}
        writes = [
            (f"POLICY#{shard}", f"D#{day}#A#{agent_record_id}", counters),
            (f"POLICY_TEAMS#{shard}", f"D#{day}#G#{team or '-'}", dict(counters)),
        ]
        for pk, sk, c in writes:
            try:
                self.repository.add(pk, sk, c)
            except Exception:
                logger.warning("Policy denial counter lost: %s / %s", pk, sk, exc_info=True)

    @staticmethod
    def _fold_denials(bucket: Dict[str, Any], item: Dict[str, Any]) -> None:
        bucket["denials"] = bucket.get("denials", 0) + int(item.get("denials") or 0)
        for key, value in item.items():
            if isinstance(key, str) and key.startswith("tool_"):
                bucket.setdefault("tools", {})
                bucket["tools"][key[5:]] = bucket["tools"].get(key[5:], 0) + int(value or 0)

    def policy_totals(self, start_date: str, end_date: str) -> Dict[str, Any]:
        total: Dict[str, Any] = {"denials": 0, "tools": {}}
        for item in self._items("POLICY#", start_date, end_date):
            self._fold_denials(total, item)
        return total

    def policy_team_totals(self, start_date: str, end_date: str) -> Dict[str, Dict[str, Any]]:
        """team -> {denials, tools}; the `-` key is the unattributed bucket."""
        out: Dict[str, Dict[str, Any]] = {}
        for item in self._items("POLICY_TEAMS#", start_date, end_date):
            team = self._suffix(str(item.get("sk", "")), "#G#")
            if team:
                self._fold_denials(out.setdefault(team, {"denials": 0, "tools": {}}), item)
        return out

    def policy_events(self, start_date: str, end_date: str, limit: int = 20) -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []
        for shard in month_shards(start_date, end_date):
            try:
                items = self.repository.query_prefix(f"POLICY_EVENTS#{shard}", "E#")
            except Exception:
                self._reads.complete = False
                logger.warning("Policy events read failed: POLICY_EVENTS#%s", shard, exc_info=True)
                continue
            events.extend(i for i in items if start_date <= str(i.get("day") or "") <= end_date)
        events.sort(key=lambda e: str(e.get("at", "")), reverse=True)
        return events[:limit]

    def record_guardrail_scan(
        self, agent_record_id: str, owner_sub: str, date: Optional[str] = None
    ) -> None:
        """Count one turn on which a guardrail trace was present, whatever it did.

        Written once per turn by the stream, on the first model call that carried
        a trace. Same items as the interventions, so a reader gets both from one
        query and an agent with turns but no scans is visible as unguarded."""
        if not self.configured or not agent_record_id:
            return
        day = date or clock.today()
        shard = month_shard(day)
        writes = [(f"GUARDRAIL#{shard}", f"D#{day}#A#{agent_record_id}", {"scanned_turns": 1})]
        if owner_sub:
            writes.append((f"GUARDRAIL_USERS#{shard}", f"D#{day}#U#{owner_sub}", {"scanned_turns": 1}))
        for pk, sk, c in writes:
            try:
                self.repository.add(pk, sk, c)
            except Exception:
                logger.warning("Guardrail scan counter lost: %s / %s", pk, sk, exc_info=True)

    @staticmethod
    def _blank_guardrail() -> Dict[str, Any]:
        """Blank guardrail counter set, breakdown dicts included."""
        blank: Dict[str, Any] = {name: 0 for name in GUARDRAIL_COUNTER_NAMES}
        for key, _ in _GUARDRAIL_BREAKDOWNS:
            blank[key] = {}
        return blank

    def _guardrail_fold(self, totals: Dict[str, Any], item: Dict[str, Any]) -> None:
        """Add one stored guardrail item onto an aggregate."""
        for name in GUARDRAIL_COUNTER_NAMES:
            totals[name] += int(item.get(name) or 0)
        for key, prefix in _GUARDRAIL_BREAKDOWNS:
            for attr, value in item.items():
                if isinstance(attr, str) and attr.startswith(prefix) and len(attr) > len(prefix):
                    label = attr[len(prefix):]
                    totals[key][label] = totals[key].get(label, 0) + int(value or 0)

    def agent_turns_since(self, start_date: str, end_date: str, since_day: str) -> Dict[str, int]:
        """Turns per agent counting only days on or after `since_day`.

        For the unguarded verdict: scan counting began on a day, and turns before
        it carry no information about whether the agent was guarded. Judging them
        would name every guarded agent as a gap for a whole window."""
        totals: Dict[str, int] = {}
        for item in self._items("AGENTS#", start_date, end_date):
            sort_key = str(item.get("sk", ""))
            prefix, sep, record_id = sort_key.partition("#A#")
            if not sep or not record_id or not prefix.startswith("D#") or prefix[2:] < since_day:
                continue
            record_id = self.canonical(record_id)
            totals[record_id] = totals.get(record_id, 0) + int(item.get("turns") or 0)
        return totals

    def guardrail_daily_totals(self, start_date: str, end_date: str) -> Dict[str, Dict[str, int]]:
        """Interventions and scanned turns per day, keyed `YYYY-MM-DD`. Sparse:
        only days with an item appear, and the caller lays it over the dense
        usage series."""
        by_day: Dict[str, Dict[str, int]] = {}
        for item in self._items("GUARDRAIL#", start_date, end_date):
            sort_key = str(item.get("sk", ""))
            prefix, sep, _ = sort_key.partition("#A#")
            if not sep or not prefix.startswith("D#"):
                continue
            point = by_day.setdefault(prefix[2:], {"interventions": 0, "scanned_turns": 0})
            point["interventions"] += int(item.get("interventions") or 0)
            point["scanned_turns"] += int(item.get("scanned_turns") or 0)
        return by_day

    def guardrail_totals(
        self, start_date: str, end_date: str
    ) -> Dict[str, Dict[str, int]]:
        """Per-agent guardrail aggregates for the window, keyed by record id."""
        totals: Dict[str, Dict[str, int]] = {}
        for item in self._items("GUARDRAIL#", start_date, end_date):
            rid = self._suffix(str(item.get("sk", "")), "#A#")
            if not rid:
                continue
            rid = self.canonical(rid)
            self._guardrail_fold(totals.setdefault(rid, self._blank_guardrail()), item)
        return totals

    def guardrail_user_totals(
        self, start_date: str, end_date: str
    ) -> Dict[str, Dict[str, int]]:
        """Per-user guardrail aggregates, keyed by Cognito subject."""
        totals: Dict[str, Dict[str, int]] = {}
        for item in self._items("GUARDRAIL_USERS#", start_date, end_date):
            sub = self._suffix(str(item.get("sk", "")), "#U#")
            if not sub:
                continue
            self._guardrail_fold(totals.setdefault(sub, self._blank_guardrail()), item)
        return totals

    # --- reads ---------------------------------------------------------------

    def begin_read(self) -> None:
        """Mark this thread's reads complete, before a handler starts reading.

        Called by the route rather than by each read, because a handler makes many
        reads and what it needs to know is whether *all* of them landed.
        """
        self._reads.complete = True

    def reads_complete(self) -> bool:
        """False when any partition this thread asked for could not be read.

        Defaults to True so a caller that never called `begin_read` is not told its
        data is suspect — the flag reports observed failures, it does not assume
        them.
        """
        return getattr(self._reads, "complete", True)

    def _items(self, prefix: str, start_date: str, end_date: str) -> List[Dict[str, Any]]:
        """Every item in `prefix`'s month partitions across the window.

        Sequential on purpose. A window touches at most two partitions, and
        parallel boto3 calls serialise behind botocore's connection pool anyway
        — the concurrency would be imaginary and the code would be worse.

        A partition that fails is logged and skipped — half a leaderboard beats a
        500 — but the skip is **recorded**, because the caller renders a total and
        the reader cannot see the hole in it. `sources.usage` reads this.
        """
        if not self.configured:
            return []
        items: List[Dict[str, Any]] = []
        for shard in month_shards(start_date, end_date):
            try:
                items.extend(
                    self.repository.query(f"{prefix}{shard}", start_date, end_date)
                )
            except Exception:
                self._reads.complete = False
                logger.warning("Usage read failed: %s%s", prefix, shard, exc_info=True)
        return items

    @staticmethod
    def _blank() -> Dict[str, int]:
        totals = {name: 0 for name in COUNTER_NAMES}
        totals["unmeasured_turns"] = 0
        return totals

    @staticmethod
    def _fold(totals: Dict[str, int], item: Dict[str, Any]) -> None:
        """Add one stored item onto an aggregate.

        Attributes are summed rather than counted: one item is a whole day for
        one dimension, written by however many flushes that day's requests took.

        `unmeasured_turns` is `turns - measured_turns`, and nothing else. The
        day-granular heuristic that used to stand in for a missing
        `measured_turns` made the same data answer three different numbers on
        three routes; the backfill stamps every legacy item, so an item without
        the attribute is a defect to surface as "unmeasured", not to guess around.
        """
        for name in COUNTER_NAMES:
            totals[name] += int(item.get(name) or 0)
        turns = int(item.get("turns") or 0)
        totals["unmeasured_turns"] += max(0, turns - int(item.get("measured_turns") or 0))

    @staticmethod
    def _suffix(sort_key: str, marker: str) -> Optional[str]:
        """`"D#2026-08-15#A#rec-1"`, `"#A#"` -> `"rec-1"`.

        Split once from the left and keep the whole remainder: a record id or a
        tool name may itself contain `#`, and truncating at the next one would
        silently merge two dimensions into one row.
        """
        head, separator, tail = sort_key.partition(marker)
        return tail if separator else None

    def agent_totals(
        self, start_date: str, end_date: str
    ) -> Dict[str, Dict[str, int]]:
        """Per-agent aggregates for the window, keyed by registry record id."""
        totals: Dict[str, Dict[str, int]] = {}
        for item in self._items("AGENTS#", start_date, end_date):
            record_id = self._suffix(str(item.get("sk", "")), "#A#")
            if not record_id:
                continue
            self._fold(totals.setdefault(self.canonical(record_id), self._blank()), item)
        return totals

    def record_totals(
        self, record_id: str, start_date: str, end_date: str
    ) -> Dict[str, int]:
        """One agent's aggregate. Same partition as the leaderboard, filtered
        here rather than in a second write path — at this scale that is a few
        hundred items and cheaper than another index."""
        return self.agent_totals(start_date, end_date).get(record_id, self._blank())

    def user_totals(
        self, start_date: str, end_date: str
    ) -> Dict[str, Dict[str, int]]:
        """Per-user aggregates, keyed by Cognito subject."""
        totals: Dict[str, Dict[str, int]] = {}
        for item in self._items("USERS#", start_date, end_date):
            sub = self._suffix(str(item.get("sk", "")), "#U#")
            if not sub:
                continue
            self._fold(totals.setdefault(sub, self._blank()), item)
        return totals

    def team_totals(self, start_date: str, end_date: str) -> Dict[str, Dict[str, int]]:
        """Per-team aggregates, keyed by team name."""
        totals: Dict[str, Dict[str, int]] = {}
        for item in self._items("TEAMS#", start_date, end_date):
            team = self._suffix(str(item.get("sk", "")), "#G#")
            if team:
                self._fold(totals.setdefault(team, self._blank()), item)
        return totals

    def distinct_users(self, record_id: str, start_date: str, end_date: str) -> int:
        """How many different people used this agent in the window.

        Counted by item existence, not by summing anything: `ADD` cannot dedupe,
        so the schema writes one item per (day, user) and the answer is the size
        of the distinct-subject set.
        """
        subs = set()
        for ledger_id in self._ledger_ids(record_id):
            for item in self._items(
                f"AGENT#{ledger_id}#USERS#", start_date, end_date
            ):
                sub = self._suffix(str(item.get("sk", "")), "#U#")
                if sub:
                    subs.add(sub)
        return len(subs)

    def agent_user_matrix(
        self, record_ids: Iterable[str], start_date: str, end_date: str
    ) -> Dict[str, Dict[str, int]]:
        """sub -> registry record id -> turns, over `record_ids`.

        The per-(agent, user, day) items are the only place the ledger holds who
        used which agent; the USERS# roll-up has lost the agent and the AGENTS#
        roll-up the person. Read per record (its own id plus its deployed-ARN
        aliases), the same partitions `distinct_users` counts from.
        """
        matrix: Dict[str, Dict[str, int]] = {}
        for record_id in record_ids:
            for ledger_id in self._ledger_ids(record_id):
                for item in self._items(f"AGENT#{ledger_id}#USERS#", start_date, end_date):
                    sub = self._suffix(str(item.get("sk", "")), "#U#")
                    turns = int(item.get("turns") or 0)
                    if not sub or turns <= 0:
                        continue
                    per_agent = matrix.setdefault(sub, {})
                    per_agent[record_id] = per_agent.get(record_id, 0) + turns
        return matrix

    def daily_users(self, start_date: str, end_date: str) -> Dict[str, set]:
        """day -> the subjects who took at least one turn that day.

        One USERS# item per (day, user) is the schema, so a day's active users is
        the set of items with turns — no summing, and it cannot double count.
        """
        by_day: Dict[str, set] = {}
        for item in self._items("USERS#", start_date, end_date):
            sort_key = str(item.get("sk", ""))
            sub = self._suffix(sort_key, "#U#")
            day = sort_key[2:12] if sort_key.startswith("D#") else ""
            if not sub or not day or int(item.get("turns") or 0) <= 0:
                continue
            by_day.setdefault(day, set()).add(sub)
        return by_day

    def tool_totals(
        self, record_id: str, start_date: str, end_date: str
    ) -> Dict[str, int]:
        """Call count per tool name for one agent."""
        totals: Dict[str, int] = {}
        for ledger_id in self._ledger_ids(record_id):
            for item in self._items(f"AGENT#{ledger_id}#TOOLS#", start_date, end_date):
                name = self._suffix(str(item.get("sk", "")), "#T#")
                if not name:
                    continue
                totals[name] = totals.get(name, 0) + int(item.get("tool_calls") or 0)
        return totals

    def daily_totals(
        self,
        start_date: str,
        end_date: str,
        record_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """One point per day **in the window**, oldest first — idle days included.

        `tokens_known` travels with each point so the trend chart can break the
        line on an unmeasured day instead of drawing it through zero.

        **The series is dense, and that is a correctness fix rather than cosmetics.**
        It used to carry only the days that had items, so every consumer that counted
        rows was wrong about time: the half-window delta splits by row count, so four
        rows across seven days compared one day against a day five days earlier and
        labelled it "앞 1일 대비", and the trend line drew a five-day gap as one step.

        A day with no items is a day with no turns, so its counters are zero and
        `tokens_known` is **true** — zero turns cannot have spent tokens, so nothing
        about it is unknown. `filled` marks the manufactured points, so an idle day
        stays distinguishable from a read one.
        """
        by_day: Dict[str, Dict[str, Any]] = {}
        for item in self._items("AGENTS#", start_date, end_date):
            sort_key = str(item.get("sk", ""))
            extracted_record_id = self._suffix(sort_key, "#A#")
            if not extracted_record_id:
                continue
            if record_id and self.canonical(extracted_record_id) != record_id:
                continue
            prefix, sep, _ = sort_key.partition("#A#")
            if not prefix.startswith("D#"):
                continue
            day = prefix[2:]
            point = by_day.setdefault(
                day,
                {"date": day, **self._blank(), "tokens_known": False, "filled": False},
            )
            self._fold(point, item)
            if any(name in item for name in _TOKEN_NAMES):
                point["tokens_known"] = True

        if not by_day:
            # Nothing was read at all — an unconfigured table, an empty window or a
            # failed shard. Manufacturing a flat line of zeros for that would assert
            # a measurement nobody took.
            return []
        return [
            by_day.get(
                day,
                {"date": day, **self._blank(), "tokens_known": True, "filled": True},
            )
            for day in clock.dates(start_date, end_date)
        ]

    # --- cost ---------------------------------------------------------------

    def user_costs(self, start_date: str, end_date: str) -> Dict[str, Dict[str, int]]:
        """Per-user model cost, summed from the ledger.

        Each turn was priced when it ended, by the model it actually ran on, and
        that cost rode onto the user's day item. So this is a sum, not a
        computation: no rate table, no model lookup, and it cannot disagree with
        the agent leaderboard because both read the same written numbers.
        """
        out: Dict[str, Dict[str, int]] = {}
        for item in self._items("USERS#", start_date, end_date):
            sub = self._suffix(str(item.get("sk", "")), "#U#")
            if not sub:
                continue
            bucket = out.setdefault(
                sub, {"model_cost_micros": 0, "priced_turns": 0, "unpriced_turns": 0}
            )
            for key in bucket:
                bucket[key] += int(item.get(key) or 0)
        return out

    def model_days(self, start_date: str, end_date: str) -> Dict[str, Dict[str, Dict[str, int]]]:
        """model_id -> day -> counters, across every agent.

        The rate manager's view: which models ran, on which days, and how many of
        those turns are still unpriced — so an unregistered family can be shown
        with the first day it went unpriced, which is the natural effective date
        for the rate an admin enters."""
        out: Dict[str, Dict[str, Dict[str, int]]] = {}
        for item in self._items("AGENT_MODELS#", start_date, end_date):
            sort_key = str(item.get("sk", ""))
            tail = self._suffix(sort_key, "#A#")
            if not tail or "#M#" not in tail or not sort_key.startswith("D#"):
                continue
            day = sort_key[2:12]
            model_id = tail.split("#M#", 1)[1]
            self._fold(out.setdefault(model_id, {}).setdefault(day, self._blank()), item)
        return out

    def agent_models(
        self, start_date: str, end_date: str
    ) -> Dict[str, Dict[str, Dict[str, int]]]:
        """record_id -> model_id -> counters, for the models each agent actually ran."""
        out: Dict[str, Dict[str, Dict[str, int]]] = {}
        for item in self._items("AGENT_MODELS#", start_date, end_date):
            tail = self._suffix(str(item.get("sk", "")), "#A#")
            if not tail or "#M#" not in tail:
                continue
            record_id, model_id = tail.split("#M#", 1)
            record_id = self.canonical(record_id)
            self._fold(out.setdefault(record_id, {}).setdefault(model_id, self._blank()), item)
        return out

    def turn_events(
        self,
        start_date: str,
        end_date: str,
        *,
        agent_record_id: Optional[str] = None,
        owner_sub: Optional[str] = None,
        thread_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """The turn events in a window, oldest first, optionally filtered.

        The month partition is read whole and filtered here: a turn's business
        date is derived from `ended_at`, and the sort key carries the raw
        timestamp rather than the date so events order correctly across zones.
        """
        events: List[Dict[str, Any]] = []
        for shard in month_shards(start_date, end_date):
            try:
                items = self.repository.query_prefix(f"TURNS#{shard}", "T#")
            except Exception:
                self._reads.complete = False
                logger.warning("Turn events read failed: TURNS#%s", shard, exc_info=True)
                continue
            for item in items:
                try:
                    day = clock.date_of(
                        datetime.fromisoformat(str(item.get("ended_at")).replace("Z", "+00:00"))
                    )
                except ValueError:
                    continue
                if not (start_date <= day <= end_date):
                    continue
                if agent_record_id and self.canonical(str(item.get("agent_record_id") or "")) != agent_record_id:
                    continue
                if owner_sub and item.get("owner_sub") != owner_sub:
                    continue
                if thread_id and item.get("thread_id") != thread_id:
                    continue
                events.append(item)
        events.sort(key=lambda event: str(event.get("ended_at", "")))
        return events

    # --- learned rates -----------------------------------------------------------

    _LEARNED_PK = "RATES#learned"
    _LEARNED_SK_PREFIX = "R#"

    def load_learned_rates(self) -> int:
        """Read `RATES#learned` and install it as the rate card's overlay.

        Returns the entry count. Never raises: a table that cannot be read
        leaves the previous overlay in place, and a server with no usage table
        has nothing to load. Called lazily before the first turn is priced and
        by the collector on every pass, so a rate one replica learned reaches
        the others within one collector interval.
        """
        if not self.configured:
            return 0
        try:
            items = self.repository.query_prefix(self._LEARNED_PK, self._LEARNED_SK_PREFIX)
        except Exception:
            logger.warning("Learned rates could not be read", exc_info=True)
            self._learned_loaded_at = time.monotonic()
            return len(learned_rates())
        set_learned_rates(items)
        self._learned_loaded_at = time.monotonic()
        return len(learned_rates())

    def rate_entries(self) -> List[Dict[str, Any]]:
        """Every stored overlay entry, raw — `source`, `learned_at`, `registered_by`
        and all — for the rate manager. The pricing overlay (`learned_rates`) keeps
        only what pricing needs."""
        if not self.configured:
            return []
        try:
            return list(self.repository.query_prefix(self._LEARNED_PK, self._LEARNED_SK_PREFIX))
        except Exception:
            logger.warning("Rate entries could not be read", exc_info=True)
            return []

    def put_rate_entry(self, entry: Dict[str, Any]) -> str:
        """Store one dated rate — admin-entered or fetched from the Price List —
        under the same key the bill's learner uses, and reload the overlay.

        Same key on purpose: if the bill later shows two consecutive days at a
        different value with the same effective date, the learner overwrites this
        entry, and the page shows the correction. An admin's figure is a bridge
        until the bill speaks, never an override of it."""
        sort_key = (
            f"{self._LEARNED_SK_PREFIX}{entry['family']}|{entry['routing']}|{entry['tier']}|{entry['effective_from']}"
        )
        self.repository.set_fields(self._LEARNED_PK, sort_key, entry)
        self.load_learned_rates()
        return sort_key

    def delete_rate_entry(self, sort_key: str) -> bool:
        """Remove one overlay entry by its sort key; False when it was not there."""
        if not sort_key.startswith(self._LEARNED_SK_PREFIX):
            return False
        existing = self.repository.get(self._LEARNED_PK, sort_key)
        if existing is None:
            return False
        self.repository.delete(self._LEARNED_PK, sort_key)
        self.load_learned_rates()
        return True

    def ensure_learned_rates(self, max_age_seconds: int = 300) -> None:
        """Load the overlay if it has never been loaded or is older than `max_age_seconds`."""
        loaded_at = getattr(self, "_learned_loaded_at", None)
        if loaded_at is None or time.monotonic() - loaded_at > max_age_seconds:
            self.load_learned_rates()

    def reprice_events(self, start_date: str, end_date: str) -> Dict[str, int]:
        """Bring every measured turn event in the window to the rate in force on
        its day, moving the counters by exactly the difference.

        Two things change a turn's correct cost after it was written: a family
        the card lacked gets learned (the turn goes from unpriced to priced), or
        a price change is learned with an effective date before the turn (the
        turn was priced at the old rate). Both are found the same way — recompute
        from the event's own tokens and compare with what is stored — so a turn
        that is already right is never touched, and running this twice changes
        nothing. Unmeasured events have no tokens and nothing to price.
        """
        report = {"repriced": 0, "newly_priced": 0, "still_unpriced": 0, "unpriced": 0}
        if not self.configured:
            return report
        self.load_learned_rates()
        for shard in month_shards(start_date, end_date):
            try:
                items = self.repository.query_prefix(f"TURNS#{shard}", "T#")
            except Exception:
                logger.warning("Reprice read failed: TURNS#%s", shard, exc_info=True)
                continue
            for item in items:
                if not item.get("measured") or not item.get("model_id"):
                    continue
                anchor = str(item.get("started_at") or item.get("ended_at") or "")
                try:
                    day = clock.date_of(datetime.fromisoformat(anchor.replace("Z", "+00:00")))
                except ValueError:
                    continue
                if not (start_date <= day <= end_date):
                    continue
                tiers = {
                    name: int(item.get(name) or 0)
                    for name in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", *LONG_COUNTERS)
                }
                rate = resolve_rate(str(item["model_id"]), day)
                expected = _cost_micros(tiers, rate) if rate else None
                stored = item.get("model_cost_micros")
                stored = int(stored) if stored is not None else None
                if expected is None and stored is None:
                    report["still_unpriced"] += 1
                    continue
                if stored == expected:
                    continue
                if expected is None:
                    # The rate that priced this turn is gone — an admin removed
                    # an entry. The honest figure is again "none", not the stale
                    # one, and the counters move back with it.
                    fields: Dict[str, Any] = {"model_cost_micros": None}
                else:
                    fields = {
                        "model_cost_micros": expected,
                        "routing": rate["routing"],
                        "rate_card_version": rate["version"],
                    }
                pk, sk = str(item.get("pk")), str(item.get("sk"))
                try:
                    self.repository.set_fields(pk, sk, fields)
                except Exception:
                    logger.warning("Reprice event write failed: %s/%s", pk, sk, exc_info=True)
                    continue
                deltas = {
                    "model_cost_micros": (expected or 0) - (stored or 0),
                    "priced_turns": (1 if stored is None else 0) - (1 if expected is None else 0),
                    "unpriced_turns": (-1 if stored is None else 0) + (1 if expected is None else 0),
                }
                rid = str(item.get("agent_record_id") or "")
                sub = str(item.get("owner_sub") or "")
                targets = [(f"AGENTS#{shard}", f"D#{day}#A#{rid}")]
                if sub:
                    targets.append((f"USERS#{shard}", f"D#{day}#U#{sub}"))
                    targets.append((f"AGENT#{rid}#USERS#{shard}", f"D#{day}#U#{sub}"))
                targets.append((f"AGENT_MODELS#{shard}", f"D#{day}#A#{rid}#M#{item['model_id']}"))
                # The team row is a projection of the same event: leaving it
                # out let team cost drift and surface as "unattributed".
                team = str(item.get("team") or "")
                if team:
                    targets.append((f"TEAMS#{shard}", f"D#{day}#G#{team}"))
                for target_pk, target_sk in targets:
                    try:
                        self.repository.add(target_pk, target_sk, deltas)
                    except Exception:
                        logger.warning("Reprice counter write failed: %s/%s", target_pk, target_sk, exc_info=True)
                report["repriced"] += 1
                if stored is None:
                    report["newly_priced"] += 1
                if expected is None:
                    report["unpriced"] += 1
        return report

    def resolve_model_for(
        self,
        *,
        harness_arn: Optional[str] = None,
        agent_runtime_arn: Optional[str] = None,
    ) -> Optional[str]:
        """The model an agent is running *now*, for the turn being written.

        Harness first: a harness-backed record also carries its companion runtime
        ARN, and that runtime's environment is not what the harness runs. Cached
        per ARN for `cache_seconds`, because this is asked on every turn and the
        answer changes only on redeploy. None on failure — the turn is still
        recorded, just unpriced.
        """
        key = harness_arn or agent_runtime_arn
        if not key:
            return None
        with self._lock:
            hit = self._model_by_arn.get(key)
            if hit and time.monotonic() - hit[0] < self._cache_seconds:
                return hit[1]
        model_id: Optional[str] = None
        try:
            if harness_arn:
                harness_id = harness_arn.rsplit("/", 1)[-1]
                model_id = getattr(self.harness.get_harness(harness_id), "model_id", None)
            elif agent_runtime_arn:
                for runtime in self.registry.list_agent_runtimes():
                    if getattr(runtime, "agent_runtime_arn", None) == agent_runtime_arn:
                        model_id = getattr(runtime, "model_id", None)
                        break
            if model_id and ":application-inference-profile/" in model_id:
                model_id = self._profile_model(model_id) or model_id
        except Exception:
            logger.warning("Could not resolve model for %s", key, exc_info=True)
        # A failed lookup is not remembered: one throttled control-plane call
        # would otherwise write five minutes of turns unpriced, and the stream
        # path cannot re-price them later.
        if model_id:
            with self._lock:
                self._model_by_arn[key] = (time.monotonic(), model_id)
        return model_id

    @property
    def bedrock(self) -> Any:
        if self._bedrock is None:
            import boto3

            from core.config import AWS_REGION

            self._bedrock = boto3.client("bedrock", region_name=AWS_REGION)
        return self._bedrock

    def _profile_model(self, profile_arn: str) -> Optional[str]:
        """The foundation model an application inference profile routes to.

        With `MODEL_COST_AIP_ENABLED` a harness's modelId is a profile ARN, which
        the rate card cannot price; the profile's first model ARN names the model.
        """
        response = self.bedrock.get_inference_profile(inferenceProfileIdentifier=profile_arn)
        for model in response.get("models") or []:
            arn = str(model.get("modelArn") or "")
            if "/" in arn:
                return arn.rsplit("/", 1)[-1]
        return None

    def _safe_records(self) -> List[Any]:
        """Registry records, or [] on failure — a read helper must not raise."""
        try:
            return list(self.registry.agent_records())
        except Exception:
            logger.warning("Could not read agent records for user_costs", exc_info=True)
            return []

    @property
    def registry(self) -> Any:
        if self._registry is None:
            from services.registry_service import RegistryService

            self._registry = RegistryService()
        return self._registry

    @property
    def harness(self) -> Any:
        if self._harness is None:
            from services.harness_service import HarnessService

            self._harness = HarnessService()
        return self._harness

    def model_map(self) -> Dict[str, str]:
        """Registry record id -> model id, resolved at read time.

        The model is on neither the stream nor the record, and where it *is*
        depends on how the agent was built — so both places are read:

        * **harness-backed** — the model is a field on the harness, paired here with
          the record's `harness_arn`;
        * **runtime-backed** — there is no harness, and `InvokeAgentRuntime` takes no
          per-request model override (`models/common.py`), so the runtime's
          `MODEL_ID` environment variable *is* the model for every turn it serves.
          `RegistryService.list_agent_runtimes` reads it out of `GetAgentRuntime`.

        The second half is not an edge case. On this account the busiest agent by
        turns — the only one with recorded tokens — is runtime-backed, so with the
        harness lookup alone every model cost on the page would read "추정 불가"
        while the rates sat right there.

        Harness wins where a record has both. A harness-backed record's companion
        runtime is AWS-managed and its environment is not what the harness runs.

        Cached briefly because each listing fans out a per-item Get call, which are
        the slowest calls in the request. Returns `{}` on total failure: an agent with
        no resolvable model renders "추정 불가", which is a worse figure than a price
        but a better one than a wrong price.
        """
        with self._lock:
            fresh = (
                self._model_cache is not None
                and time.monotonic() - self._model_cache_at < self._cache_seconds
            )
            if fresh:
                return dict(self._model_cache or {})

            models: Dict[str, str] = {}
            try:
                records = list(self.registry.agent_records())
            except Exception:
                logger.warning("Could not read agent records", exc_info=True)
                return {}

            by_harness: Dict[str, str] = {}
            try:
                by_harness = {
                    harness.harness_arn: harness.model_id
                    for harness in self.harness.list_harnesses()
                    if harness.harness_arn and harness.model_id
                }
            except Exception:
                # One resolver failing must not cost the other its answers: a
                # runtime-backed agent's model is still readable when the harness
                # listing times out, and vice versa.
                logger.warning("Could not resolve harness models", exc_info=True)

            by_runtime: Dict[str, str] = {}
            if any(
                getattr(record, "agent_runtime_arn", None)
                and not getattr(record, "harness_arn", None)
                for record in records
            ):
                # Only fetched when some record actually needs it. The listing fans
                # out `GetAgentRuntime` per runtime, and an all-harness registry has
                # no use for it.
                try:
                    by_runtime = {
                        runtime.agent_runtime_arn: runtime.model_id
                        for runtime in self.registry.list_agent_runtimes()
                        if runtime.agent_runtime_arn and runtime.model_id
                    }
                except Exception:
                    logger.warning("Could not resolve runtime models", exc_info=True)

            for record in records:
                model_id = by_harness.get(getattr(record, "harness_arn", None))
                if not model_id:
                    model_id = by_runtime.get(
                        getattr(record, "agent_runtime_arn", None)
                    )
                if model_id:
                    models[record.record_id] = model_id

            self._model_cache = models
            self._model_cache_at = time.monotonic()
            return dict(models)

    # --- composition rollup -------------------------------------------------

    @staticmethod
    def _tool_keys(harness: Any) -> List[str]:
        """The endpoint URLs and gateway ARNs a harness attached, as match keys."""
        keys: List[str] = []
        for tool in getattr(harness, "tools", None) or []:
            config = (tool or {}).get("config") or {}
            gateway = (config.get("agentCoreGateway") or {}).get("gatewayArn")
            if gateway:
                keys.append(gateway)
            url = (config.get("remoteMcp") or {}).get("url")
            if url:
                keys.append(url)
        return keys

    @staticmethod
    def _skill_keys(harness: Any) -> List[str]:
        """The S3 prefixes a harness attached as skill sources."""
        keys: List[str] = []
        for skill in getattr(harness, "skills", None) or []:
            uri = ((skill or {}).get("s3") or {}).get("uri")
            if uri:
                keys.append(uri)
        return keys

    @staticmethod
    def _record_keys(descriptor_content: Any) -> List[str]:
        """The match keys a composable registry record advertises."""
        from services.harness_service import _mcp_url
        from services.registry_service import gateway_arn_of, skill_source_of

        keys = []
        gateway = gateway_arn_of(descriptor_content)
        if gateway:
            keys.append(gateway)
        url = _mcp_url(descriptor_content)
        if url:
            keys.append(url)
        source = skill_source_of(descriptor_content)
        if source and source.get("uri"):
            keys.append(source["uri"])
        return keys

    def composition(
        self, start_date: str, end_date: str
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Which MCP servers, skills and knowledge bases carried real traffic.

        Nothing is stored: each agent's aggregate is rolled up through the
        harness it points at, so a re-composed harness re-interprets the same
        history on the next read.

        Skills are reported as **reach**, not calls. A skill is markdown a
        harness reads; no telemetry anywhere counts that, so the figure is how
        much traffic ran through agents that include it, and the entry says so.
        """
        empty: Dict[str, List[Dict[str, Any]]] = {
            "mcp": [], "skills": [], "knowledge_bases": [], "tools": []
        }
        totals = self.agent_totals(start_date, end_date)

        try:
            harnesses = {
                harness.harness_arn: harness
                # `with_tools`: this rollup matches agents to servers/skills by the
                # harness's `tools`/`skills`, which the plain listing omits.
                for harness in self.harness.list_harnesses(with_tools=True)
                if harness.harness_arn
            }
            agent_records = self.registry.agent_records()
        except Exception:
            logger.warning("Composition rollup unavailable", exc_info=True)
            return empty

        # Which agents (by record id) each match key reaches, and their traffic.
        reach: Dict[str, Dict[str, int]] = {}
        for record in agent_records:
            harness = harnesses.get(getattr(record, "harness_arn", None))
            if harness is None:
                continue
            agent = totals.get(record.record_id)
            if not agent:
                continue
            for key in self._tool_keys(harness) + self._skill_keys(harness):
                entry = reach.setdefault(key, {"turns": 0, "agents": 0})
                entry["turns"] += agent["turns"]
                entry["agents"] += 1

        from services.harness_service import _mcp_url
        from services.registry_service import gateway_arn_of

        sections: Dict[str, List[Dict[str, Any]]] = {
            "mcp": [], "skills": [], "knowledge_bases": []
        }
        # Each record's descriptor is read once and kept: the URL-fronting pass
        # below needs every gateway's ARN before any URL record is scored.
        details: Dict[str, List[tuple]] = {"mcp": [], "skills": []}
        for descriptor_type, section in (
            ("MCP", "mcp"),
            ("AGENT_SKILLS", "skills"),
        ):
            try:
                records = self.registry.list_records(descriptor_type=descriptor_type)
            except Exception:
                logger.warning("Could not list %s records", descriptor_type, exc_info=True)
                continue
            for summary in records:
                try:
                    detail = self.registry.get_record(summary.record_id)
                except Exception:
                    continue
                details[section].append(
                    (summary, getattr(detail, "descriptor_content", None))
                )

        # Gateway target endpoint -> target names, over every gateway record. A
        # Runtime-hosted MCP server is registered by its invocations URL that no
        # harness attaches directly — agents call it through a gateway target
        # whose endpoint is that URL — so without this the record read 0 while
        # the gateway took its calls (see `record_reach`).
        fronting: Dict[str, set] = {}
        for _, content in details["mcp"]:
            gateway_arn = gateway_arn_of(content)
            if not gateway_arn:
                continue
            try:
                endpoints = self.harness.gateway_target_endpoints(gateway_arn)
            except Exception:
                logger.warning(
                    "Could not read target endpoints for %s", gateway_arn, exc_info=True
                )
                continue
            for name, endpoint in endpoints.items():
                fronting.setdefault(endpoint, set()).add(name)

        for section in ("mcp", "skills"):
            for summary, content in details[section]:
                matched = {"turns": 0, "agents": 0}
                for key in self._record_keys(content):
                    hit = reach.get(key)
                    if hit:
                        matched["turns"] += hit["turns"]
                        matched["agents"] += hit["agents"]
                url = _mcp_url(content) if section == "mcp" else None
                if url and not gateway_arn_of(content) and fronting.get(url):
                    _, callers = self._calls_by_target(
                        fronting[url], start_date, end_date
                    )
                    matched["turns"] += sum(turns for _, turns in callers)
                    matched["agents"] += len(callers)
                # A knowledge base is an MCP record whose descriptor names a
                # gateway; a plain MCP server names a URL. Splitting them here
                # keeps the UI from calling a knowledge base a tool server.
                target = (
                    "knowledge_bases"
                    if section == "mcp" and gateway_arn_of(content)
                    else section
                )
                sections[target].append({
                    "record_id": summary.record_id,
                    "name": summary.name,
                    "turns": matched["turns"],
                    "agents": matched["agents"],
                    "metric": "reach" if section == "skills" else "traffic",
                })

        tools: Dict[str, int] = {}
        for record_id in totals:
            for name, count in self.tool_totals(
                record_id, start_date, end_date
            ).items():
                tools[name] = tools.get(name, 0) + count

        return {
            "mcp": sorted(sections["mcp"], key=lambda e: -e["turns"]),
            "skills": sorted(sections["skills"], key=lambda e: -e["turns"]),
            "knowledge_bases": sorted(
                sections["knowledge_bases"], key=lambda e: -e["turns"]
            ),
            "tools": sorted(
                [
                    {"name": name, "tool_calls": count, "metric": "calls"}
                    for name, count in tools.items()
                ],
                key=lambda e: -e["tool_calls"],
            ),
        }

    def mcp_gateway_usage(
        self, record_id: str, start_date: str, end_date: str
    ) -> Optional[Dict[str, Any]]:
        """Per-tool usage of one MCP **gateway** record, rolled up on read.

        `record_turn` keys every tool call under the calling *agent*, so a gateway
        record has no counter of its own — an agent is called, a gateway is called
        *through*. This reinterprets the agents' counters the way `composition`
        reinterprets their turns: nothing is stored, so history re-rolls on read.

        Attribution is exact and does not touch harness configuration. A gateway
        names every tool it serves `<target>___<tool>`, and its target names are
        the authority for what belongs to it, read live from `ListGatewayTargets`.
        A recorded call whose target is one of this gateway's is counted here —
        which is why it works no matter how many gateways an agent attaches, and
        for runtime-deployed agents that expose no harness `tools` list at all.

        Returns `None` when the record is not a gateway — an agent record, or a
        plain remote MCP server whose bare tool names carry no target and so
        cannot be told apart from built-in tools. Callers read that as "show this
        record's own counters instead".

        `{"agents": n, "turns": t, "tools": {bare_name: calls}}` otherwise, where
        `agents`/`turns` count the agents that actually called this gateway and
        the traffic they carried.
        """
        from services.registry_service import gateway_arn_of

        try:
            detail = self.registry.get_record(record_id)
        except Exception:
            return None
        if getattr(detail, "descriptor_type", None) != "MCP":
            return None
        gateway_arn = gateway_arn_of(getattr(detail, "descriptor_content", None))
        if not gateway_arn:
            return None

        tools, callers = self._gateway_calls(gateway_arn, start_date, end_date)
        return {
            "agents": len(callers),
            "turns": sum(turns for _, turns in callers),
            "tools": tools,
        }

    def _gateway_targets(self, gateway_arn: str) -> set:
        """The target names a gateway serves, or an empty set when they cannot be
        read — then nothing is attributed rather than guessed."""
        try:
            return set(self.harness.gateway_target_names(gateway_arn))
        except Exception:
            logger.warning(
                "Could not list targets for gateway %s", gateway_arn, exc_info=True
            )
            return set()

    def _gateway_calls(
        self, gateway_arn: str, start_date: str, end_date: str
    ) -> "tuple[Dict[str, int], List[tuple]]":
        """Every agent's calls to one gateway: `(tools, callers)`."""
        return self._calls_by_target(
            self._gateway_targets(gateway_arn), start_date, end_date
        )

    def _calls_by_target(
        self, targets: set, start_date: str, end_date: str
    ) -> "tuple[Dict[str, int], List[tuple]]":
        """The recorded `<target>___<tool>` calls whose target is in `targets`.

        Returns `(tools, callers)`: calls per bare tool name, and `(record, turns)`
        for each agent that made at least one such call, with the turns it ran in
        the window. Both empty when there are no targets or the ledger is
        unavailable.
        """
        if not targets:
            return {}, []
        try:
            agent_records = self.registry.agent_records()
            agent_turns = self.agent_totals(start_date, end_date)
        except Exception:
            logger.warning("Gateway usage rollup unavailable", exc_info=True)
            return {}, []

        tools: Dict[str, int] = {}
        callers: List[tuple] = []
        for record in agent_records:
            called_here = False
            for name, count in self.tool_totals(
                record.record_id, start_date, end_date
            ).items():
                if _GATEWAY_TOOL_SEPARATOR not in name:
                    continue
                target, bare = name.split(_GATEWAY_TOOL_SEPARATOR, 1)
                if target not in targets:
                    continue
                tools[bare] = tools.get(bare, 0) + count
                called_here = True
            if called_here:
                agent = agent_turns.get(record.record_id)
                callers.append((record, int(agent["turns"]) if agent else 0))
        return tools, callers

    def _attaching_agents(
        self, keys: List[str], start_date: str, end_date: str
    ) -> List[tuple]:
        """`(record, turns)` for each agent whose harness attaches one of `keys`
        (gateway ARNs, remote MCP URLs or skill S3 prefixes) and carried traffic
        in the window. The same match `composition` makes, for one record."""
        if not keys:
            return []
        wanted = set(keys)
        try:
            harnesses = {
                harness.harness_arn: harness
                for harness in self.harness.list_harnesses(with_tools=True)
                if harness.harness_arn
            }
            agent_records = self.registry.agent_records()
            totals = self.agent_totals(start_date, end_date)
        except Exception:
            logger.warning("Reach rollup unavailable", exc_info=True)
            return []

        hits: List[tuple] = []
        for record in agent_records:
            harness = harnesses.get(getattr(record, "harness_arn", None))
            if harness is None:
                continue
            agent = totals.get(record.record_id)
            if not agent:
                continue
            attached = set(self._tool_keys(harness) + self._skill_keys(harness))
            if attached & wanted:
                hits.append((record, int(agent["turns"])))
        return hits

    def _fronting_targets(self, url: str) -> set:
        """The gateway target names whose `mcpServer` endpoint is `url`, across
        every gateway record in the registry.

        A Runtime-hosted MCP server is registered by its invocations URL, but no
        agent attaches that URL: Runtime-hosted MCP rejects a harness's
        unsigned `remoteMcp` call, so it is always fronted by a gateway target
        (`platform-status` on bap-gateway, live). The target's endpoint is the
        only thing that ties the record to the calls, so it is read from the
        gateway rather than inferred from names.
        """
        from services.registry_service import gateway_arn_of

        targets: set = set()
        try:
            records = self.registry.list_records(descriptor_type="MCP")
        except Exception:
            logger.warning("Could not list MCP records", exc_info=True)
            return targets
        for summary in records:
            try:
                detail = self.registry.get_record(summary.record_id)
            except Exception:
                continue
            gateway_arn = gateway_arn_of(getattr(detail, "descriptor_content", None))
            if not gateway_arn:
                continue
            try:
                endpoints = self.harness.gateway_target_endpoints(gateway_arn)
            except Exception:
                logger.warning(
                    "Could not read target endpoints for %s", gateway_arn, exc_info=True
                )
                continue
            targets.update(name for name, ep in endpoints.items() if ep == url)
        return targets

    def _daily_reach(
        self, agent_ids: set, targets: set, start_date: str, end_date: str
    ) -> List[Dict[str, Any]]:
        """One point per day in the window, oldest first, idle days as zero.

        `turns` is the day's turns of the agents in `agent_ids` — the same
        quantity the record's `turns` sums — and `tool_calls` is the day's
        recorded `<target>___<tool>` calls whose target is in `targets`, across
        every agent (empty `targets`, as for a skill, means zero). Dense for the
        same reason `daily_totals` is: a chart that infers its axis from the
        days that happened to be written is wrong about time whenever the
        platform was idle.
        """
        turns_by_day: Dict[str, int] = {}
        if agent_ids:
            for item in self._items("AGENTS#", start_date, end_date):
                sort_key = str(item.get("sk", ""))
                record_id = self._suffix(sort_key, "#A#")
                if not record_id or self.canonical(record_id) not in agent_ids:
                    continue
                prefix, _, _ = sort_key.partition("#A#")
                if not prefix.startswith("D#"):
                    continue
                day = prefix[2:]
                turns_by_day[day] = turns_by_day.get(day, 0) + int(item.get("turns") or 0)

        calls_by_day: Dict[str, int] = {}
        if targets:
            try:
                agent_records = self.registry.agent_records()
            except Exception:
                logger.warning("Daily reach rollup unavailable", exc_info=True)
                agent_records = []
            for record in agent_records:
                for ledger_id in self._ledger_ids(record.record_id):
                    for item in self._items(
                        f"AGENT#{ledger_id}#TOOLS#", start_date, end_date
                    ):
                        sort_key = str(item.get("sk", ""))
                        prefix, sep, name = sort_key.partition("#T#")
                        if not sep or not prefix.startswith("D#"):
                            continue
                        if _GATEWAY_TOOL_SEPARATOR not in name:
                            continue
                        target, _ = name.split(_GATEWAY_TOOL_SEPARATOR, 1)
                        if target not in targets:
                            continue
                        day = prefix[2:]
                        calls_by_day[day] = (
                            calls_by_day.get(day, 0) + int(item.get("tool_calls") or 0)
                        )

        return [
            {
                "date": day,
                "turns": turns_by_day.get(day, 0),
                "tool_calls": calls_by_day.get(day, 0),
            }
            for day in clock.dates(start_date, end_date)
        ]

    def _reach(
        self,
        metric: str,
        tools: Dict[str, int],
        callers: List[tuple],
        targets: set,
        start_date: str,
        end_date: str,
    ) -> Dict[str, Any]:
        """The Usage tab's non-agent payload. `callers` may name one agent twice
        (attached directly *and* called through a gateway); it is counted once."""
        by_id: Dict[str, tuple] = {}
        for record, turns in callers:
            prior = by_id.get(record.record_id)
            if prior is None or turns > prior[1]:
                by_id[record.record_id] = (record, turns)
        ranked = sorted(
            by_id.values(),
            key=lambda pair: (-pair[1], getattr(pair[0], "name", "") or ""),
        )
        return {
            "metric": metric,
            "agents": len(ranked),
            "turns": sum(turns for _, turns in ranked),
            "tools": tools,
            "agent_records": [
                {
                    "record_id": record.record_id,
                    "name": getattr(record, "name", None),
                    "turns": turns,
                }
                for record, turns in ranked
            ],
            "daily": self._daily_reach(
                set(by_id), targets, start_date, end_date
            ),
        }

    def record_reach(
        self, record_id: str, start_date: str, end_date: str
    ) -> Optional[Dict[str, Any]]:
        """What the Usage tab shows for a record that is not an agent.

        A skill or MCP server is *reached*, not run: `record_turn` books every turn
        and tool call under the calling agent, so such a record's own counters are
        zero by construction. Until this existed the tab fell back to those zeros
        for every skill and every non-gateway MCP record — 0 turns, 0 users, an
        empty trend — while the composition page, reading the same ledger through
        the harnesses, showed their reach. This is that rollup for one record.

        `None` for an agent record (it has counters of its own). Otherwise
        `{"metric", "agents", "turns", "tools", "agent_records", "daily"}`:

        - **gateway MCP** — calls by target prefix, as `mcp_gateway_usage`;
          `metric` is `traffic`.
        - **remote MCP** — agents attaching the URL directly, plus calls through
          any gateway target whose endpoint is that URL; `traffic`.
        - **skill** — agents whose harness attaches the bundle prefix. No
          telemetry counts a skill being read, so `metric` is `reach`, `tools`
          is empty, and `turns` is the traffic of those agents.

        `agent_records` names the agents ranked by traffic, each with its turns,
        so the tab can draw them and send the reader to where evaluation lives. `daily` is the same turns
        and calls per day, dense over the window, for the tab's trend.
        """
        from services.harness_service import _mcp_url
        from services.registry_service import gateway_arn_of, skill_source_of

        try:
            detail = self.registry.get_record(record_id)
        except Exception:
            return None
        kind = getattr(detail, "descriptor_type", None)
        content = getattr(detail, "descriptor_content", None)

        if kind == "MCP":
            gateway_arn = gateway_arn_of(content)
            if gateway_arn:
                targets = self._gateway_targets(gateway_arn)
                tools, callers = self._calls_by_target(targets, start_date, end_date)
                return self._reach("traffic", tools, callers, targets, start_date, end_date)
            url = _mcp_url(content)
            if not url:
                return self._reach("traffic", {}, [], set(), start_date, end_date)
            attached = self._attaching_agents([url], start_date, end_date)
            targets = self._fronting_targets(url)
            tools, callers = self._calls_by_target(targets, start_date, end_date)
            return self._reach(
                "traffic", tools, attached + callers, targets, start_date, end_date
            )

        if kind == "AGENT_SKILLS":
            source = skill_source_of(content) or {}
            keys = [source["uri"]] if source.get("uri") else []
            return self._reach(
                "reach", {}, self._attaching_agents(keys, start_date, end_date),
                set(), start_date, end_date,
            )

        return None
