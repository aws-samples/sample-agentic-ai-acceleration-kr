"""Insights routes.

**Every handler is `sync def`.** They make blocking boto3 calls, and an
`async def` route holding a socket read stalls every request in the process, not
just its own — FastAPI's threadpool exists precisely to absorb this.

**The sources meet here and nowhere else.** `usage_service` never imports
`telemetry_service`: they fail independently, so a CloudWatch outage collapses
one panel while our own exact, attributable figures still render.

**`/summary` never touches CloudWatch, and that is a price decision.**
`GetMetricData` bills $0.01 per 1,000 metrics requested, and the dashboard polls
itself once a minute. Measured on the live account 2026-08-16, one sweep asked
for 279 metrics — $0.0028 a request, $4.02 a day for a single tab left open,
multiplied by every tab because nothing cached the *result*, only the discovery
call in front of it. So the vended tier now lives behind `GET /telemetry`, which
a reader asks for, and the default page costs nothing beyond the usage table and
a six-hour-cached Cost Explorer read.

**Figures we cannot stand behind are not served at all.** `ActiveSessionCount`
and Memory's token usage were removed rather than relabelled: both are
dimensioned per *service*, so in a shared account they count other tenants'
sessions and other tenants' extraction tokens, and the KPI tiles presented both
as ours.

**Model cost is back, and the condition it had to meet was a citable rate.** It
was deleted because the only per-million figures available for the models this
platform runs were Anthropic's list prices transcribed by hand — the Price List
API publishes token rates for Nova and for Claude 3 and earlier and nothing at
all for these models, which is still true (measured 2026-08-17). What it does not
publish, the account's own bill does: `UnblendedCost / UsageQuantity` on a Cost
Explorer token line *is* the rate that was charged, per model and per tier, and it
divides to exact round numbers. Paired with a token count that now separates the
four tiers Bedrock bills at four different rates, that is a figure with a source.
`billing_service.model_rates` reads it; `usage_service.estimate_model_cost`
refuses rather than guessing when any tier is unpriced.

**Everything the Price List API does publish is now read from it.** One
`GetProducts` call returns all 25 AgentCore consumption rates for a region, so the
runtime estimate no longer multiplies two transcribed constants and the billed
panel can print the published rate beside the line item it prices.

**The metered budget on this page, per day, per window.** `/summary` makes three
Cost Explorer reads — cost by usage type, model rates, per-agent tags — each $0.01
and each cached six hours: 4 refreshes × 3 reads × 2 windows = $0.24 a day no
matter how many tabs are open. A *page* of a read is billed like a read, and the
rate query now groups by region as well as by usage type, so a large enough bill
raises that figure by whatever it paginates into. `/telemetry` is the only
CloudWatch read and it is still click-only. The Price List API is free. DynamoDB is
`(2 + agents)` keyed queries per `/summary`, which at on-demand read pricing is
noise — under a cent a day for a tab left open.

**Every figure states what it could not read.** `sources.usage` used to be a
literal `True`: a month partition that failed left the totals short by whatever it
held, with nothing on the page to show the hole, while every *other* source on the
page degraded to a dash or a collapsed card. It now reports
`usage_service.reads_complete()`, and the cost figures carry `cost_is_floor` and
`rate_scope` for the same reason — a qualifier that does not reach the tile the
reader looks at is not a qualifier.
"""
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from core import clock
from core.auth import AuthUser, current_user
from core.dependencies import (
    billing_service,
    pricing_service,
    telemetry_service,
    trace_service,
    thread_service,
    usage_service,
    evaluation_service,
    layout_service,
    collector_service,
    directory_service,
    rate_card_service,
)
from services.billing_service import BillingUnavailable
from services.collector_service import CollectorService
from services.evaluation_service import EvaluationsUnavailable
from services.pricing_service import PricingUnavailable
from services.rate_card_service import RateCardError
from services.telemetry_service import TelemetryUnavailable
from services.trace_service import TracesUnavailable
from services.usage_service import guardrail_sum
from data.model_rates import RATE_CARD_VERSION, resolve_rate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/insights", tags=["insights"])


class StartEvaluationRequest(BaseModel):
    """Start a batch evaluation over threads."""
    thread_ids: List[str]
    evaluator_ids: List[str]
    record_id: Optional[str] = None


class StartAnalysisRequest(BaseModel):
    """Start an AgentCore insights (triage) run over threads.

    `insight_ids` defaults to all three built-in insight types on the service side;
    the client passes nothing unless it wants fewer.
    """
    thread_ids: List[str]
    record_id: Optional[str] = None
    insight_ids: Optional[List[str]] = None


class RateEntryRequest(BaseModel):
    """One dated rate an admin registers. Validated in the service, not here, so
    the error names the field in the admin's language instead of a 422 body."""
    family: str
    routing: str
    tier: str
    usd_per_1m: str
    effective_from: str
    # `bill` / `price-list` when accepting a candidate; anything else is recorded
    # as the admin's own entry.
    source: Optional[str] = None


class RateRegisterRequest(BaseModel):
    entries: List[RateEntryRequest]


class LayoutUpdateRequest(BaseModel):
    """Update the dashboard layout.

    Both fields are optional so a partial body reconciles instead of 422-ing.
    Use Any for widgets so any value (valid or junk) passes to reconciliation.
    """
    version: Optional[int] = None
    widgets: Optional[Any] = None

    class Config:
        # Allow arbitrary types and don't validate extras
        extra = "ignore"

# The two windows the UI offers. Anything else is clamped rather than rejected:
# a hand-edited query string should show a sensible page, not a 422.
SUPPORTED_DAYS = (7, 30)

# The thread listing is a filtered Scan sorted in memory, so the cost is in the
# table size rather than the limit — but the drill-down is a list somebody reads,
# and a hand-edited `?limit=5000` should clamp rather than 422.
MAX_RECORD_THREADS = 100
# Enough to read the pattern; the counters carry the total.
RECENT_GUARDRAIL_EVENTS = 20

# Test seams. Production leaves these None and the dependency instances are used.
_usage_override = None
_telemetry_override = None
_threads_override = None
_traces_override = None
_billing_override = None
_pricing_override = None
_evaluations_override = None
_layout_override = None
_collector_override = None
_directory_override = None
_rate_card_override = None


def _usage():
    return _usage_override or usage_service


def _telemetry():
    return _telemetry_override or telemetry_service


def _pricing():
    return _pricing_override or pricing_service


def _threads():
    return _threads_override or thread_service


def _traces():
    return _traces_override or trace_service


def _billing():
    return _billing_override or billing_service


def _evaluations():
    return _evaluations_override or evaluation_service


def _layout():
    return _layout_override or layout_service


def _collector():
    return _collector_override or collector_service


def _directory():
    return _directory_override or directory_service


def _rate_card():
    return _rate_card_override or rate_card_service


def _subjects(subs) -> Dict[str, str]:
    """sub -> email for the people a response names, or `{}` when the directory
    cannot say. Names are decoration on a usage page: the client falls back to
    the shortened sub, so a directory failure costs the labels, never the rows.
    Only admin-facing payloads call this — it is the same line `/users` draws."""
    try:
        return dict(_directory().emails(subs))
    except Exception:
        logger.info("Subject directory unavailable", exc_info=True)
        return {}


def _window(days: int) -> Dict[str, Any]:
    """The window, in the calendar the counters are bucketed in.

    Delegated to `core.clock` rather than computed from `utcnow()` here, because
    the day boundary is a property of the whole feature: it decides which bucket a
    turn is written into *and* which days this window asks for, and those two have
    to be the same calendar or the newest day is always short.

    Also carries `partial_day`. The window ends now, so its last day is still being
    written — a fact every consumer that subtracts or draws the last point needs,
    and none of them had. The half-window delta was comparing a fraction of today
    against whole days and reporting the shortfall as a decline in usage.
    """
    days = days if days in SUPPORTED_DAYS else max(SUPPORTED_DAYS)
    return clock.window(days)


def _window_fields(window: Dict[str, Any]) -> Dict[str, Any]:
    """The window descriptor every response repeats, including its calendar.

    `timezone` travels with the dates because a date axis whose timezone is
    unstated is one the reader assumes is theirs — and with the default UTC on a
    Korean deployment, 09:00 local is where the day breaks.
    """
    return {
        "days": window["days"],
        "start_date": window["start_date"],
        "end_date": window["end_date"],
        "timezone": window["timezone"],
        "partial_day": window["partial_day"],
    }


def _trace_window(thread: Any) -> tuple:
    """The thread's lifetime, padded a minute either side.

    Span timestamps and this platform's own are written by different clocks, so a
    window pinned exactly to the thread's `created_at`/`updated_at` can exclude
    the very spans it is looking for — and Logs Insights answers that with an
    empty result set rather than an error.
    """
    pad = timedelta(minutes=1)
    created = getattr(thread, "created_at", None) or ""
    updated = getattr(thread, "updated_at", None) or created
    try:
        start = datetime.fromisoformat(created[:19]) - pad
        end = datetime.fromisoformat(updated[:19]) + pad
    except ValueError:
        end = datetime.utcnow()
        start = end - timedelta(hours=1)
    return start, end


def _record_usage_view(usage: Any, record_id: str, window: Dict[str, Any]) -> tuple:
    """The Usage tab's two record-kind-dependent pieces: `(reach, tools)`.

    An agent's tool calls are keyed to it directly. A gateway record has none of
    its own — it is called *through*, not run — so `mcp_gateway_usage` rolls the
    agents' calls back to it by target and returns the attaching agents too. That
    method is `None` for anything that is not a gateway, which is the signal to
    fall back to the record's own counters and show no `reach`.
    """
    gateway = usage.mcp_gateway_usage(
        record_id, window["start_date"], window["end_date"]
    )
    if gateway is not None:
        reach = {"agents": gateway["agents"], "turns": gateway["turns"]}
        return reach, gateway["tools"]
    tools = usage.tool_totals(record_id, window["start_date"], window["end_date"])
    return None, tools


