"""One-off, idempotent rebuild of the insights ledger from what history still holds.

What history holds, and what this recovers from it:

* **Threads** hold every human message (one per turn) and AI messages whose ids are
  `msg-<epoch ms>` — so every past turn gets a `TURNS#` event with an exact time,
  a thread, an owner and an agent. Tokens per turn are gone; the event says so
  (`measured: false`, `source: "backfill"`).
* **The old day counters** hold exact tokens per (day, agent) and per (day, agent,
  user) — but the agent ledger lost turns the user ledger kept (97 vs 105 on the
  live table, against 105 human messages). Turn counts are SET from the threads;
  tokens are left as written.
* **Runtime and harness version history** holds each version's model and the
  moment it went live, so a day's tokens are priced by the model the agent ran
  that day. A day with a model switch inside it splits tokens by the number of
  turns either side of the switch and is flagged `split: "approx"` — the only
  approximation this script makes, and it says so on the item.
* **CloudWatch** still holds per-runtime daily quantities for 63 days (5-minute
  rollups), so the collector can rebuild `RESOURCES#` for the window, and Cost
  Explorer the `RECON#` snapshots.

Every write is a conditional put or a SET. Run it twice and nothing changes.
Today is never touched: the live stream is writing it.

Usage (from `server/`, with `.env` pointing at the live tables):

    python scripts/backfill_ledger.py --start 2026-07-22 --end 2026-09-22          # dry run, prints the report
    python scripts/backfill_ledger.py --start 2026-07-22 --end 2026-09-22 --apply
"""
import argparse
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import clock  # noqa: E402
from data.model_rates import cost_micros, resolve_rate  # noqa: E402
from repositories.usage_repository import month_shard  # noqa: E402

logger = logging.getLogger("backfill_ledger")

_AI_ID = re.compile(r"^msg-(\d{13})$")
_TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")


# --- pure helpers ----------------------------------------------------------------