def _require_admin(user: AuthUser) -> None:
    """The org-wide aggregates name every agent's usage and spend, so the routes
    that serve them are admin-only.

    Checked inline against the passed user rather than via `Depends(require_admin)`
    so the sync handlers stay callable — and unit-testable — by direct call, the
    same way `/users` and `/me` guard themselves. Authorisation before availability:
    a plain user gets 403 whether or not this environment records usage, because the
    501 would otherwise leak that the feature exists and what it needs.

    Only `/summary`, `/telemetry` and `/composition` carry this. The per-record and
    evaluation routes are shared with the Registry page — which is not admin-only —
    so gating them here would break a page a plain user is entitled to.
    """
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다.")


def _require_configured() -> None:
    if not _usage().configured:
        # 501, not 503: the request is fine and auth succeeded — the usage table
        # simply is not provisioned in this environment. Same shape as knowledge.
        raise HTTPException(
            status_code=501,
            detail="USAGE_TABLE is not configured on the server, so usage is "
            "not being recorded and there is nothing to report.",
        )


def _agent_arns(records: List[Any]) -> Dict[str, List[str]]:
    """Record id -> the ARNs CloudWatch might know it by.

    Both are tried because a harness-backed record carries its harness ARN and
    its companion runtime ARN, and the vended series are dimensioned on either.
    """
    arns: Dict[str, List[str]] = {}
    for record in records:
        candidates = [
            arn
            for arn in (
                getattr(record, "harness_arn", None),
                getattr(record, "agent_runtime_arn", None),
            )
            if arn
        ]
        if candidates:
            arns[record.record_id] = candidates
    return arns


def _runtime_arn_for_threads(threads: List[Any]) -> str:
    """The one runtime ARN a batch evaluation can be scoped to.

    AWS caps `serviceNames` at a single entry (measured 2026-08-16), so a batch
    covers exactly one agent. The threads must therefore agree on their agent, and
    the agent's runtime ARN comes from its registry record — harness-backed records
    carry it too, because `harness_service` writes `agent_runtime_arn` from the
    companion runtime when it registers.
    """
    record_ids = {getattr(thread, "agent_record_id", None) for thread in threads}
    record_ids.discard(None)
    if len(record_ids) != 1:
        raise HTTPException(
            status_code=422,
            detail=(
                "한 번의 평가는 에이전트 하나만 다룰 수 있습니다 "
                f"(스레드가 가리키는 에이전트: {len(record_ids)}개)."
            ),
        )
    record_id = record_ids.pop()

    try:
        records = _usage().registry.agent_records()
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail=f"레지스트리를 읽을 수 없습니다: {exc}"
        ) from exc

    for record in records:
        if record.record_id != record_id:
            continue
        runtime_arn = getattr(record, "agent_runtime_arn", None)
        if runtime_arn:
            return runtime_arn
        raise HTTPException(
            status_code=422,
            detail=(
                "이 에이전트의 레지스트리 레코드에 런타임 ARN 이 없어 평가 대상을 "
                "특정할 수 없습니다."
            ),
        )

    raise HTTPException(
        status_code=422,
        detail="이 스레드의 에이전트가 레지스트리에 없습니다.",
    )


def _status_value(status: Any) -> Optional[str]:
    """A thread status as a string, whether it arrives as an enum or already flat."""
    if status is None:
        return None
    return getattr(status, "value", status)


def _turn_count(thread: Any) -> int:
    """Assistant messages in the thread — its turns.

    Counted from the stored messages because the usage table cannot answer it: its
    counters are keyed by date and agent, so "how many turns in *this* thread" has
    no partition to read. Absent messages are 0 turns, not unknown — a thread row
    exists only once it has been created, and an empty one genuinely had none.
    """
    values = getattr(thread, "values", None) or {}
    messages = values.get("messages") or []
    return sum(1 for message in messages if message.get("type") == "ai")


def _thread_title(thread: Any) -> str:
    """The thread's opening question, trimmed to one line — what a reader knows it by.

    The drill-down led every row with "저장된 답변 N", which names the thread's
    *shape*, not the thread: two conversations with three answers each are one row
    repeated. The first human turn is the question that opened it, and it is the
    same field the sidebar titles a thread by (`useThreads`), so the two agree.

    Empty when the opening turn carries no readable text — an attachment-only
    prompt, or history written before messages were stored — and the client falls
    back to the id. Bounded here rather than trusting the client so a pathological
    first message does not ship the whole transcript into the list payload.
    """
    values = getattr(thread, "values", None) or {}
    messages = values.get("messages") or []
    for message in messages:
        if not isinstance(message, dict) or message.get("type") != "human":
            continue
        content = message.get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list) and content:
            first = content[0]
            if isinstance(first, dict):
                text = first.get("text") or ""
        # Only the first non-empty line, so a multi-line prompt does not smuggle
        # newlines into a single-row label.
        line = text.strip().splitlines()[0].strip() if text.strip() else ""
        return line[:120]
    return ""


def _int(value: Any) -> Optional[int]:
    """DynamoDB hands numbers back as Decimal; the API speaks integer micros."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _runtime_arns_of(record: Any, harness_runtimes: Dict[str, str]) -> List[str]:
    """The runtime ARNs a record's cost lives under.

    A harness-backed record carries its companion runtime as `agent_runtime_arn`
    when the registration wrote it, and the harness listing knows it otherwise.
    """
    arns: List[str] = []
    runtime = getattr(record, "agent_runtime_arn", None)
    if runtime:
        arns.append(runtime)
    harness = getattr(record, "harness_arn", None)
    companion = harness_runtimes.get(harness) if harness else None
    if companion and companion not in arns:
        arns.append(companion)
    return arns


def _harness_runtimes(usage: Any) -> Dict[str, str]:
    """harness ARN -> companion runtime ARN, or {} when the listing fails."""
    try:
        return {
            harness.harness_arn: harness.runtime_arn
            for harness in usage.harness.list_harnesses()
            if getattr(harness, "harness_arn", None) and getattr(harness, "runtime_arn", None)
        }
    except Exception:
        logger.info("Harness listing unavailable for runtime mapping", exc_info=True)
        return {}


def _collected(window: Dict[str, Any]) -> Dict[str, Any]:
    """Everything the collector wrote for the window, or empty maps when it is
    not configured. Reads DDB only."""
    collector = _collector()
    empty = {"resources": {}, "gateways": {}, "memories": {}, "sessions": {}}
    if collector is None:
        return empty
    out = dict(empty)
    for key in empty:
        try:
            out[key] = getattr(collector, key)(window["start_date"], window["end_date"])
        except Exception:
            logger.warning("Collector read failed: %s", key, exc_info=True)
    return out


def _sum_days(days: Dict[str, Dict[str, Any]], field: str) -> Optional[int]:
    """Sum one micro field over a resource's days; None when no day carried it."""
    values = [_int(item.get(field)) for item in days.values() if field in item]
    values = [v for v in values if v is not None]
    return sum(values) if values else None


def _days_between(start_date: str, end_date: str) -> List[str]:
    """Every `YYYY-MM-DD` from start to end inclusive."""
    try:
        start = datetime.fromisoformat(start_date)
        end = datetime.fromisoformat(end_date)
    except ValueError:
        return []
    return [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range((end - start).days + 1)]


def _bill_settled(day: str, checked_at: Any) -> bool:
    """Whether Cost Explorer had a whole day to ingest `day` before the reconciler
    read it.

    CE publishes a day's usage over the following ~24 hours. The reconciler runs
    every six hours and rewrites every day in its window, so yesterday's row is
    checked as early as 04:00 the next morning — when CE has ingested a fifth of
    it. Measured live on 2026-09-24 04:06Z: the 09-23 row read $0.24 billed
    against $1.48 of ours (−84%) while all 28 earlier days agreed to within
    0.05%, and that single day turned the page's headline into "−3.45%".

    A day is settled when the check happened on or after the second calendar day
    following it (the day after `day` had fully elapsed). Conservative by up to a
    day; exact would need CE's own ingestion clock, which it does not publish.
    The bill's *totals* keep unsettled days — that money is real so far — only
    the comparison excludes them.
    """
    stamp = str(checked_at or "")[:10]
    if not stamp or not day:
        return False
    try:
        settled_from = (datetime.fromisoformat(day) + timedelta(days=2)).strftime("%Y-%m-%d")
    except ValueError:
        return False
    return stamp >= settled_from


def _runtime_block(resources: Dict[str, Dict[str, Dict[str, Any]]]) -> Dict[str, Any]:
    """The collector's runtime tier, totalled per runtime ARN (never per record)."""
    cost = vcpu = gb = 0
    any_cost = False
    as_of: Optional[str] = None
    complete_through: Optional[str] = None
    incomplete_days: set = set()
    for days in resources.values():
        for day, item in days.items():
            if "runtime_cost_micros" in item:
                cost += _int(item["runtime_cost_micros"]) or 0
                any_cost = True
            vcpu += _int(item.get("vcpu_hours_micro")) or 0
            gb += _int(item.get("gb_hours_micro")) or 0
            stamp = str(item.get("collected_at") or "")
            if stamp and (as_of is None or stamp > as_of):
                as_of = stamp
            if item.get("complete"):
                if complete_through is None or day > complete_through:
                    complete_through = day
            else:
                incomplete_days.add(day)
    active = idle = None
    if any_cost:
        # CPU is billed only while consumed; memory for every second a microVM
        # lived. The split is what shows a keep-warm session's cost for what it is.
        rate_items = [i for d in resources.values() for i in d.values() if "rate_vcpu_hour_micro" in i]
        if rate_items:
            rate_vcpu = _int(rate_items[-1]["rate_vcpu_hour_micro"]) or 0
            rate_gb = _int(rate_items[-1]["rate_gb_hour_micro"]) or 0
            active = round(vcpu * rate_vcpu / 1_000_000)
            idle = round(gb * rate_gb / 1_000_000)
    return {
        "micros": cost if any_cost else None,
        "vcpu_hours_micro": vcpu if any_cost else None,
        "gb_hours_micro": gb if any_cost else None,
        "active_micros": active,
        "idle_micros": idle,
        "as_of": as_of,
        "complete_through": complete_through,
        "runtimes": len(resources),
    }