def _iso_utc(value: str) -> str:
    """A stored timestamp as tz-aware ISO. Naive strings were written by
    `utcnow()`, so UTC is a correction rather than an assumption."""
    text = str(value or "").replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def _parse(value: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _ai_time(message: Dict[str, Any]) -> Optional[str]:
    match = _AI_ID.match(str(message.get("id") or ""))
    if not match:
        return None
    return datetime.fromtimestamp(int(match.group(1)) / 1000, tz=timezone.utc).isoformat()


def turns_from_thread(thread: Any) -> List[Dict[str, Any]]:
    """One turn per human message, timed by the next AI message id when it has one.

    `status` is `completed` when an AI message follows, else `unknown` — the
    stored transcript cannot tell an interrupted turn from a failed one.
    """
    values = getattr(thread, "values", None) or {}
    messages = [m for m in (values.get("messages") or []) if isinstance(m, dict)]
    fallback = _iso_utc(getattr(thread, "updated_at", None) or getattr(thread, "created_at", None) or "")
    turns: List[Dict[str, Any]] = []
    for index, message in enumerate(messages):
        if message.get("type") != "human":
            continue
        ended_at: Optional[str] = None
        followed = False
        for later in messages[index + 1:]:
            if later.get("type") == "human":
                break
            if later.get("type") == "ai":
                followed = True
                ended_at = _ai_time(later) or ended_at
                if ended_at:
                    break
        # No timed AI message after it (the last question never got an answer):
        # the thread's last write is the latest moment it can have happened.
        turns.append({
            "turn_id": f"{thread.thread_id}:{message.get('id') or index}",
            "thread_id": thread.thread_id,
            "agent_record_id": getattr(thread, "agent_record_id", "") or "",
            "owner_sub": getattr(thread, "owner_sub", "") or "",
            "ended_at": ended_at or fallback,
            "status": "completed" if followed else "unknown",
        })
    return turns


def model_at(timeline: List[Tuple[str, str]], moment: str) -> Optional[str]:
    """The model live at `moment`: the latest timeline entry not after it."""
    when = _parse(moment)
    if when is None:
        return None
    chosen: Optional[str] = None
    for from_iso, model_id in sorted(timeline, key=lambda pair: _parse(pair[0]) or datetime.min.replace(tzinfo=timezone.utc)):
        start = _parse(from_iso)
        if start is not None and start <= when:
            chosen = model_id
    return chosen


def day_split(turn_times: List[str], switch_iso: str) -> Tuple[int, int]:
    """How many turns fell before and after a model switch."""
    switch = _parse(switch_iso)
    before = sum(1 for t in turn_times if (_parse(t) or switch) < switch)
    return before, len(turn_times) - before


def _split_tokens(total: int, weights: List[int]) -> List[int]:
    """Integer split by weight; the last share takes the rounding remainder."""
    if not weights:
        return []
    denominator = sum(weights) or len(weights)
    shares = [total * w // denominator for w in weights[:-1]]
    shares.append(total - sum(shares))
    return shares


# --- version history -----------------------------------------------------------------


def runtime_timeline(control: Any, runtime_arn: str) -> List[Tuple[str, str]]:
    """[(went_live_iso, MODEL_ID)] from the runtime's version list."""
    runtime_id = runtime_arn.rsplit("/", 1)[-1]
    out: List[Tuple[str, str]] = []
    token = None
    while True:
        kwargs = {"agentRuntimeId": runtime_id}
        if token:
            kwargs["nextToken"] = token
        page = control.list_agent_runtime_versions(**kwargs)
        for version in page.get("agentRuntimes", []):
            number = version.get("agentRuntimeVersion")
            when = version.get("lastUpdatedAt") or version.get("createdAt")
            try:
                detail = control.get_agent_runtime(agentRuntimeId=runtime_id, agentRuntimeVersion=str(number))
            except Exception:
                logger.warning("GetAgentRuntime %s v%s failed", runtime_id, number, exc_info=True)
                continue
            model = (detail.get("environmentVariables") or {}).get("MODEL_ID")
            if model and when:
                out.append((when.isoformat() if hasattr(when, "isoformat") else str(when), model))
        token = page.get("nextToken")
        if not token:
            break
    return sorted(out)


def harness_timeline(control: Any, harness_arn: str) -> List[Tuple[str, str]]:
    """[(went_live_iso, modelId)] from the harness's version list.

    `ListHarnessVersions` returns summaries only, so each version is fetched with
    `GetHarness(harnessId, harnessVersion)` and the model read from
    `harness.model.bedrockModelConfig.modelId` — the same path `harness_service`
    reads the current model from.
    """
    harness_id = harness_arn.rsplit("/", 1)[-1]
    out: List[Tuple[str, str]] = []
    token = None
    while True:
        kwargs = {"harnessId": harness_id}
        if token:
            kwargs["nextToken"] = token
        page = control.list_harness_versions(**kwargs)
        for version in page.get("harnessVersions") or []:
            number = version.get("harnessVersion")
            when = version.get("createdAt") or version.get("updatedAt")
            try:
                detail = control.get_harness(harnessId=harness_id, harnessVersion=str(number)).get("harness") or {}
            except Exception:
                logger.warning("GetHarness %s v%s failed", harness_id, number, exc_info=True)
                continue
            model = ((detail.get("model") or {}).get("bedrockModelConfig") or {}).get("modelId")
            if model and when:
                out.append((when.isoformat() if hasattr(when, "isoformat") else str(when), model))
        token = page.get("nextToken")
        if not token:
            break
    return sorted(out)


def model_timelines(control: Any, records: Iterable[Any]) -> Dict[str, List[Tuple[str, str]]]:
    timelines: Dict[str, List[Tuple[str, str]]] = {}
    for record in records:
        timeline: List[Tuple[str, str]] = []
        harness_arn = getattr(record, "harness_arn", None)
        runtime_arn = getattr(record, "agent_runtime_arn", None)
        try:
            if harness_arn:
                timeline = harness_timeline(control, harness_arn)
            elif runtime_arn:
                timeline = runtime_timeline(control, runtime_arn)
        except Exception:
            logger.warning("Version history unavailable for %s", record.record_id, exc_info=True)
        timelines[record.record_id] = timeline
        # A turn served through the deployed-resource fallback was written under
        # `deployed:<arn>`; it ran the same versions as the record that owns the
        # ARN, so it is priced from the same history instead of being left
        # unpriced for want of a timeline.
        for arn in (harness_arn, runtime_arn):
            if arn:
                timelines.setdefault(f"deployed:{arn}", timeline)
    return timelines


# --- the rebuild -------------------------------------------------------------------


class _Writer:
    """SET/put through to the repository, or count what would be written."""

    def __init__(self, repository: Any, apply: bool):
        self.repository = repository
        self.apply = apply
        self.puts = 0
        self.sets = 0

    def put(self, pk: str, sk: str, item: Dict[str, Any]) -> bool:
        if not self.apply:
            exists = self.repository.get(pk, sk) is not None
            if not exists:
                self.puts += 1
            return not exists
        created = self.repository.put_if_absent(pk, sk, item)
        if created:
            self.puts += 1
        return created

    def set(self, pk: str, sk: str, fields: Dict[str, Any]) -> None:
        self.sets += 1
        if self.apply:
            self.repository.set_fields(pk, sk, fields)

    def add(self, pk: str, sk: str, counters: Dict[str, int]) -> None:
        """ADD deltas — only for moving an event's figures between counter buckets."""
        self.sets += 1
        if self.apply:
            self.repository.add(pk, sk, counters)


def _tokens_of(item: Dict[str, Any]) -> Dict[str, int]:
    return {key: int(item.get(key) or 0) for key in _TOKEN_KEYS}


def _has_tokens(item: Dict[str, Any]) -> bool:
    return any(key in item for key in _TOKEN_KEYS)


def run(
    usage_repo: Any,
    threads: Iterable[Any],
    timelines: Dict[str, List[Tuple[str, str]]],
    *,
    today: str,
    apply: bool,
) -> Dict[str, Any]:
    """Rebuild TURNS#, correct the day counters, price them by the day's model.

    `today` (business date) and anything after it are skipped: the stream is
    writing those. Returns a report; with `apply=False` nothing is written.
    """
    writer = _Writer(usage_repo, apply)
    report: Dict[str, Any] = {
        "turn_events_created": 0,
        "turn_events_existing": 0,
        "agent_days_corrected": 0,
        "unpriced_agent_days": 0,
        "approx_split_days": 0,
        "skipped_today_or_later": 0,
    }

    # 0. Turn events written under the old time-prefixed key fold into the
    # turn-id key. One turn could hold two rows (the unmeasured messageStop flush
    # and the measured post-loop flush); they merge — tokens summed, measured if
    # either was, cost recomputed — and the old rows are removed.
    report["turn_keys_migrated"] = _migrate_turn_keys(usage_repo, writer)
    report["stream_events_repriced"] = _reprice_unresolved_events(usage_repo, writer, timelines, today)

    # 1. Turn events, and the per-day tallies the counters are corrected from.
    by_agent_day: Dict[Tuple[str, str], List[str]] = {}
    by_agent_user_day: Dict[Tuple[str, str, str], int] = {}
    by_user_day: Dict[Tuple[str, str], int] = {}
    for thread in threads:
        for turn in turns_from_thread(thread):
            when = _parse(turn["ended_at"])
            day = clock.date_of(when) if when else clock.today()
            if day >= today:
                report["skipped_today_or_later"] += 1
                continue
            rid = turn["agent_record_id"]
            if not rid:
                continue
            model_id = model_at(timelines.get(rid, []), turn["ended_at"])
            rate = resolve_rate(model_id, day)
            event = {
                "turn_id": turn["turn_id"],
                "thread_id": turn["thread_id"],
                "agent_record_id": rid,
                "owner_sub": turn["owner_sub"],
                "ended_at": turn["ended_at"],
                "status": turn["status"],
                "measured": False,
                "source": "backfill",
                "model_calls": 0,
                "tool_calls": {},
            }
            if model_id:
                event["model_id"] = model_id
            if rate:
                event["routing"] = rate["routing"]
                event["rate_card_version"] = rate["version"]
            shard = month_shard(day)
            if writer.put(f"TURNS#{shard}", f"T#{turn['turn_id']}", event):
                report["turn_events_created"] += 1
            else:
                report["turn_events_existing"] += 1
            by_agent_day.setdefault((rid, day), []).append(turn["ended_at"])
            if turn["owner_sub"]:
                by_agent_user_day[(rid, turn["owner_sub"], day)] = by_agent_user_day.get((rid, turn["owner_sub"], day), 0) + 1
                by_user_day[(turn["owner_sub"], day)] = by_user_day.get((turn["owner_sub"], day), 0) + 1

    # 2. Evidence per (agent, user, day). Two sources survive: the human messages
    # in threads that still exist (`H`) and the per-(agent, user) items the stream
    # wrote (which include turns from since-deleted threads). Both are evidence
    # and neither can be an overcount, so every figure is the larger of the two —
    # and the two roll-ups are SET from the same per-(agent, user) figures, which
    # is what makes the three ledgers agree by construction. (Measured on the
    # live table before this: agents 19, users 17, agent-users 23 for one day.)
    agent_user_items = _agent_user_items(usage_repo, today)
    au_turns: Dict[Tuple[str, str, str], int] = {}
    for key, item in agent_user_items.items():
        au_turns[key] = int(item.get("turns") or 0)
    for key, count in by_agent_user_day.items():
        au_turns[key] = max(au_turns.get(key, 0), count)
    au_by_agent_day: Dict[Tuple[str, str], int] = {}
    au_by_user_day: Dict[Tuple[str, str], int] = {}
    for (rid, sub, day), turns in au_turns.items():
        au_by_agent_day[(rid, day)] = au_by_agent_day.get((rid, day), 0) + turns
        au_by_user_day[(sub, day)] = au_by_user_day.get((sub, day), 0) + turns

    def _measured(item: Dict[str, Any], turns: int) -> int:
        """The item's measured count as it should stand: kept if stamped, else
        the legacy reading (tokens present -> the old turn count) capped at turns."""
        if "measured_turns" in item:
            return min(turns, int(item.get("measured_turns") or 0))
        old_turns = int(item.get("turns") or 0)
        return min(turns, old_turns) if _has_tokens(item) and old_turns else 0

    # What the (agent, user) rows already say is measured, per agent-day. Evidence
    # of tokens is evidence whichever ledger holds it, so the agent row is raised
    # to its user rows' sum below, and the user rows to the agent's share after —
    # the three ledgers then agree on measured turns as they do on turns.
    au_measured_sum: Dict[Tuple[str, str], int] = {}
    for (rid, sub, day), item in agent_user_items.items():
        au_measured_sum[(rid, day)] = au_measured_sum.get((rid, day), 0) + _measured(item, au_turns[(rid, sub, day)])

    # 3. Agent day items: turns from the evidence, tokens as written, cost by the
    # day's model(s) weighted by the turns each served.
    #
    # Stream events come first. Each carries its own tokens and the model the
    # turn actually ran on — a per-thread override included — so the part of a
    # day they cover is priced exactly, turn by turn. Only the tokens no event
    # accounts for (turns history lost before token capture, or that a pre-ledger
    # server wrote as counters alone) fall back to the version timeline. Pricing
    # the whole day from the timeline priced an Opus 5.5 override turn at the
    # harness's Sonnet rate and called the day settled (live, 2026-09-23).
    events_by_agent_day: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for ev in _scan_prefix(usage_repo, "TURNS#", sk_prefix="T#"):
        if ev.get("source") != "stream" or not ev.get("measured") or not ev.get("model_id"):
            continue
        moment = _parse(str(ev.get("started_at") or ev.get("ended_at") or ""))
        if moment is None:
            continue
        events_by_agent_day.setdefault(
            (str(ev.get("agent_record_id") or ""), clock.date_of(moment)), []
        ).append(ev)

    day_models: Dict[Tuple[str, str], List[Tuple[Optional[str], int]]] = {}
    # model -> tier -> tokens for each agent-day, so the per-user pass below can
    # split a user's tokens the way the day's tokens actually fell across models.
    day_model_tokens: Dict[Tuple[str, str], Dict[Optional[str], Dict[str, int]]] = {}
    # (measured, turns) per agent-day, so the per-user rows below can carry the
    # same measured count the agent row does.
    day_measured: Dict[Tuple[str, str], Tuple[int, int]] = {}
    for (rid, day) in sorted(set(by_agent_day) | set(au_by_agent_day)):
        times = by_agent_day.get((rid, day), [])
        turns = max(len(times), au_by_agent_day.get((rid, day), 0))
        shard = month_shard(day)
        pk, sk = f"AGENTS#{shard}", f"D#{day}#A#{rid}"
        item = usage_repo.get(pk, sk) or {}
        fields: Dict[str, Any] = {
            "turns": turns,
            "measured_turns": min(turns, max(_measured(item, turns), au_measured_sum.get((rid, day), 0))),
        }

        models: Dict[Optional[str], int] = {}
        for moment in times:
            model_id = model_at(timelines.get(rid, []), moment)
            models[model_id] = models.get(model_id, 0) + 1
        if not times:
            models[model_at(timelines.get(rid, []), f"{day}T12:00:00+00:00")] = 0
        ordered = sorted(models.items(), key=lambda pair: (pair[0] is None, str(pair[0])))
        # Turns known only from the granular ledger have no time; they go to the
        # model that served most of the day (first after sorting by count).
        extra = turns - len(times)
        if extra > 0:
            top = max(range(len(ordered)), key=lambda i: ordered[i][1])
            ordered[top] = (ordered[top][0], ordered[top][1] + extra)
        ordered = [(m, c) for m, c in ordered if c > 0] or [(None, turns)]

        tokens = _tokens_of(item)
        # What the events already know, per model they ran on.
        exact: Dict[Optional[str], Dict[str, int]] = {}
        for ev in events_by_agent_day.get((rid, day), []):
            bucket = exact.setdefault(str(ev["model_id"]), {**{tier: 0 for tier in _TOKEN_KEYS}, "turns": 0})
            for tier in _TOKEN_KEYS:
                bucket[tier] += int(ev.get(tier) or 0)
            bucket["turns"] += 1
        exact_turns = sum(bucket["turns"] for bucket in exact.values())
        residual = {
            tier: max(0, tokens[tier] - sum(bucket[tier] for bucket in exact.values()))
            for tier in _TOKEN_KEYS
        }
        # The rest of the day — turns and tokens no event covers — by the timeline.
        timeline_turns = max(0, turns - exact_turns)
        weights = [count for _, count in ordered]
        timeline_models = [
            (model_id, count)
            for (model_id, _), count in zip(ordered, _split_tokens(timeline_turns, weights))
            if count > 0
        ]
        if not timeline_models and any(residual.values()):
            timeline_models = [(ordered[0][0], 0)]
        approx = len([m for m, _ in timeline_models if m]) > 1
        per_model: Dict[Optional[str], Dict[str, int]] = {}
        tl_weights = [count for _, count in timeline_models] or [1]
        for index, (model_id, count) in enumerate(timeline_models):
            bucket = per_model.setdefault(model_id, {**{tier: 0 for tier in _TOKEN_KEYS}, "turns": 0, "exact_turns": 0})
            bucket["turns"] += count
            for tier in _TOKEN_KEYS:
                bucket[tier] += _split_tokens(residual[tier], tl_weights)[index]
        for model_id, known in exact.items():
            bucket = per_model.setdefault(model_id, {**{tier: 0 for tier in _TOKEN_KEYS}, "turns": 0, "exact_turns": 0})
            bucket["turns"] += known["turns"]
            bucket["exact_turns"] += known["turns"]
            for tier in _TOKEN_KEYS:
                bucket[tier] += known[tier]
        ordered = sorted(
            ((m, b["turns"]) for m, b in per_model.items()),
            key=lambda pair: (pair[0] is None, str(pair[0])),
        )
        day_models[(rid, day)] = ordered
        day_model_tokens[(rid, day)] = {m: {tier: b[tier] for tier in _TOKEN_KEYS} for m, b in per_model.items()}
        day_measured[(rid, day)] = (fields["measured_turns"], turns)

        cost_total = 0
        priced_turns = 0
        unpriced_turns = 0
        any_cost = False
        timeline_measured = max(0, fields["measured_turns"] - exact_turns)
        for model_id, bucket in per_model.items():
            rate = resolve_rate(model_id, day)
            count = bucket["turns"]
            model_fields: Dict[str, Any] = {
                "turns": count,
                # Every event turn reported tokens; the timeline share takes what
                # is left of the day's measured count.
                "measured_turns": bucket["exact_turns"] + min(count - bucket["exact_turns"], timeline_measured),
                **{tier: bucket[tier] for tier in _TOKEN_KEYS},
            }
            if approx and count > bucket["exact_turns"]:
                model_fields["split"] = "approx"
            micros = cost_micros({tier: bucket[tier] for tier in _TOKEN_KEYS}, rate) if rate else None
            if micros is not None:
                model_fields["model_cost_micros"] = micros
                model_fields["priced_turns"] = count
                model_fields["unpriced_turns"] = 0
                cost_total += micros
                priced_turns += count
                any_cost = True
            else:
                model_fields["unpriced_turns"] = count
                model_fields["priced_turns"] = 0
                unpriced_turns += count
            if model_id:
                writer.set(f"AGENT_MODELS#{shard}", f"D#{day}#A#{rid}#M#{model_id}", model_fields)
        if any_cost:
            fields["model_cost_micros"] = cost_total
        fields["priced_turns"] = priced_turns
        fields["unpriced_turns"] = unpriced_turns
        if approx:
            fields["split"] = "approx"
            report["approx_split_days"] += 1
        if unpriced_turns and not any_cost:
            report["unpriced_agent_days"] += 1
        writer.set(pk, sk, fields)
        report["agent_days_corrected"] += 1

    # 4. Per-(agent, user, day) items: the evidence figure, priced from that item's
    # own tokens at the day's model split; then per-(user, day) as the roll-up.
    # A user row's measured count is the agent row's, shared out. The agent-day
    # item was stamped by the timeline pass (its tokens are known for the turns the
    # timeline matched), the user items only by what the live stream reported —
    # so on 2026-09-20 the agent rows read 42 of 43 turns measured and the one
    # user's row read 12, and the user table showed "31턴 미측정" against tokens
    # that were priced in full one widget above. The agent-day's surplus over what
    # its user rows already carry is split by each row's room (turns − measured),
    # never above a row's own turns; a single-user day is simply the agent's count.
    au_measured: Dict[Tuple[str, str, str], int] = {}
    by_agent_day_users: Dict[Tuple[str, str], List[Tuple[str, int, int]]] = {}
    for (rid, sub, day), turns in au_turns.items():
        item = agent_user_items.get((rid, sub, day)) or usage_repo.get(
            f"AGENT#{rid}#USERS#{month_shard(day)}", f"D#{day}#U#{sub}") or {}
        by_agent_day_users.setdefault((rid, day), []).append((sub, turns, _measured(item, turns)))
    for (rid, day), users in by_agent_day_users.items():
        agent_measured, _ = day_measured.get((rid, day), (0, 0))
        surplus = agent_measured - sum(measured for _, _, measured in users)
        room = [turns - measured for _, turns, measured in users]
        extra = _split_tokens(surplus, room) if surplus > 0 and any(room) else [0] * len(users)
        for (sub, turns, measured), more in zip(users, extra):
            au_measured[(rid, sub, day)] = min(turns, measured + max(0, more))

    user_cost: Dict[Tuple[str, str], Dict[str, int]] = {}
    user_measured: Dict[Tuple[str, str], int] = {}
    for (rid, sub, day), turns in sorted(au_turns.items()):
        shard = month_shard(day)
        pk, sk = f"AGENT#{rid}#USERS#{shard}", f"D#{day}#U#{sub}"
        item = agent_user_items.get((rid, sub, day)) or usage_repo.get(pk, sk) or {}
        fields = {"turns": turns, "measured_turns": au_measured.get((rid, sub, day), _measured(item, turns))}
        user_measured[(sub, day)] = user_measured.get((sub, day), 0) + fields["measured_turns"]
        ordered = day_models.get((rid, day), [(None, turns)])
        weights = [count for _, count in ordered]
        tokens = _tokens_of(item)
        # A user's tokens are split across the day's models by each model's share
        # of the agent-day's tokens of that tier — not by turn count. One Opus 5.5
        # override turn among ten Sonnet turns held 2% of the day's tokens; split
        # by turns it took 9% of them at the Opus rate, and the agent's only user
        # was billed $6.4568 against the agent's own $6.0443 (live, 2026-09-23).
        # Turns are still shared by count: a turn is a turn. Token weights fall
        # back to turn weights only when the agent-day has no tokens of a tier.
        model_tokens = day_model_tokens.get((rid, day), {})
        shares: Dict[str, List[int]] = {}
        for tier in _TOKEN_KEYS:
            tier_weights = [int(model_tokens.get(model_id, {}).get(tier) or 0) for model_id, _ in ordered]
            shares[tier] = _split_tokens(tokens[tier], tier_weights if any(tier_weights) else weights)
        cost_total = 0
        priced = 0
        unpriced = 0
        any_cost = False
        for index, (model_id, count) in enumerate(ordered):
            rate = resolve_rate(model_id, day)
            share_tokens = {tier: shares[tier][index] for tier in _TOKEN_KEYS}
            share_turns = _split_tokens(turns, weights)[index]
            share_cost = cost_micros(share_tokens, rate) if rate else None
            if share_cost is not None:
                cost_total += share_cost
                priced += share_turns
                any_cost = True
            else:
                unpriced += share_turns
        if any_cost:
            fields["model_cost_micros"] = cost_total
        fields["priced_turns"] = priced
        fields["unpriced_turns"] = unpriced
        writer.set(pk, sk, fields)
        bucket = user_cost.setdefault((sub, day), {"model_cost_micros": 0, "priced_turns": 0, "unpriced_turns": 0, "any_cost": 0})
        bucket["model_cost_micros"] += cost_total
        bucket["priced_turns"] += priced
        bucket["unpriced_turns"] += unpriced
        bucket["any_cost"] += 1 if any_cost else 0

    for (sub, day) in sorted(set(by_user_day) | set(au_by_user_day)):
        turns = max(by_user_day.get((sub, day), 0), au_by_user_day.get((sub, day), 0))
        shard = month_shard(day)
        pk, sk = f"USERS#{shard}", f"D#{day}#U#{sub}"
        item = usage_repo.get(pk, sk) or {}
        # The roll-up carries the sum of its (agent, user) rows — never less than
        # what it already held, never more than its turns.
        fields = {"turns": turns, "measured_turns": min(turns, max(_measured(item, turns), user_measured.get((sub, day), 0)))}
        cost = user_cost.get((sub, day))
        if cost:
            if cost["any_cost"]:
                fields["model_cost_micros"] = cost["model_cost_micros"]
            fields["priced_turns"] = cost["priced_turns"]
            fields["unpriced_turns"] = cost["unpriced_turns"]
        writer.set(pk, sk, fields)

    # Verification: the three ledgers must now agree on turns per day.
    if apply:
        agents_total = sum(int((usage_repo.get(f"AGENTS#{month_shard(d)}", f"D#{d}#A#{r}") or {}).get("turns") or 0)
                           for (r, d) in set(by_agent_day) | set(au_by_agent_day))
        users_total = sum(int((usage_repo.get(f"USERS#{month_shard(d)}", f"D#{d}#U#{u}") or {}).get("turns") or 0)
                          for (u, d) in set(by_user_day) | set(au_by_user_day))
        au_total = sum(au_turns.values())
        report["verification"] = {
            "agent_turns": agents_total, "user_turns": users_total, "agent_user_turns": au_total,
            "thread_turns": sum(len(v) for v in by_agent_day.values()),
            "agree": agents_total == users_total == au_total,
        }

    report["writes"] = {"puts": writer.puts, "sets": writer.sets, "applied": apply}
    return report


def _migrate_turn_keys(usage_repo: Any, writer: "_Writer") -> int:
    """Fold `T#{ended_at}#{turn_id}` rows into `T#{turn_id}`. Returns rows removed."""
    from data.model_rates import cost_micros as _cost, resolve_rate as _rate

    removed = 0
    grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for item in _scan_prefix(usage_repo, "TURNS#", sk_prefix="T#"):
        turn_id = str(item.get("turn_id") or "")
        sk = str(item.get("sk", ""))
        if not turn_id or sk == f"T#{turn_id}":
            continue
        grouped.setdefault((str(item.get("pk")), turn_id), []).append(item)
    for (pk, turn_id), rows in sorted(grouped.items()):
        rows.sort(key=lambda r: str(r.get("ended_at", "")))
        existing = usage_repo.get(pk, f"T#{turn_id}")
        base = dict(existing) if existing else {}
        merged: Dict[str, Any] = {k: v for k, v in (base or rows[0]).items() if k not in ("pk", "sk")}
        for tier in _TOKEN_KEYS:
            merged[tier] = int(base.get(tier) or 0) + sum(int(r.get(tier) or 0) for r in rows)
        merged["model_calls"] = int(base.get("model_calls") or 0) + sum(int(r.get("model_calls") or 0) for r in rows)
        merged["measured"] = bool(base.get("measured")) or any(bool(r.get("measured")) for r in rows) or any(merged[t] for t in _TOKEN_KEYS)
        merged["ended_at"] = max([str(base.get("ended_at") or "")] + [str(r.get("ended_at") or "") for r in rows])
        merged["status"] = rows[-1].get("status") or merged.get("status")
        for key in ("turn_id", "thread_id", "agent_record_id", "owner_sub", "model_id", "routing", "rate_card_version", "started_at", "source", "tool_calls"):
            if key not in merged or merged.get(key) in (None, "", {}):
                for r in rows:
                    if r.get(key) not in (None, "", {}):
                        merged[key] = r[key]
                        break
        rate = _rate(merged.get("model_id"))
        if rate:
            merged["model_cost_micros"] = _cost(merged, rate)
            merged["routing"] = rate["routing"]
            merged["rate_card_version"] = rate["version"]
        else:
            merged.pop("model_cost_micros", None)
        merged = {k: v for k, v in merged.items() if v is not None}
        if existing:
            writer.set(pk, f"T#{turn_id}", merged)
        else:
            writer.put(pk, f"T#{turn_id}", merged)
        for r in rows:
            if writer.apply:
                usage_repo.delete(pk, str(r.get("sk")))
            removed += 1
    return removed


def _reprice_unresolved_events(usage_repo: Any, writer: "_Writer", timelines: Dict[str, List[Tuple[str, str]]], today: str) -> int:
    """Stream events written without a model (a throttled lookup at turn time) get
    the model the version history says ran, their cost, and their day counters
    moved from unpriced to priced. Measured events only — no tokens, no figure."""
    repriced = 0
    for item in _scan_prefix(usage_repo, "TURNS#", sk_prefix="T#"):
        if item.get("source") != "stream" or item.get("model_id") or not item.get("measured"):
            continue
        rid = str(item.get("agent_record_id") or "")
        when = str(item.get("started_at") or item.get("ended_at") or "")
        moment = _parse(when)
        if not rid or moment is None:
            continue
        day = clock.date_of(moment)
        if day >= today:
            continue
        model_id = model_at(timelines.get(rid, []), when)
        rate = resolve_rate(model_id, day)
        if not model_id or not rate:
            continue
        tokens = _tokens_of(item)
        cost = cost_micros(tokens, rate)
        pk, sk = str(item.get("pk")), str(item.get("sk"))
        writer.set(pk, sk, {"model_id": model_id, "routing": rate["routing"],
                            "rate_card_version": rate["version"], "model_cost_micros": cost})
        shard = month_shard(day)
        deltas = {"model_cost_micros": cost, "priced_turns": 1, "unpriced_turns": -1}
        targets = [(f"AGENTS#{shard}", f"D#{day}#A#{rid}", deltas)]
        sub = str(item.get("owner_sub") or "")
        if sub:
            targets.append((f"USERS#{shard}", f"D#{day}#U#{sub}", deltas))
            targets.append((f"AGENT#{rid}#USERS#{shard}", f"D#{day}#U#{sub}", deltas))
        targets.append((f"AGENT_MODELS#{shard}", f"D#{day}#A#{rid}#M#{model_id}",
                        {**tokens, "turns": 1, "measured_turns": 1, "model_cost_micros": cost, "priced_turns": 1}))
        for tpk, tsk, counters in targets:
            writer.add(tpk, tsk, counters)
        repriced += 1
    return repriced


def _agent_user_items(usage_repo: Any, today: str) -> Dict[Tuple[str, str, str], Dict[str, Any]]:
    """Every `AGENT#{rid}#USERS#` item before today, keyed (rid, sub, day).

    The agents are discovered from the `AGENTS#` partitions (every month with an
    item), so a retired agent's legacy days are covered too.
    """
    out: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    shards: set = set()
    records: set = set()
    for item in _scan_prefix(usage_repo, "AGENTS#"):
        sk = str(item.get("sk", ""))
        day = sk[2:12]
        if day >= today or "#A#" not in sk:
            continue
        shards.add(month_shard(day))
        records.add(sk.split("#A#", 1)[1])
    for rid in sorted(records):
        for shard in sorted(shards):
            try:
                items = usage_repo.query_prefix(f"AGENT#{rid}#USERS#{shard}", "D#")
            except Exception:
                continue
            for item in items:
                sk = str(item.get("sk", ""))
                day = sk[2:12]
                if day >= today or "#U#" not in sk:
                    continue
                out[(rid, sk.split("#U#", 1)[1], day)] = item
    return out


def _scan_prefix(usage_repo: Any, prefix: str, sk_prefix: str = "D#") -> List[Dict[str, Any]]:
    """Items of every month partition with the given prefix (last 13 months)."""
    from datetime import date

    items: List[Dict[str, Any]] = []
    year, month = date.today().year, date.today().month
    for _ in range(13):
        try:
            items.extend(usage_repo.query_prefix(f"{prefix}{year:04d}-{month:02d}", sk_prefix))
        except Exception:
            pass
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return items


# --- CLI ------------------------------------------------------------------------------


def _dates(start: str, end: str) -> List[str]:
    return clock.dates(start, end)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", required=True, help="First business date to rebuild (YYYY-MM-DD).")
    parser.add_argument("--end", required=True, help="Last business date to rebuild; must be before today.")
    parser.add_argument("--apply", action="store_true", help="Write. Without it the report is printed and nothing changes.")
    parser.add_argument("--skip-resources", action="store_true", help="Do not rebuild RESOURCES#/GATEWAYS#/MEMORIES# from CloudWatch.")
    parser.add_argument("--skip-recon", action="store_true", help="Do not snapshot Cost Explorer into RECON#.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    import boto3

    from core.config import AWS_REGION, DYNAMODB_THREADS_TABLE, USAGE_TABLE
    from core.dependencies import billing_service, collector_service, pricing_service
    from repositories.thread_repository import ThreadRepository
    from repositories.usage_repository import UsageRepository
    from services.harness_service import HarnessService
    from services.registry_service import RegistryService

    if not USAGE_TABLE:
        raise SystemExit("USAGE_TABLE is not set")
    today = clock.today()
    if args.end >= today:
        raise SystemExit(f"--end must be before today ({today}); the stream owns today")

    usage_repo = UsageRepository(table_name=USAGE_TABLE, region_name=AWS_REGION)
    # Price with the card as it stands *including* the dated overlay — rates the
    # bill taught and rates an admin registered. Without this a backfilled day
    # on a family the committed table lacks stays unpriced no matter what the
    # page says its rate is, and re-running the backfill is how those days get
    # priced (a backfilled event has no tokens of its own for `reprice_events`).
    from services.usage_service import UsageService
    UsageService(repository=usage_repo).load_learned_rates()
    thread_repo = ThreadRepository(table_name=DYNAMODB_THREADS_TABLE, region_name=AWS_REGION)
    registry = RegistryService()
    harness = HarnessService(registry=registry)
    control = boto3.client("bedrock-agentcore-control", region_name=AWS_REGION)

    threads: List[Any] = []
    response = thread_repo.table.scan()
    while True:
        threads.extend(thread_repo._to_thread(item) for item in response.get("Items", []))
        key = response.get("LastEvaluatedKey")
        if not key:
            break
        response = thread_repo.table.scan(ExclusiveStartKey=key)

    records = list(registry.agent_records())
    timelines = model_timelines(control, records)
    for record in records:
        logger.info("model timeline %s (%s): %s", record.name, record.record_id, timelines.get(record.record_id))

    report = run(usage_repo, threads, timelines, today=today, apply=args.apply)

    if not args.skip_resources and collector_service is not None:
        arns = set(collector_service.runtime_arns())
        # Runtimes that only survive as log groups (deleted agents) keep their days.
        logs = boto3.client("logs", region_name=AWS_REGION)
        account = boto3.client("sts", region_name=AWS_REGION).get_caller_identity()["Account"]
        paginator = logs.get_paginator("describe_log_groups")
        for page in paginator.paginate(logGroupNamePrefix="/aws/bedrock-agentcore/runtimes/"):
            for group in page.get("logGroups", []):
                name = group["logGroupName"].rsplit("/", 1)[-1]
                runtime_id = name[: -len("-DEFAULT")] if name.endswith("-DEFAULT") else name
                if runtime_id.startswith(("bap_", "harness_")):
                    arns.add(f"arn:aws:bedrock-agentcore:{AWS_REGION}:{account}:runtime/{runtime_id}")
        gateways = collector_service.gateway_arns()
        memories = collector_service.memory_arns()
        written = {"runtime": 0, "gateway": 0, "memory": 0}
        if args.apply:
            for day in _dates(args.start, args.end):
                written["runtime"] += collector_service.collect_day(day, sorted(arns), today=today)["written"]
                written["gateway"] += collector_service.collect_gateways_day(day, gateways, today=today)["written"]
                written["memory"] += collector_service.collect_memories_day(day, memories, today=today)["written"]
        report["resources"] = {"runtimes": len(arns), "gateways": len(gateways), "memories": len(memories), **written}

    if not args.skip_recon and collector_service is not None and args.apply:
        collector_service.billing = billing_service
        recon = collector_service.billing.reconcile(
            args.start, args.end, repository=usage_repo,
            ledger_agent_days=collector_service.ledger_inputs(args.start, args.end)[0],
            ledger_components=collector_service.ledger_inputs(args.start, args.end)[1],
        )
        bad = [
            row for row in recon["agent_runtime"]
            if row.get("diff_micros") is not None and row.get("ours_micros")
            and abs(row["diff_micros"]) > 0.001 * int(row["ours_micros"])
        ]
        report["recon"] = {
            "agent_days": len(recon["agent_runtime"]),
            "agent_days_over_0_1pct": len(bad),
            "rate_mismatches": [(m["family"], m["routing"], m["tier"]) for m in recon["mismatches"]],
            "unregistered_families": recon["unregistered_families"],
        }
        for row in bad[:20]:
            logger.warning("recon gap %s %s billed=%s ours=%s", row["agent"], row["day"], row["billed_micros"], row["ours_micros"])

    # Turn events cover only threads that still exist, so they are reported
    # beside the ledgers rather than compared with them.
    from services.usage_service import UsageService
    usage = UsageService(repository=usage_repo)
    report.setdefault("verification", {})["turn_events"] = len(usage.turn_events(args.start, args.end))

    print("\n=== backfill report ===")
    for key, value in report.items():
        print(f"{key:28s} {value}")
    if args.apply and not report.get("verification", {}).get("agree", True):
        raise SystemExit(1)
    if args.apply and report.get("recon", {}).get("agent_days_over_0_1pct"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