def _recon(window: Dict[str, Any], usage: Any) -> Dict[str, Any]:
    """The reconciliation snapshots in the window, grouped by kind. DDB only."""
    from services.usage_service import month_shards

    out: Dict[str, List[Dict[str, Any]]] = {"agent_runtime": [], "component": [], "model_rate": [], "summary": []}
    repository = getattr(usage, "repository", None)
    if repository is None or not hasattr(repository, "query_prefix"):
        return out
    for shard in month_shards(window["start_date"], window["end_date"]):
        try:
            items = repository.query_prefix(f"RECON#{shard}", "D#")
        except Exception:
            logger.warning("RECON read failed for %s", shard, exc_info=True)
            continue
        for item in items:
            sort_key = str(item.get("sk", ""))
            head, sep, rest = sort_key.partition("#K#")
            if not sep or not head.startswith("D#"):
                continue
            day = head[2:]
            if not (window["start_date"] <= day <= window["end_date"]):
                continue
            kind, _, key = rest.partition("#")
            if kind not in out:
                continue
            out[kind].append({"day": day, "key": key, **item})
    return out


def _cost_block(
    window: Dict[str, Any],
    *,
    totals_rows: List[Dict[str, Any]],
    model_rows: List[Dict[str, Any]],
    collected: Dict[str, Any],
    recon: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, Any]:
    """The page's cost section: ledger + collector as the figures, RECON as the check."""
    priced = [row["model_cost_micros"] for row in totals_rows if row.get("model_cost_micros") is not None]
    model = {
        "micros": sum(priced) if priced else None,
        "priced_turns": sum(row["priced_turns"] for row in totals_rows),
        "unpriced_turns": sum(row["unpriced_turns"] for row in totals_rows),
        "unregistered_models": sorted({row["model_id"] for row in model_rows if not row["registered"]}),
        "rate_card_version": RATE_CARD_VERSION,
    }
    runtime = _runtime_block(collected["resources"])
    # Per-session figures, when USAGE_LOGS are flowing. Keep-warm sessions are the
    # price of staying warm; nobody's conversation caused them.
    session_total = keepwarm_total = 0
    any_session = False
    for days_map in collected["sessions"].values():
        for item in days_map.values():
            if "runtime_cost_micros" not in item:
                continue
            any_session = True
            micros = _int(item["runtime_cost_micros"]) or 0
            session_total += micros
            if item.get("keepwarm"):
                keepwarm_total += micros
    runtime["session_micros"] = session_total if any_session else None
    runtime["keepwarm_micros"] = keepwarm_total if any_session else None
    gateway_cost = None
    gateway_calls = 0
    gateway_billable: Optional[int] = None
    for days in collected["gateways"].values():
        for item in days.values():
            gateway_calls += _int(item.get("invocations")) or 0
            # Items written before the split have no `billable_requests`; the
            # figure stays absent rather than pretending the handshake was billed.
            if "billable_requests" in item:
                gateway_billable = (gateway_billable or 0) + (_int(item["billable_requests"]) or 0)
            if "gateway_cost_micros" in item:
                gateway_cost = (gateway_cost or 0) + (_int(item["gateway_cost_micros"]) or 0)
    memory_cost = None
    memory_events = memory_retrievals = 0
    for days in collected["memories"].values():
        for item in days.values():
            memory_events += _int(item.get("events")) or 0
            memory_retrievals += _int(item.get("retrievals")) or 0
            if "memory_cost_micros" in item:
                memory_cost = (memory_cost or 0) + (_int(item["memory_cost_micros"]) or 0)

    parts = [model["micros"], runtime["micros"], gateway_cost, memory_cost]
    known = [part for part in parts if part is not None]
    # A total exists once the two tiers that carry real money — model and
    # runtime — both do. Gateway and memory are cents and join when present.
    total = sum(known) if model["micros"] is not None and runtime["micros"] is not None else None

    billed = None
    diff = None
    agent_days = recon["agent_runtime"]
    components = recon["component"]
    # The window's last day is still being written, and Cost Explorer's figure
    # for it covers only the hours it has ingested so far. Comparing that
    # fraction against this page's full-day quantities manufactured a gap
    # (measured live: −8.5% over 7 days, of which every complete day agreed to
    # within 0.05% and the partial day alone was −$0.48 of −$0.84). The bill's
    # own totals keep the partial day — it is real money so far — but every
    # comparison uses complete days only.
    # ... and yesterday is only *partly* in Cost Explorer for most of today
    # (`_bill_settled`), so the comparison waits one more day for each row.
    partial_day = window["end_date"]
    complete_agent_days = [r for r in agent_days if r["day"] < partial_day and _bill_settled(r["day"], r.get("checked_at"))]
    complete_components = [r for r in components if r["day"] < partial_day] or components
    if agent_days or components:
        latest_component_day = max((r["day"] for r in complete_components), default=None)
        component_billed = {
            r["key"]: _int(r.get("billed_micros")) or 0
            for r in complete_components
            if r["day"] == latest_component_day
        }
        billed_runtime = sum((_int(r.get("billed_micros")) or 0) for r in agent_days)
        latest_day = max((r["day"] for r in agent_days), default=max((r["day"] for r in components), default=None))
        billed = {
            "runtime_micros": billed_runtime if agent_days else component_billed.get("runtime"),
            "component": component_billed,
            "component_day": latest_component_day,
            "latest_day": latest_day,
            "ce_estimated": any(bool(r.get("ce_estimated")) for r in agent_days + components),
            "checked_at": max((str(r.get("checked_at") or "") for r in agent_days + components), default=None),
        }
        compared = [r for r in complete_agent_days if r.get("diff_micros") is not None]
        if compared:
            ours = sum((_int(r.get("ours_micros")) or 0) for r in compared)
            total_diff = sum((_int(r.get("diff_micros")) or 0) for r in compared)
            diff = {
                "runtime_micros": total_diff,
                "runtime_pct": round(total_diff / ours, 6) if ours else None,
                "days_compared": len({r["day"] for r in compared}),
                "through": max(r["day"] for r in compared),
            }

    rates = recon["model_rate"]
    latest_rate_day = max((r["day"] for r in rates), default=None)
    current_rates = [r for r in rates if r["day"] == latest_rate_day]
    mismatches = []
    unregistered = set()
    # The bill carries other tenants' models too. Only a family *we* ran
    # unpriced is a finding for this page; the rest is somebody else's tariff.
    from services.billing_service import model_key

    ours_unregistered = {model_key(mid) for mid in model["unregistered_models"]}
    for row in current_rates:
        family, _, rest = row["key"].partition("|")
        routing, _, tier = rest.partition("|")
        if row.get("registered") is False:
            if model_key(family) in ours_unregistered:
                unregistered.add(family)
        elif row.get("match") is False:
            mismatches.append({
                "family": family, "routing": routing, "tier": tier,
                "card_micro": _int(row.get("card_usd_per_1m_micro")),
                "billed_micro": _int(row.get("billed_usd_per_1m_micro")),
            })
    summaries = recon["summary"]
    checked_at = max((str(r.get("checked_at") or "") for r in summaries), default=None) if summaries else (
        max((str(r.get("checked_at") or "") for r in current_rates), default=None) if current_rates else None
    )
    from data.model_rates import family_of, learned_rates

    # What the bill taught the card, dated — so the reader can see that a figure
    # for a model the committed table lacks rests on billed rates. Only the
    # families this platform actually ran: the account bills other tenants'
    # models too, and their learned rates are facts about the account, not about
    # this page.
    ours = {family_of(row["model_id"]) for row in model_rows} - {None}
    rate_card = {
        "version": RATE_CARD_VERSION,
        "checked_at": checked_at,
        "mismatches": mismatches,
        "unregistered_families": sorted(unregistered),
        "learned": [
            {
                "family": entry["family"],
                "routing": entry["routing"],
                "tier": entry["tier"],
                "effective_from": entry["effective_from"],
                "usd_per_1m": str(entry["usd_per_1m"]),
                "source": entry["source"],
            }
            for entry in learned_rates()
            if entry["family"] in ours
        ],
    }

    return {
        "model": model,
        "runtime": runtime,
        "gateway": {"micros": gateway_cost, "invocations": gateway_calls, "billable_requests": gateway_billable},
        "memory": {
            "micros": memory_cost,
            "events": memory_events,
            "retrievals": memory_retrievals,
            # Long-term storage has no metric; the bill is its only source.
            "billed_micros": (billed or {}).get("component", {}).get("memory") if billed else None,
        },
        "total_micros": total,
        "billed": billed,
        "diff": diff,
        "rate_card": rate_card,
    }


@router.get("/summary")
def summary(
    days: int = Query(7),
    user: AuthUser = Depends(current_user),
) -> Dict[str, Any]:
    """KPI row plus the agent leaderboard — the point of the page.

    Everything here was written before the request arrived: turn events and their
    derived counters by the chat stream, runtime/gateway/memory quantities by the
    collector, and the bill by the six-hourly reconciliation. The route reads DDB
    and nothing else, which is why the page can poll it: no CloudWatch, no Cost
    Explorer, no Price List call is made on this path (`test_insights_summary_cost`
    wires clients that raise to prove it).

    Admin-only: the leaderboard is every agent's traffic and spend across the org.
    """
    _require_admin(user)
    _require_configured()
    usage = _usage()
    usage.begin_read()
    usage.ensure_learned_rates()
    window = _window(days)

    # Aliases before any ledger read. `agent_totals` folds a `deployed:<arn>`
    # ledger key into its record through `canonical`, which reads the aliases
    # set on this (process-wide) service — so with the reads first, the very
    # first /summary after a deploy showed a harness's turn as its own row named
    # by its ARN, and every later request folded it (live, 2026-09-24).
    records: List[Any] = []
    try:
        records = list(usage.registry.agent_records())
    except Exception:
        logger.warning("Could not read agent records", exc_info=True)
    usage.set_record_aliases(records)
    totals = usage.agent_totals(window["start_date"], window["end_date"])
    by_model = usage.agent_models(window["start_date"], window["end_date"])
    names = {record.record_id: record.name for record in records}
    harness_runtimes = _harness_runtimes(usage) if any(
        getattr(r, "harness_arn", None) for r in records
    ) else {}
    collected = _collected(window)
    recon = _recon(window, usage)

    # Billed runtime per agent name, summed over the window's *complete* RECON
    # days — the partial last day would compare a few hours of bill against a
    # whole day of this page (see `_cost_block`).
    billed_by_agent: Dict[str, Dict[str, int]] = {}
    for row in recon["agent_runtime"]:
        if row["day"] >= window["end_date"]:
            continue
        bucket = billed_by_agent.setdefault(row["key"], {"billed": 0, "diff": 0, "has_diff": False})
        bucket["billed"] += _int(row.get("billed_micros")) or 0
        # The per-agent gap obeys the same settlement rule as the page's headline
        # (`_bill_settled`): a day CE has only begun to ingest is not a gap.
        if row.get("diff_micros") is not None and _bill_settled(row["day"], row.get("checked_at")):
            bucket["diff"] += _int(row["diff_micros"]) or 0
            bucket["has_diff"] = True

    rows: List[Dict[str, Any]] = []
    claimed_runtimes: set = set()
    for record_id, counters in totals.items():
        record = next((r for r in records if r.record_id == record_id), None)
        arns = _runtime_arns_of(record, harness_runtimes) if record else []
        runtime_days = {}
        for arn in arns:
            for day, item in collected["resources"].get(arn, {}).items():
                runtime_days[(arn, day)] = item
        runtime_cost = _sum_days(runtime_days, "runtime_cost_micros")
        invocations = _sum_days(runtime_days, "invocations")
        sessions = _sum_days(runtime_days, "sessions")
        model_cost = counters["model_cost_micros"] if counters["priced_turns"] > 0 else None
        total_cost = (
            model_cost + runtime_cost
            if model_cost is not None and runtime_cost is not None
            else None
        )
        name = names.get(record_id, record_id)
        billed = billed_by_agent.get(name)
        rows.append({
            "record_id": record_id,
            "name": name,
            "model_ids": sorted(by_model.get(record_id, {})),
            "turns": counters["turns"],
            "distinct_users": usage.distinct_users(
                record_id, window["start_date"], window["end_date"]
            ),
            "input_tokens": counters["input_tokens"],
            "output_tokens": counters["output_tokens"],
            "cache_read_tokens": counters["cache_read_tokens"],
            "cache_write_tokens": counters["cache_write_tokens"],
            "unmeasured_turns": counters["unmeasured_turns"],
            "tool_calls": counters["tool_calls"],
            "interrupted_turns": counters["interrupted_turns"],
            "failed_turns": counters["failed_turns"],
            "threads_started": counters["threads_started"],
            "priced_turns": counters["priced_turns"],
            "unpriced_turns": counters["unpriced_turns"],
            # Written at turn time from the rate card. Absent, never 0, when no
            # turn in the window could be priced.
            "model_cost_micros": model_cost,
            # The collector's figure for this record's runtime(s). Two records
            # that share a runtime both show it; the totals dedupe on ARN.
            "runtime_cost_micros": runtime_cost,
            "total_cost_micros": total_cost,
            "invocations": invocations,
            "sessions": sessions,
            "billed_runtime_micros": billed["billed"] if billed else None,
            "cost_diff_micros": billed["diff"] if billed and billed["has_diff"] else None,
            "_runtimes": frozenset(arns),
        })
    rows.sort(key=lambda row: -row["turns"])
    for row in rows:
        claimed_runtimes |= row.pop("_runtimes")
    # Runtime cost no leaderboard row claims. The Runtime tile totals every
    # collected runtime; a record with no runtime of its own (an MCP-app record),
    # a runtime a redeploy retired mid-window, or a deleted harness's companion
    # all bill without a row to sit in. Live on 2026-09-24 that was $1.22 of
    # $37.74 — the reader summing the table came up short and nothing said why.
    unclaimed: List[Dict[str, Any]] = []
    for arn, days_map in collected["resources"].items():
        if arn in claimed_runtimes:
            continue
        micros = _sum_days(days_map, "runtime_cost_micros")
        if micros is None:
            continue
        unclaimed.append({"runtime": CollectorService.runtime_name(arn), "runtime_cost_micros": micros})
    unclaimed.sort(key=lambda entry: -entry["runtime_cost_micros"])

    models: List[Dict[str, Any]] = []
    for record_id, per_model in by_model.items():
        for model_id, counters in per_model.items():
            rate = resolve_rate(model_id)
            models.append({
                "record_id": record_id,
                "model_id": model_id,
                "family": rate["family"] if rate else None,
                "routing": rate["routing"] if rate else None,
                "registered": rate is not None,
                "turns": counters["turns"],
                "input_tokens": counters["input_tokens"],
                "output_tokens": counters["output_tokens"],
                "cache_read_tokens": counters["cache_read_tokens"],
                "cache_write_tokens": counters["cache_write_tokens"],
                "model_cost_micros": counters["model_cost_micros"] if counters["priced_turns"] > 0 else None,
            })
    # One row per model across agents for the mix panel.
    mix: Dict[str, Dict[str, Any]] = {}
    for entry in models:
        row = mix.setdefault(entry["model_id"], {**{k: v for k, v in entry.items() if k != "record_id"},
                                                 "turns": 0, "input_tokens": 0, "output_tokens": 0,
                                                 "cache_read_tokens": 0, "cache_write_tokens": 0,
                                                 "model_cost_micros": None, "agents": 0})
        for key in ("turns", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
            row[key] += entry[key]
        if entry["model_cost_micros"] is not None:
            row["model_cost_micros"] = (row["model_cost_micros"] or 0) + entry["model_cost_micros"]
        row["agents"] += 1

    cost = _cost_block(window, totals_rows=rows, model_rows=models, collected=collected, recon=recon)
    cost["runtime"]["unclaimed_micros"] = sum(e["runtime_cost_micros"] for e in unclaimed) if unclaimed else None
    cost["runtime"]["unclaimed_runtimes"] = unclaimed

    guardrail = None
    guardrail_by_day: Dict[str, Dict[str, int]] = {}
    # sub -> email for the people the guardrail section names; the only place
    # this route names anyone.
    subjects: Dict[str, str] = {}
    try:
        by_agent = usage.guardrail_totals(window["start_date"], window["end_date"])
        turns_by_agent = {row["record_id"]: row["turns"] for row in rows}
        # Rate, not count, is what compares a busy agent with a quiet one: two
        # interventions are a footnote on 700 turns and a pattern on 7.
        for record_id, agg in by_agent.items():
            turns = turns_by_agent.get(record_id, 0)
            agg["intervention_rate"] = agg["interventions"] / turns if turns else None
        agg = guardrail_sum(by_agent.values())
        all_turns = sum(turns_by_agent.values())
        agg["intervention_rate"] = agg["interventions"] / all_turns if all_turns else None
        # The governance gap. An agent that served turns without a single guardrail
        # trace ran unguarded; zero interventions alone cannot say that, because a
        # guarded agent nobody provoked reads the same.
        #
        # Judged only on turns from the day *after* the first scan recorded in the
        # window. Scan counting has a start date, and a turn before it says
        # nothing either way — weighing those would list every guarded agent as a
        # gap until the window rolled past the deploy. The first scan day itself is
        # skipped too: day granularity cannot split it into before and after, and
        # the live list did name two guarded runtimes on deploy day. No scan in the
        # window means no verdict, not "everyone is unguarded".
        for row in rows:
            row["guardrail_scanned_turns"] = by_agent.get(row["record_id"], {}).get("scanned_turns", 0)
        guardrail_by_day = usage.guardrail_daily_totals(window["start_date"], window["end_date"])
        scan_days = sorted(day for day, point in guardrail_by_day.items() if point.get("scanned_turns"))
        unguarded: List[str] = []
        if scan_days:
            judged_from = (datetime.fromisoformat(scan_days[0]) + timedelta(days=1)).strftime("%Y-%m-%d")
            judged_turns = usage.agent_turns_since(window["start_date"], window["end_date"], judged_from)
            unguarded = sorted(
                row["record_id"] for row in rows
                if judged_turns.get(row["record_id"], 0) > 0 and row["guardrail_scanned_turns"] == 0
            )
        # The rows behind the counts, and the per-person half. Both name people
        # (owner subs) and conversations (thread ids); this route is admin-only,
        # which is the same line `/users` and the per-record turn list draw.
        recent = usage.guardrail_events(window["start_date"], window["end_date"], limit=RECENT_GUARDRAIL_EVENTS)
        by_user = usage.guardrail_user_totals(window["start_date"], window["end_date"])
        guardrail = {
            "by_agent": by_agent,
            "totals": agg,
            "unguarded_agents": unguarded,
            "recent": recent,
            "by_user": by_user,
        }
        subjects = _subjects(list(by_user) + [e.get("owner_sub") for e in recent])
    except Exception:
        logger.info("Guardrail rollup unavailable", exc_info=True)

    # Daily series with both cost tiers. Runtime cost per day is summed over every
    # collected runtime, which is the same population the runtime tile totals.
    daily = usage.daily_totals(window["start_date"], window["end_date"])
    runtime_by_day: Dict[str, int] = {}
    for days_map in collected["resources"].values():
        for day, item in days_map.items():
            if "runtime_cost_micros" in item:
                runtime_by_day[day] = runtime_by_day.get(day, 0) + (_int(item["runtime_cost_micros"]) or 0)
    for point in daily:
        point["runtime_cost_micros"] = runtime_by_day.get(point["date"])
        point["guardrail_interventions"] = guardrail_by_day.get(point["date"], {}).get("interventions", 0)

    today_collected = any(
        window["end_date"] in days_map for days_map in collected["resources"].values()
    )
    sources = {
        # Never hardcoded true again. A shard that could not be read leaves the
        # totals short by however much it held, and the reader cannot see the hole.
        "usage": usage.reads_complete(),
        "collector": today_collected,
        "recon": bool(recon["agent_runtime"] or recon["component"] or recon["model_rate"]),
        "usage_logs": bool(collected["sessions"]),
        "guardrail": guardrail is not None,
    }

    return {
        **_window_fields(window),
        "sources": sources,
        "totals": {
            "turns": sum(row["turns"] for row in rows),
            "input_tokens": sum(row["input_tokens"] for row in rows),
            "output_tokens": sum(row["output_tokens"] for row in rows),
            "cache_read_tokens": sum(row["cache_read_tokens"] for row in rows),
            "cache_write_tokens": sum(row["cache_write_tokens"] for row in rows),
            "unmeasured_turns": sum(row["unmeasured_turns"] for row in rows),
            "tool_calls": sum(row["tool_calls"] for row in rows),
            "interrupted_turns": sum(row["interrupted_turns"] for row in rows),
            "failed_turns": sum(row["failed_turns"] for row in rows),
            "priced_turns": sum(row["priced_turns"] for row in rows),
            "unpriced_turns": sum(row["unpriced_turns"] for row in rows),
        },
        "cost": cost,
        "agents": rows,
        "models": sorted(mix.values(), key=lambda row: -row["turns"]),
        "guardrail": guardrail,
        "subjects": subjects,
        "daily": daily,
    }


@router.get("/telemetry")
def telemetry(
    days: int = Query(7),
    user: AuthUser = Depends(current_user),
) -> Dict[str, Any]:
    """The CloudWatch tier, only when a reader asks for it.

    Its own route, and not on the poll, because it is the one metered read on
    this page: $0.01 per 1,000 metrics requested, measured at 72 metrics a sweep
    once the dimension memo is warm and 176 cold. The service caches the result
    for five minutes, so pressing the button repeatedly is free.

    Not gated on the usage table: these figures come from CloudWatch and a
    registry listing, so they are answerable in an environment where nothing is
    being recorded. Every field is absent rather than zero when CloudWatch has
    nothing for that agent — the UI renders those as dashes.

    **No cost lives here any more.** Runtime cost is collected per day by
    `collector_service` and served from `/summary`; this route is the performance
    tier only — latency, error rate, raw invocation counts for the window.

    Admin-only, like `/summary`: it is the same org-wide fleet, priced.
    """
    _require_admin(user)
    window = _window(days)
    usage = _usage()

    records: List[Any] = []
    try:
        records = list(usage.registry.agent_records())
    except Exception:
        logger.warning("Could not read agent records", exc_info=True)
    usage.set_record_aliases(records)

    names = {record.record_id: record.name for record in records}
    arns_by_record = _agent_arns(records)
    flat_arns = [arn for arns in arns_by_record.values() for arn in arns]

    vended: Dict[str, Dict[str, float]] = {}
    available = False
    if flat_arns:
        try:
            vended = _telemetry().agent_metrics(
                flat_arns, window["start"], window["end"]
            )
            available = True
        except TelemetryUnavailable as exc:
            # 200 with sources.cloudwatch false, never a 5xx: the reader asked a
            # reasonable question and the answer is "this account will not say".
            logger.info("CloudWatch unavailable: %s", exc)
        except Exception:
            logger.warning("CloudWatch read failed", exc_info=True)

    rows: List[Dict[str, Any]] = []
    for record_id, arns in arns_by_record.items():
        metrics: Dict[str, float] = {}
        source: List[str] = []
        for arn in arns:
            found = vended.get(arn)
            if found:
                metrics.update(found)
                source.append(arn)
        if not metrics:
            # No series for this agent at all. Omitted rather than sent as a row
            # of nulls, so "CloudWatch knows nothing about this agent" and "this
            # agent was idle" stay distinguishable.
            continue
        rows.append({
            "record_id": record_id,
            "name": names.get(record_id, record_id),
            "invocations": metrics.get("invocations"),
            "latency_p90_ms": metrics.get("latency_p90_ms"),
            "error_rate": metrics.get("error_rate"),
            # Which of `SystemErrors`/`UserErrors` the rate stands on. A rate built
            # on one of the two is a different claim from one built on both, and the
            # numerator used to drop the missing series without saying so.
            "error_basis": metrics.get("error_basis"),
            "vcpu_hours": metrics.get("vcpu_hours"),
            "gb_hours": metrics.get("gb_hours"),
            # Which ARN answered. Not rendered — it is what the totals below
            # de-duplicate on.
            "_source": frozenset(source),
        })
    rows.sort(key=lambda row: -(row["invocations"] or 0.0))

    # **Totalled per runtime, not per row.** Two registry records may describe one
    # physical runtime, and their rows then carry byte-identical vended figures.
    # Measured on the live registry:
    #
    #   harness_builtin_test_agent  runtime/harness_builtin_test_agent-KlMnO13579
    #   builtin_test_agent          harness/builtin_test_agent-OpQrS99001
    #                             + runtime/harness_builtin_test_agent-KlMnO13579
    #
    # Summing the rows counted that runtime's hours twice, inflating both the cost
    # total and our side of the reconciliation — which made coverage look better
    # than it is.
    #
    # The identity test is **overlap, not equality**: the ARNs a record carries are
    # aliases for one runtime (a harness and its companion), so sharing any one of
    # them means sharing the runtime. Comparing the sets for equality misses exactly
    # the case above, where one record knows both aliases and the other knows one.
    #
    # Per-record rows stay — each record really did serve its turns, and an operator
    # comparing two records wants both.
    claimed: set = set()
    vended_vcpu = 0.0
    vended_gb = 0.0
    distinct_runtimes = 0
    for row in rows:
        if row["_source"] & claimed:
            continue
        claimed |= row["_source"]
        distinct_runtimes += 1
        vended_vcpu += row["vcpu_hours"] or 0.0
        vended_gb += row["gb_hours"] or 0.0
    for row in rows:
        del row["_source"]

    return {
        **_window_fields(window),
        "sources": {"cloudwatch": available},
        "agents": rows,
        "totals": {
            "vcpu_hours": round(vended_vcpu, 6),
            "gb_hours": round(vended_gb, 6),
            # Rows can outnumber runtimes when records share a harness, and the
            # difference is why the totals are not the column sums.
            "agents_listed": len(rows),
            "distinct_runtimes": distinct_runtimes,
        },
    }


@router.get("/composition")
def composition(
    days: int = Query(7),
    user: AuthUser = Depends(current_user),
) -> Dict[str, Any]:
    """Which MCP servers, skills and knowledge bases earned their place.

    Its own route, not part of `summary`, because it needs the harness listing's
    `GetHarness` fan-out — the slowest call in this feature. The leaderboard must
    not wait behind it.

    Admin-only, like `/summary`: it rolls up the whole fleet's tool and skill use.
    """
    _require_admin(user)
    _require_configured()
    usage = _usage()
    usage.begin_read()
    window = _window(days)
    rollup = usage.composition(window["start_date"], window["end_date"])
    return {
        **_window_fields(window),
        # The rollup is keyed on our own counters, so an unreadable partition
        # undercounts every row in it — the same admission `/summary` makes.
        "sources": {"usage": usage.reads_complete()},
        **rollup,
    }


@router.get("/me")
def my_insights(
    days: int = Query(7),
    sub: Optional[str] = Query(None),
    user: AuthUser = Depends(current_user),
) -> Dict[str, Any]:
    """One person's own usage. Owner-or-admin, matching thread ownership."""
    _require_configured()
    target = sub or user.sub
    if target != user.sub and not user.is_admin:
        raise HTTPException(
            status_code=403, detail="You may only read your own usage."
        )
    usage = _usage()
    usage.begin_read()
    window = _window(days)
    totals = usage.user_totals(window["start_date"], window["end_date"])
    return {
        "sub": target,
        **_window_fields(window),
        "sources": {"usage": usage.reads_complete()},
        "totals": totals.get(target, {}),
    }


@router.get("/users")
def user_leaderboard(
    days: int = Query(7),
    user: AuthUser = Depends(current_user),
) -> Dict[str, Any]:
    """Usage per person, with each person's spend. **Admin only.**

    Model cost per person is the ledger's own sum: every turn was priced when it
    ended, by the model it ran on, and the figure rode onto the user's day item.
    Runtime cost per person exists only when USAGE_LOGS session attribution is on
    (`sources.usage_logs`); without it the column is absent, never zero.

    Admin-only, unlike the organisation aggregates: those name agents, which are
    public records, and this names people. `/me` remains the owner-scoped door to
    the same data, so a plain user can still see their own row.

    A subject is a Cognito `sub`, not a name. Resolving it would mean a
    `ListUsers` per read against a pool this service otherwise never calls, so the
    id is returned as-is and the client shortens it for display.
    """
    # Authorisation before availability. A plain user must get 403 whether or not
    # this environment records usage — answering 501 first tells them the route
    # exists and what it needs, which is a fact about the deployment they have no
    # standing to learn from an endpoint they cannot call.
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")
    _require_configured()

    usage = _usage()
    usage.begin_read()
    window = _window(days)
    totals = usage.user_totals(window["start_date"], window["end_date"])
    costs = usage.user_costs(window["start_date"], window["end_date"])
    guardrail_by_user: Dict[str, Dict[str, Any]] = {}
    try:
        guardrail_by_user = usage.guardrail_user_totals(window["start_date"], window["end_date"])
    except Exception:
        logger.info("Guardrail per-user rollup unavailable", exc_info=True)

    # Runtime cost per person needs a session's cost (USAGE_LOGS, keyed by thread
    # id) and the thread's owner (the turn events carry it). Both are ledger reads.
    collected = _collected(window)
    sessions = collected["sessions"]
    runtime_by_user: Dict[str, int] = {}
    if sessions:
        owner_of_thread: Dict[str, str] = {}
        for event in usage.turn_events(window["start_date"], window["end_date"]):
            if event.get("thread_id") and event.get("owner_sub"):
                owner_of_thread.setdefault(str(event["thread_id"]), str(event["owner_sub"]))
        for key, days_map in sessions.items():
            session_id = key.split("#R#", 1)[0]
            owner = owner_of_thread.get(session_id)
            if not owner:
                continue
            for item in days_map.values():
                if "runtime_cost_micros" in item:
                    runtime_by_user[owner] = runtime_by_user.get(owner, 0) + (_int(item["runtime_cost_micros"]) or 0)

    # Who uses which agent, and who shows up on which days. With two accounts on
    # the live pool the per-person table said little beyond two totals; these are
    # the two questions an admin asks once there are ten — "what does this team
    # actually use" and "how many people were here today".
    records: List[Any] = []
    try:
        records = list(usage.registry.agent_records())
    except Exception:
        logger.warning("Could not read agent records", exc_info=True)
    usage.set_record_aliases(records)
    names = {record.record_id: record.name for record in records}
    matrix = usage.agent_user_matrix([record.record_id for record in records], window["start_date"], window["end_date"])
    by_day = usage.daily_users(window["start_date"], window["end_date"])
    active_days: Dict[str, int] = {}
    for subs in by_day.values():
        for sub in subs:
            active_days[sub] = active_days.get(sub, 0) + 1

    rows: List[Dict[str, Any]] = []
    for sub, counters in totals.items():
        cost = costs.get(sub, {})
        model_cost = cost.get("model_cost_micros") if cost.get("priced_turns") else None
        runtime_cost = runtime_by_user.get(sub) if sessions else None
        agents = sorted(
            ({"record_id": rid, "name": names.get(rid, rid), "turns": turns} for rid, turns in matrix.get(sub, {}).items()),
            key=lambda entry: (-entry["turns"], entry["name"]),
        )
        rows.append({
            "sub": sub,
            "agents": agents,
            "active_days": active_days.get(sub, 0),
            "turns": counters.get("turns", 0),
            "input_tokens": counters.get("input_tokens", 0),
            "output_tokens": counters.get("output_tokens", 0),
            "cache_read_tokens": counters.get("cache_read_tokens", 0),
            "cache_write_tokens": counters.get("cache_write_tokens", 0),
            "failed_turns": counters.get("failed_turns", 0),
            # How this person works, not just how much: conversations started
            # (turns per thread is depth), answers they walked away from, and
            # tool calls made on their behalf. All three are ledger counters
            # every USERS# row already carries.
            "threads_started": counters.get("threads_started", 0),
            "interrupted_turns": counters.get("interrupted_turns", 0),
            "tool_calls": counters.get("tool_calls", 0),
            "unmeasured_turns": counters.get("unmeasured_turns", 0),
            "priced_turns": cost.get("priced_turns", 0),
            "unpriced_turns": cost.get("unpriced_turns", 0),
            "model_cost_micros": model_cost,
            "runtime_cost_micros": runtime_cost,
            "total_cost_micros": (
                model_cost + runtime_cost
                if model_cost is not None and runtime_cost is not None
                else None
            ),
            # Who keeps tripping the guardrail — the per-person half of the
            # governance question the agent leaderboard cannot answer.
            "guardrail_interventions": int(guardrail_by_user.get(sub, {}).get("interventions") or 0),
        })
    rows.sort(key=lambda row: -row["turns"])

    # Endings the agent ledger counted that no person's row carries. Live, the
    # agent ledger had 7 interrupted turns in the window and the rows summed to
    # 2: the other 5 were written on days when only the agent counter recorded
    # how a turn ended. The table says so rather than letting the reader hunt
    # for the person who "abandoned" them.
    agent_endings = usage.agent_totals(window["start_date"], window["end_date"]).values()
    unattributed = {
        key: max(0, sum(int(t.get(key) or 0) for t in agent_endings) - sum(row[key] for row in rows))
        for key in ("interrupted_turns", "failed_turns")
    }

    priced = [row["model_cost_micros"] for row in rows if row["model_cost_micros"] is not None]
    runtime_priced = [row["runtime_cost_micros"] for row in rows if row["runtime_cost_micros"] is not None]

    return {
        **_window_fields(window),
        "sources": {
            "usage": usage.reads_complete(),
            "usage_logs": bool(sessions),
            # The first day a session was logged in this window. Runtime cost per
            # person exists only from here, and only for conversations — the
            # keep-warm sessions and every day before this are in the Runtime
            # tile and in nobody's row, which is why the column does not sum to it.
            "usage_logs_since": min((day for days_map in sessions.values() for day in days_map), default=None),
        },
        "users": rows,
        # Distinct people per day, dense over the window: an idle day is 0.
        "daily_active_users": [
            {"date": day, "users": len(by_day.get(day, ()))}
            for day in _days_between(window["start_date"], window["end_date"])
        ],
        # sub -> email, for the rows that resolve. The client shows the local part;
        # a sub with no entry (deleted account, directory down) stays a sub.
        "subjects": _subjects(row["sub"] for row in rows),
        "totals": {
            "users": len(rows),
            "turns": sum(row["turns"] for row in rows),
            "input_tokens": sum(row["input_tokens"] for row in rows),
            "output_tokens": sum(row["output_tokens"] for row in rows),
            "cache_read_tokens": sum(row["cache_read_tokens"] for row in rows),
            "cache_write_tokens": sum(row["cache_write_tokens"] for row in rows),
            "failed_turns": sum(row["failed_turns"] for row in rows),
            "threads_started": sum(row["threads_started"] for row in rows),
            "interrupted_turns": sum(row["interrupted_turns"] for row in rows),
            "interrupted_turns_unattributed": unattributed["interrupted_turns"],
            "failed_turns_unattributed": unattributed["failed_turns"],
            "tool_calls": sum(row["tool_calls"] for row in rows),
            "unmeasured_turns": sum(row["unmeasured_turns"] for row in rows),
            "priced_turns": sum(row["priced_turns"] for row in rows),
            "unpriced_turns": sum(row["unpriced_turns"] for row in rows),
            "model_cost_micros": sum(priced) if priced else None,
            "runtime_cost_micros": sum(runtime_priced) if runtime_priced else None,
            "guardrail_interventions": sum(row["guardrail_interventions"] for row in rows),
        },
    }


@router.get("/records/{record_id:path}/threads")
def record_threads(
    record_id: str,
    limit: int = Query(20, ge=1),
    user: AuthUser = Depends(current_user),
) -> Dict[str, Any]:
    """The threads one agent answered in, newest first.

    The drill-down's entry point: a span timeline is fetched per thread, and a
    batch evaluation is started over thread ids, so neither is reachable from a
    record alone. The usage table cannot answer this — it holds counters keyed by
    date and agent, never thread ids — so the threads table is the only source.

    Scoped like `GET /threads`: a plain user sees only their own conversations,
    an admin sees everyone's. The record is public; the conversations under it are
    not.
    """
    threads = _threads().search_threads(
        # None means unscoped, the same signal routes/threads.py uses.
        owner_sub=None if user.is_admin else user.sub,
        limit=min(limit, MAX_RECORD_THREADS),
        sort_by="updated_at",
        sort_order="desc",
        agent_record_id=record_id,
    )

    return {
        "record_id": record_id,
        # Names for the owners above — admin only, like the owners themselves. A
        # plain user's list carries no owners, so the directory is not consulted.
        "subjects": (
            _subjects(getattr(thread, "owner_sub", "") for thread in threads)
            if user.is_admin else {}
        ),
        "threads": [
            {
                "thread_id": thread.thread_id,
                "created_at": getattr(thread, "created_at", None),
                "updated_at": getattr(thread, "updated_at", None),
                "status": _status_value(getattr(thread, "status", None)),
                "turns": _turn_count(thread),
                "title": _thread_title(thread),
                # Only an admin sees other people's threads, so only an admin gets
                # the owner: a plain user's list is already all their own, and the
                # by-subject leaderboard draws the same admin-only line for the same
                # reason — this field names a person. Absent (not empty) for a plain
                # user, so the client shows the column exactly when it is meaningful.
                "owner_sub": (
                    getattr(thread, "owner_sub", "") if user.is_admin else None
                ),
            }
            for thread in threads
        ],
    }


@router.get("/traces/{thread_id}")
def thread_traces(
    thread_id: str,
    user: AuthUser = Depends(current_user),
) -> Dict[str, Any]:
    """The span timeline for one thread.

    Thread-scoped, not turn-scoped. It always was in substance — the query below
    asks for every span in the session over the thread's lifetime — but the URL
    used to carry a `{turn_id}` that was echoed back and never filtered on, so
    the chat rendered the same thread-wide timeline under every turn and called
    each one "이 턴".

    Owner-or-admin, unlike the rest of this router: organisation aggregates are
    open to any authenticated user, but a span timeline is the inside of one
    person's conversation.

    Never returns 5xx for "not ready". A query still running comes back as
    `status: "timeout"`, and a missing prerequisite as `status: "unavailable"`
    with the reason — both inside a 200, so the UI can offer a retry the user
    chooses to press instead of SWR backing off on its own.
    """
    thread = _threads().get_thread(thread_id)
    owner = getattr(thread, "owner_sub", None) if thread else None
    if thread is None or (owner != user.sub and not user.is_admin):
        # 404 rather than 403: a 403 would confirm the thread exists, which is
        # the line routes/threads.py already draws.
        raise HTTPException(status_code=404, detail="Thread not found.")

    # The window is the thread's own lifetime, widened by a minute at each end:
    # span timestamps and the thread's own are written by different clocks, and a
    # tight window silently returns nothing.
    start, end = _trace_window(thread)

    try:
        result = _traces().spans_for_session(
            _traces().session_id_for(thread_id), start, end
        )
    except TracesUnavailable as exc:
        # The cause (an AWS error message) goes to the server log, not the
        # response: it can carry ARNs and policy details the caller shouldn't see.
        logger.warning("Traces unavailable for thread %s: %s", thread_id, exc)
        return {
            "thread_id": thread_id,
            "status": "unavailable",
            "spans": [],
            "sources": {"traces": False},
            "detail": (
                "스팬을 읽을 수 없습니다. CloudWatch Transaction Search 가 "
                "계정에 켜져 있는지 확인해 주세요."
            ),
        }

    return {
        "thread_id": thread_id,
        "status": result["status"],
        "spans": result["spans"],
        "log_group": result.get("log_group"),
        "sources": {"traces": result["status"] == "ok"},
    }


@router.get("/evaluators")
def evaluators(
    user: AuthUser = Depends(current_user),
) -> Dict[str, Any]:
    """List available evaluators, cached.

    Any authenticated user can see the catalogue — it is a zero-spend read.
    """
    try:
        evals = _evaluations().evaluators()
        return {
            "evaluators": evals,
            "sources": {"evaluations": True},
        }
    except EvaluationsUnavailable as exc:
        logger.info("Evaluations unavailable: %s", exc)
        return {
            "evaluators": [],
            "sources": {"evaluations": False},
        }


@router.post("/evaluations")
def start_evaluation(
    body: StartEvaluationRequest,
    user: AuthUser = Depends(current_user),
) -> Dict[str, Any]:
    """Start a batch evaluation over threads.

    Admin-only: LLM-as-judge is charged per judge model call. Thread ownership
    is also checked to ensure the run is marked with the thread's actual data,
    not a missing thread that would still be billable.
    """
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")

    # Verify every thread exists and is owned by the caller or the caller is admin.
    threads = _thread_ids_checked(body.thread_ids, user)

    # Check for empty evaluator_ids before calling the service (the service
    # will also raise ValueError, but we want to catch it here for a 422).
    if not body.evaluator_ids:
        raise HTTPException(
            status_code=422,
            detail="evaluator_ids must not be empty",
        )

    try:
        result = _evaluations().start(
            thread_ids=body.thread_ids,
            evaluator_ids=body.evaluator_ids,
            # Resolved before the paid call, not inside it: a batch that cannot be
            # scoped to one agent must fail for free.
            runtime_arn=_runtime_arn_for_threads(threads),
            record_id=body.record_id,
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except EvaluationsUnavailable as exc:
        logger.warning("Failed to start evaluation: %s", exc)
        # The reason travels with the status. A bare "Evaluation service
        # unavailable" is what this route used to answer, and it sent a reader
        # hunting for a missing AgentCore entitlement when the actual cause was a
        # ValidationException on the request we sent — the caller is an admin, so
        # AWS's own sentence is more use to them than our summary of it.
        raise HTTPException(
            status_code=503,
            detail=f"평가를 시작할 수 없습니다: {exc}",
        ) from exc


@router.get("/evaluations/{batch_id}")
def evaluation_status(
    batch_id: str,
    user: AuthUser = Depends(current_user),
) -> Dict[str, Any]:
    """Get status of a batch evaluation.

    Any authenticated user can query the status of an evaluation — it is
    attributable to a public record, like the leaderboard.
    """
    try:
        result = _evaluations().status(batch_id)
        result["sources"] = {"evaluations": True}
        return result
    except EvaluationsUnavailable as exc:
        logger.info("Evaluation unavailable: %s", exc)
        # Never raise: degrade inside a 200, matching turn_traces for TracesUnavailable.
        return {
            "batch_id": batch_id,
            "status": "unavailable",
            "sources": {"evaluations": False},
        }


@router.get("/records/{record_id:path}/evaluation")
def record_evaluation(
    record_id: str,
    user: AuthUser = Depends(current_user),
) -> Dict[str, Any]:
    """Get the latest evaluation for a record, if any.

    Any authenticated user can query — it is a score on a public agent record.
    """
    try:
        result = _evaluations().latest_for(record_id)
        if result is None:
            return {"status": "none"}
        result["sources"] = {"evaluations": True}
        return result
    except EvaluationsUnavailable as exc:
        logger.info("Evaluation unavailable: %s", exc)
        # Never raise: degrade inside a 200.
        return {
            "status": "unavailable",
            "sources": {"evaluations": False},
        }


# --- AgentCore insights (triage) --------------------------------------------
#
# The same batch machinery as the evaluation routes, with `insights` in place of
# `evaluators`, so the same rules hold: admin-only to start (billed per session
# analysed), free for anyone to read, one agent per run, degrade inside a 200.
#
# What is new is a line inside the response. Cluster names and
# `affected_session_count` describe a public agent and are shown to everyone. The
# per-session lists beneath them quote the conversations — `user_messages` is the
# user's own words, `explanation` paraphrases the exchange — so they are stripped
# for a plain user, the same line `record_threads` draws around `owner_sub`.
#
# Counts are AgentCore's, at every level, and nothing here sums them: one session
# can sit in several failure categories, so a total of category counts would be
# a number with no referent.


def _thread_ids_checked(thread_ids: List[str], user: AuthUser) -> List[Any]:
    """Every thread must exist and be visible to the caller before anything is
    charged. 404 rather than 403, matching `thread_traces`."""
    threads_svc = _threads()
    threads: List[Any] = []
    for thread_id in thread_ids:
        thread = threads_svc.get_thread(thread_id)
        owner = getattr(thread, "owner_sub", None) if thread else None
        if thread is None or (owner != user.sub and not user.is_admin):
            raise HTTPException(status_code=404, detail="Thread not found.")
        threads.append(thread)
    return threads


def _scope_insights(
    result: Dict[str, Any],
    user: AuthUser,
    record_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Apply the reader's scope to a run's `insights` tree, in place.

    Admin: keep the session lists and add `thread_id` to each hit, resolved from
    the record's own threads — AgentCore knows only the padded session id, and the
    person triaging wants to open the thread. Plain user: empty the lists and say
    so with `session_details: false`, so the client can tell "nobody" from
    "not for you".
    """
    tree = result.get("insights")
    if not tree:
        return result

    by_session: Dict[str, str] = {}
    if user.is_admin and record_id:
        try:
            threads = _threads().search_threads(
                owner_sub=None,
                limit=MAX_RECORD_THREADS,
                sort_by="updated_at",
                sort_order="desc",
                agent_record_id=record_id,
            )
            traces = _traces()
            by_session = {
                traces.session_id_for(t.thread_id): t.thread_id for t in threads
            }
        except Exception as exc:  # the map is a convenience, never a failure
            logger.info("Could not map sessions to threads: %s", exc)

    def scope(hits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not user.is_admin:
            return []
        return [
            {**hit, "thread_id": by_session.get(hit.get("session_id"))}
            for hit in hits
        ]

    for category in tree.get("failures") or []:
        for sub in category.get("sub_categories") or []:
            for root in sub.get("root_causes") or []:
                root["sessions"] = scope(root.get("sessions") or [])
    for cluster in tree.get("user_intents") or []:
        cluster["sessions"] = scope(cluster.get("sessions") or [])
    for cluster in tree.get("execution_summaries") or []:
        cluster["sessions"] = scope(cluster.get("sessions") or [])
    tree["session_details"] = bool(user.is_admin)
    return result


@router.post("/analyses")
def start_analysis(
    body: StartAnalysisRequest,
    user: AuthUser = Depends(current_user),
) -> Dict[str, Any]:
    """Start an insights run — failure triage, user intents, execution patterns —
    over threads. Admin-only: billed per session analysed."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")

    threads = _thread_ids_checked(body.thread_ids, user)

    try:
        result = _evaluations().start_insights(
            thread_ids=body.thread_ids,
            # Resolved before the paid call: a run that cannot be scoped to one
            # agent must fail for free.
            runtime_arn=_runtime_arn_for_threads(threads),
            record_id=body.record_id,
            insight_ids=body.insight_ids,
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except EvaluationsUnavailable as exc:
        logger.warning("Failed to start insights run: %s", exc)
        raise HTTPException(
            status_code=503,
            detail=f"분석을 시작할 수 없습니다: {exc}",
        ) from exc


@router.get("/analyses/{batch_id}")
def analysis_status(
    batch_id: str,
    user: AuthUser = Depends(current_user),
) -> Dict[str, Any]:
    """One insights run, scoped to the reader. Never a 5xx for "not ready"."""
    try:
        result = _evaluations().status(batch_id)
    except EvaluationsUnavailable as exc:
        logger.info("Insights run unavailable: %s", exc)
        return {
            "batch_id": batch_id,
            "status": "unavailable",
            "sources": {"evaluations": False},
        }
    result = _scope_insights(result, user)
    result["sources"] = {"evaluations": True}
    return result


@router.get("/records/{record_id:path}/analysis")
def record_analysis(
    record_id: str,
    user: AuthUser = Depends(current_user),
) -> Dict[str, Any]:
    """The latest insights run for a record, scoped to the reader."""
    try:
        result = _evaluations().latest_insights_for(record_id)
    except EvaluationsUnavailable as exc:
        logger.info("Insights run unavailable: %s", exc)
        return {
            "status": "unavailable",
            "sources": {"evaluations": False},
        }
    if result is None:
        return {"status": "none"}
    result = _scope_insights(result, user, record_id=record_id)
    result["sources"] = {"evaluations": True}
    return result


# `record_id:path`, and registered after the `/records/{record_id}/…`
# sub-routes above, on purpose. A deployed-agent fallback record's id is
# `deployed:<arn>`, and an ARN carries slashes (`…:harness/name`), so a plain
# `{record_id}` segment stops at the first slash and the route 404s — which is
# what stranded the Usage tab on "사용량을 읽고 있습니다". `:path` captures the
# rest, but a `:path` base route also greedily matches `/…/threads`,
# `/…/evaluation` and `/…/analysis`, so it must come last for those to win first.
@router.get("/records/{record_id:path}")
def record_insights(
    record_id: str,
    days: int = Query(7),
    user: AuthUser = Depends(current_user),
) -> Dict[str, Any]:
    """One agent: its counters, its cost, its models, its trend, its tools, its turns."""
    _require_configured()
    usage = _usage()
    usage.begin_read()
    window = _window(days)
    # Records first: a turn written under a `deployed:<arn>` fallback id belongs
    # to the record that owns the ARN, and every read below folds it in.
    records: List[Any] = []
    try:
        records = list(usage.registry.agent_records())
    except Exception:
        logger.info("Registry unavailable for record %s", record_id, exc_info=True)
    usage.set_record_aliases(records)
    counters = usage.record_totals(
        record_id, window["start_date"], window["end_date"]
    )
    # The model the agent runs *now*, for the header. Cost never reads it: every
    # turn was priced against the model it ran on, and `models` lists those.
    model_id = usage.model_map().get(record_id)
    by_model = usage.agent_models(window["start_date"], window["end_date"]).get(record_id, {})
    models = [
        {
            "model_id": mid,
            "turns": c["turns"],
            "input_tokens": c["input_tokens"],
            "output_tokens": c["output_tokens"],
            "cache_read_tokens": c["cache_read_tokens"],
            "cache_write_tokens": c["cache_write_tokens"],
            "model_cost_micros": c["model_cost_micros"] if c["priced_turns"] > 0 else None,
            "registered": resolve_rate(mid) is not None,
        }
        for mid, c in sorted(by_model.items(), key=lambda pair: -pair[1]["turns"])
    ]

    runtime_cost = None
    invocations = None
    record = next((r for r in records if r.record_id == record_id), None)
    if record is not None:
        collected = _collected(window)
        harness_runtimes = _harness_runtimes(usage) if getattr(record, "harness_arn", None) else {}
        runtime_days = {}
        for arn in _runtime_arns_of(record, harness_runtimes):
            for day, item in collected["resources"].get(arn, {}).items():
                runtime_days[(arn, day)] = item
        runtime_cost = _sum_days(runtime_days, "runtime_cost_micros")
        invocations = _sum_days(runtime_days, "invocations")

    # `reach` is present only for a gateway record, whose own turn/token counters
    # are always zero — it is called *through*, not run. Its Usage tab shows the
    # attaching agents and its per-tool calls instead. `tools` is the record's own
    # calls for an agent, and the gateway's rolled-up calls for a gateway.
    reach, tool_totals = _record_usage_view(usage, record_id, window)

    turns = usage.turn_events(
        window["start_date"], window["end_date"], agent_record_id=record_id
    )[-100:]
    turns.reverse()
    return {
        "record_id": record_id,
        **_window_fields(window),
        "sources": {"usage": usage.reads_complete()},
        "reach": reach,
        "totals": counters,
        "model_id": model_id,
        "models": models,
        "model_cost_micros": counters["model_cost_micros"] if counters["priced_turns"] > 0 else None,
        "runtime_cost_micros": runtime_cost,
        "invocations": invocations,
        "distinct_users": usage.distinct_users(
            record_id, window["start_date"], window["end_date"]
        ),
        "daily": usage.daily_totals(
            window["start_date"], window["end_date"], record_id=record_id
        ),
        "tools": [
            {"name": name, "tool_calls": count, "metric": "calls"}
            for name, count in sorted(
                tool_totals.items(),
                key=lambda pair: -pair[1],
            )
        ],
        "turns": [
            {
                "turn_id": event.get("turn_id"),
                "thread_id": event.get("thread_id"),
                "ended_at": event.get("ended_at"),
                "status": event.get("status"),
                "model_id": event.get("model_id"),
                "input_tokens": _int(event.get("input_tokens")) or 0,
                "output_tokens": _int(event.get("output_tokens")) or 0,
                "cache_read_tokens": _int(event.get("cache_read_tokens")) or 0,
                "cache_write_tokens": _int(event.get("cache_write_tokens")) or 0,
                "model_cost_micros": _int(event.get("model_cost_micros")),
                "measured": bool(event.get("measured")),
                "source": event.get("source"),
                # Only an admin sees other people's turns by owner.
                "owner_sub": event.get("owner_sub") if user.is_admin else None,
            }
            for event in turns
        ],
    }


@router.get("/layout")
def get_layout(
    user: AuthUser = Depends(current_user),
) -> Dict[str, Any]:
    """Retrieve the user's dashboard layout preference.

    A layout is readable in an environment with no usage table.
    The key always comes from the token's sub, never from the request.
    """
    return _layout().get(user.sub)


@router.put("/layout")
def put_layout(
    body: LayoutUpdateRequest,
    user: AuthUser = Depends(current_user),
) -> Dict[str, Any]:
    """Store the user's dashboard layout preference.

    The layout key always comes from the token's sub, never from the body.
    A junk body reconciles to a valid default rather than rejecting with 422.
    """
    # Convert the request body to a dict. Optional fields remain None if absent.
    layout_data = {}
    if body.version is not None:
        layout_data["version"] = body.version
    if body.widgets is not None:
        layout_data["widgets"] = body.widgets

    return _layout().put(user.sub, layout_data)


# --- model rate card ------------------------------------------------------------
#
# Admin-only, like every route that prices the whole platform. The card is read
# from DDB and the process overlay; only `/rates/fetch` makes a live call (Price
# List), and only when the admin presses the button. Writes reprice immediately —
# see `services/rate_card_service` for why the bill still has the last word.


def _admin_name(user: AuthUser) -> str:
    """Who registered a rate, for the entry's `source`. The token's username is
    the pool's UUID, so the directory's email is the readable name."""
    return _subjects([user.sub]).get(user.sub) or user.username or user.sub


@router.get("/rates")
def get_rate_card(
    days: int = Query(30),
    user: AuthUser = Depends(current_user),
) -> Dict[str, Any]:
    """The rate card for the models this platform ran, with provenance per figure,
    the bill's candidates for what is missing, and every registered entry."""
    _require_admin(user)
    _require_configured()
    window = _window(days)
    return {
        "window": _window_fields(window),
        **_rate_card().overview(window["start_date"], window["end_date"]),
    }


@router.post("/rates/fetch")
def fetch_published_rates(
    user: AuthUser = Depends(current_user),
) -> Dict[str, Any]:
    """Ask the Price List what it publishes for our families. Live call; the
    response is candidates, nothing is stored."""
    _require_admin(user)
    _require_configured()
    window = _window(30)
    overview = _rate_card().overview(window["start_date"], window["end_date"])
    families = sorted({row["family"] for row in overview["rows"]})
    try:
        return _rate_card().published(families)
    except PricingUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"Price List 를 읽을 수 없습니다: {exc}") from exc


@router.put("/rates")
def register_rates(
    body: RateRegisterRequest,
    user: AuthUser = Depends(current_user),
) -> Dict[str, Any]:
    """Store the submitted rates and reprice from the earliest effective date."""
    _require_admin(user)
    _require_configured()
    try:
        return _rate_card().register(
            [entry.model_dump() for entry in body.entries],
            by=_admin_name(user),
            today=clock.today(),
        )
    except RateCardError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/rates")
def delete_rate(
    key: str = Query(..., description="R#family|routing|tier|effective_from"),
    user: AuthUser = Depends(current_user),
) -> Dict[str, Any]:
    """Remove one registered entry and reprice the turns it covered."""
    _require_admin(user)
    _require_configured()
    try:
        return _rate_card().remove(key, today=clock.today())
    except RateCardError as exc:
        raise HTTPException(status_code=404 if "없습니다" in str(exc) else 400, detail=str(exc)) from exc
