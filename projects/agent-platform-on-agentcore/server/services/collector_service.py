"""Background collector: CloudWatch quantities × published rates, per day, into DDB.

**Why a collector and not a read-through.** The dashboard polls `/summary` once a
minute, and `GetMetricData` bills per metric requested — so the runtime tier used
to live behind a click, and the page's headline cost was the model estimate while
the runtime bill was four times larger. Collecting on a fixed cadence into the
usage table makes the runtime figure free to read and a few minutes old at most
(CloudWatch's own vended lag is up to 60 minutes; `collected_at` says when).

**Per day, because that is how the bill is cut.** Every query uses `Period=86400`
aligned to the business calendar's day (`core.clock`). Measured 2026-09-23 on the
live account: per-runtime daily `MemoryUsed-GBHours` from CloudWatch matched the
Cost Explorer `UsageQuantity` billed to the same agent to three decimals on every
one of 30 days (59.659 vs 59.678 GB-h, 0.300 vs 0.300 vCPU-h). The rate is the
Price List's, verified exact against the bill by `billing_service`. So this is
not an estimate: it is the invoice arithmetic, run early.

**SET, never ADD.** A day is re-read on every pass until it is complete, and the
last reading wins. Running twice cannot double anything. Today's item carries
`complete: false` so a reader knows it is still moving.

**A missing series is left missing.** Writing 0 for an agent CloudWatch has no
series for would price it as free; absence renders as "—".

Gateway invocations and memory events/retrievals are priced from the published
rate card too. Long-term memory *storage* is billed per stored-record-hour, which
no metric counts, so that slice stays with Cost Explorer and the item says so
(`storage_priced: false`).
"""
import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, Iterable, List, Optional

from core import clock
from repositories.usage_repository import month_shard
from services.usage_service import month_shards

logger = logging.getLogger(__name__)

NAMESPACE = "AWS/Bedrock-AgentCore"
_MAX_QUERIES = 500
_DAY = 86400

# The runtime series worth a day item, in two dimension sets. CloudWatch keys
# resource usage by (Service, Resource) and invocation counts by
# (Name=<runtime>::<endpoint>, Operation, Resource); an inexact set returns no
# datapoints at all, which is how every row once read `invocations: None`.
_USAGE_METRICS = ("CPUUsed-vCPUHours", "MemoryUsed-GBHours")
_OPERATION_METRICS = ("Invocations", "SystemErrors", "UserErrors")
_RUNTIME_METRICS = _USAGE_METRICS + _OPERATION_METRICS
_RUNTIME_FIELD = {
    "Invocations": "invocations",
    "SystemErrors": "system_errors",
    "UserErrors": "user_errors",
}
_DEFAULT_ENDPOINT = "DEFAULT"

# Price List usagetype suffixes for the per-call components. The prefix is the
# billing region and not constructible from the region code, so these are
# matched on the suffix — the same reasoning as `pricing_service._rate_for`.
_GATEWAY_INVOCATION_SUFFIX = "Gateway:Consumption-based:API-Invocations"
# The MCP methods the gateway bill counts as API invocations. The session
# handshake (`initialize`, `notifications/initialized`) is metered but not billed.
_GATEWAY_BILLABLE_METHODS = ("tools/list", "tools/call")
_MEMORY_EVENT_SUFFIX = "Memory:Consumption-based:Short-Term-Memory"
_MEMORY_RETRIEVAL_SUFFIX = "Memory:Consumption-based:Long-Term-Memory-Retrieval"

_MICRO = Decimal(1_000_000)


def _micro(value: Any) -> int:
    """A float quantity or dollar amount as an integer of millionths, half-up."""
    return int((Decimal(str(value)) * _MICRO).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _usd_micros(quantity: Any, usd_per_unit: Any) -> int:
    return int(
        (Decimal(str(quantity)) * Decimal(str(usd_per_unit)) * _MICRO).quantize(
            Decimal(1), rounding=ROUND_HALF_UP
        )
    )


class CollectorService:
    """Reads vended AgentCore quantities per day and writes priced day items."""

    def __init__(
        self,
        *,
        repository: Any,
        cloudwatch: Any,
        pricing: Any,
        registry: Any,
        harness: Any,
        region: str,
        lease_seconds: int = 240,
    ):
        self.repository = repository
        self.cloudwatch = cloudwatch
        self.pricing = pricing
        self.registry = registry
        self.harness = harness
        self.region = region
        self.lease_seconds = lease_seconds
        self._holder = uuid.uuid4().hex
        # Set by `run_forever`'s owner to run the six-hourly reconciliation.
        self.reconciler: Optional[Any] = None
        # The ledger, for two things only the collector's cadence can do: refresh
        # the learned-rate overlay on every replica, and reprice turns after the
        # six-hourly reconciliation has learned a rate. Optional, like `billing`.
        self.usage: Optional[Any] = None
        # `ObservabilityService.collect_sessions(day) -> int` and
        # `ObservabilityService.ensure_usage_logs(arn) -> str`, when USAGE_LOGS
        # delivery is configured. Both None keeps the collector metrics-only.
        self.session_collector: Optional[Any] = None
        self.usage_logs_ensurer: Optional[Any] = None
        # `BillingService`, for the six-hourly reconciliation (`reconcile`).
        self.billing: Optional[Any] = None

    # --- CloudWatch ----------------------------------------------------------

    @staticmethod
    def _day_bounds(day: str):
        start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=clock.ZONE)
        return start, start + timedelta(days=1)

    def _sums(
        self, day: str, specs: List[Dict[str, Any]]
    ) -> Dict[str, float]:
        """`specs` = [{"id", "metric", "dimensions"}]. Returns id -> day sum for the
        ids that had any datapoint; ids CloudWatch had nothing for are absent."""
        start, end = self._day_bounds(day)
        queries = [
            {
                "Id": spec["id"],
                "ReturnData": True,
                "MetricStat": {
                    "Metric": {
                        "Namespace": NAMESPACE,
                        "MetricName": spec["metric"],
                        "Dimensions": spec["dimensions"],
                    },
                    "Period": _DAY,
                    "Stat": "Sum",
                },
            }
            for spec in specs
        ]
        values: Dict[str, float] = {}
        for offset in range(0, len(queries), _MAX_QUERIES):
            response = self.cloudwatch.get_metric_data(
                MetricDataQueries=queries[offset : offset + _MAX_QUERIES],
                StartTime=start,
                EndTime=end,
            )
            for result in response.get("MetricDataResults", []):
                points = result.get("Values") or []
                if points:
                    values[result["Id"]] = float(sum(float(v) for v in points))
        return values

    def _rate(self, card: Dict[str, Dict[str, Any]], suffix: str) -> Optional[float]:
        for usage_type, entry in card.items():
            if usage_type.endswith(suffix):
                return float(entry["usd"])
        return None

    def _card(self) -> Dict[str, Dict[str, Any]]:
        try:
            return self.pricing.rate_card()
        except Exception:
            logger.info("Price List unavailable for the per-call components", exc_info=True)
            return {}

    # --- runtimes ------------------------------------------------------------

    def collect_day(self, day: str, arns: List[str], *, today: str) -> Dict[str, Any]:
        """One day of runtime quantities for `arns`, priced, SET into RESOURCES#."""
        specs = []
        index: Dict[str, tuple] = {}
        for i, arn in enumerate(arns):
            for j, metric in enumerate(_RUNTIME_METRICS):
                qid = f"r{i}_{j}"
                index[qid] = (arn, metric)
                if metric in _USAGE_METRICS:
                    dimensions = [
                        {"Name": "Service", "Value": "AgentCore.Runtime"},
                        {"Name": "Resource", "Value": arn},
                    ]
                else:
                    dimensions = [
                        {"Name": "Name", "Value": f"{self.runtime_name(arn, strip_harness=False)}::{_DEFAULT_ENDPOINT}"},
                        {"Name": "Operation", "Value": "InvokeAgentRuntime"},
                        {"Name": "Resource", "Value": arn},
                    ]
                specs.append({"id": qid, "metric": metric, "dimensions": dimensions})
        if not specs:
            return {"day": day, "written": 0}
        values = self._sums(day, specs)
        rates = self.pricing.runtime_rates()

        by_arn: Dict[str, Dict[str, float]] = {}
        for qid, (arn, metric) in index.items():
            if qid in values:
                by_arn.setdefault(arn, {})[metric] = values[qid]

        shard = month_shard(day)
        written = 0
        collected_at = clock.now().isoformat()
        for arn, metrics in by_arn.items():
            fields: Dict[str, Any] = {
                "collected_at": collected_at,
                "complete": day < today,
                "rate_source": rates["source"],
                "rate_vcpu_hour_micro": _micro(rates["vcpu_hour"]),
                "rate_gb_hour_micro": _micro(rates["gb_hour"]),
            }
            if "CPUUsed-vCPUHours" in metrics:
                fields["vcpu_hours_micro"] = _micro(metrics["CPUUsed-vCPUHours"])
            if "MemoryUsed-GBHours" in metrics:
                fields["gb_hours_micro"] = _micro(metrics["MemoryUsed-GBHours"])
            for metric, field in _RUNTIME_FIELD.items():
                if metric in metrics:
                    fields[field] = int(round(metrics[metric]))
            if "vcpu_hours_micro" in fields or "gb_hours_micro" in fields:
                # Both halves of the published formula: CPU only while consumed,
                # memory for every second the microVM lived. Absent half = 0 here
                # because CloudWatch omits a series that summed to nothing.
                fields["runtime_cost_micros"] = _usd_micros(
                    metrics.get("CPUUsed-vCPUHours", 0.0), rates["vcpu_hour"]
                ) + _usd_micros(metrics.get("MemoryUsed-GBHours", 0.0), rates["gb_hour"])
            self.repository.set_fields(f"RESOURCES#{shard}", f"D#{day}#R#{arn}", fields)
            written += 1
        return {"day": day, "written": written}

    # --- gateways ------------------------------------------------------------

    def collect_gateways_day(self, day: str, arns: List[str], *, today: str) -> Dict[str, Any]:
        """Gateway `InvokeGateway` calls per day, priced per published invocation rate.

        Only the tool requests are priced. Every MCP session opens with two
        handshake requests (`initialize`, `notifications/initialized`) that the
        `Invocations` metric counts like any other and the bill does not: on
        2026-09-20 the metric read 470 requests for the day, of which 264 were
        handshake, against 162 billed API invocations. `invocations` keeps the
        full metric count for the reader who wants traffic; `billable_requests`
        is what the rate is applied to.
        """
        specs: List[Dict[str, Any]] = []
        for i, arn in enumerate(arns):
            specs.append({
                "id": f"g{i}",
                "metric": "Invocations",
                "dimensions": [
                    {"Name": "Operation", "Value": "InvokeGateway"},
                    {"Name": "Protocol", "Value": "MCP"},
                    {"Name": "Resource", "Value": arn},
                ],
            })
            for j, method in enumerate(_GATEWAY_BILLABLE_METHODS):
                specs.append({
                    "id": f"g{i}m{j}",
                    "metric": "Invocations",
                    "dimensions": [
                        {"Name": "Method", "Value": method},
                        {"Name": "Operation", "Value": "InvokeGateway"},
                        {"Name": "Protocol", "Value": "MCP"},
                        {"Name": "Resource", "Value": arn},
                    ],
                })
        if not specs:
            return {"day": day, "written": 0}
        values = self._sums(day, specs)
        rate = self._rate(self._card(), _GATEWAY_INVOCATION_SUFFIX)
        shard = month_shard(day)
        collected_at = clock.now().isoformat()
        written = 0
        for i, arn in enumerate(arns):
            if f"g{i}" not in values:
                continue
            invocations = int(round(values[f"g{i}"]))
            billable = sum(
                int(round(values.get(f"g{i}m{j}", 0.0))) for j in range(len(_GATEWAY_BILLABLE_METHODS))
            )
            fields: Dict[str, Any] = {
                "invocations": invocations,
                "billable_requests": billable,
                "collected_at": collected_at,
                "complete": day < today,
                "rate_source": "price_list" if rate is not None else "unavailable",
            }
            if rate is not None:
                fields["gateway_cost_micros"] = _usd_micros(billable, rate)
                fields["rate_invocation_micro"] = _micro(rate)
            self.repository.set_fields(f"GATEWAYS#{shard}", f"D#{day}#G#{arn}", fields)
            written += 1
        return {"day": day, "written": written}

    # --- memories ------------------------------------------------------------

    def collect_memories_day(self, day: str, arns: List[str], *, today: str) -> Dict[str, Any]:
        """Short-term events created and long-term retrievals per memory, priced.

        Storage (per stored-record-hour) has no metric and is left to the bill —
        `storage_priced: false` is what tells the reader the figure is partial.
        """
        specs = []
        for i, arn in enumerate(arns):
            specs.append({
                "id": f"me{i}",
                "metric": "CreationCount",
                "dimensions": [
                    {"Name": "ItemType", "Value": "Event"},
                    {"Name": "Resource", "Value": arn},
                ],
            })
            specs.append({
                "id": f"mr{i}",
                "metric": "Invocations",
                "dimensions": [
                    {"Name": "Operation", "Value": "RetrieveMemoryRecords"},
                    {"Name": "Resource", "Value": arn},
                ],
            })
        if not specs:
            return {"day": day, "written": 0}
        values = self._sums(day, specs)
        card = self._card()
        event_rate = self._rate(card, _MEMORY_EVENT_SUFFIX)
        retrieval_rate = self._rate(card, _MEMORY_RETRIEVAL_SUFFIX)
        shard = month_shard(day)
        collected_at = clock.now().isoformat()
        written = 0
        for i, arn in enumerate(arns):
            events = values.get(f"me{i}")
            retrievals = values.get(f"mr{i}")
            if events is None and retrievals is None:
                continue
            fields: Dict[str, Any] = {
                "collected_at": collected_at,
                "complete": day < today,
                "storage_priced": False,
            }
            cost = 0
            priced = True
            if events is not None:
                fields["events"] = int(round(events))
                if event_rate is None:
                    priced = False
                else:
                    cost += _usd_micros(fields["events"], event_rate)
            if retrievals is not None:
                fields["retrievals"] = int(round(retrievals))
                if retrieval_rate is None:
                    priced = False
                else:
                    cost += _usd_micros(fields["retrievals"], retrieval_rate)
            if priced:
                fields["memory_cost_micros"] = cost
            self.repository.set_fields(f"MEMORIES#{shard}", f"D#{day}#M#{arn}", fields)
            written += 1
        return {"day": day, "written": written}

    # --- reads ---------------------------------------------------------------

    def _grouped(self, prefix: str, marker: str, start_date: str, end_date: str) -> Dict[str, Dict[str, Dict[str, Any]]]:
        out: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for shard in month_shards(start_date, end_date):
            try:
                items = self.repository.query_prefix(f"{prefix}{shard}", "D#")
            except Exception:
                logger.warning("Collector read failed: %s%s", prefix, shard, exc_info=True)
                continue
            for item in items:
                sort_key = str(item.get("sk", ""))
                head, sep, key = sort_key.partition(marker)
                if not sep or not head.startswith("D#"):
                    continue
                day = head[2:]
                if not (start_date <= day <= end_date):
                    continue
                out.setdefault(key, {})[day] = item
        return out

    def resources(self, start_date: str, end_date: str) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """runtime_arn -> day -> item."""
        return self._grouped("RESOURCES#", "#R#", start_date, end_date)

    def gateways(self, start_date: str, end_date: str) -> Dict[str, Dict[str, Dict[str, Any]]]:
        return self._grouped("GATEWAYS#", "#G#", start_date, end_date)

    def memories(self, start_date: str, end_date: str) -> Dict[str, Dict[str, Dict[str, Any]]]:
        return self._grouped("MEMORIES#", "#M#", start_date, end_date)

    def sessions(self, start_date: str, end_date: str) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """session_id -> day -> item (one per runtime the session touched, keyed
        `{session}#R#{arn}` — the caller folds)."""
        return self._grouped("SESSIONS#", "#S#", start_date, end_date)

    # --- naming ----------------------------------------------------------------

    @staticmethod
    def runtime_name(arn: str, *, strip_harness: bool = False) -> str:
        """`…runtime/bap_default-AbCdE12345` -> `bap_default`.

        The id suffix after the last `-` is AgentCore's; the rest is the name the
        runtime was launched with. A harness's companion runtime is named
        `harness_<harness name>`, so `strip_harness` yields the harness name — the
        one the AgentName cost tag carries.
        """
        runtime_id = arn.rsplit("/", 1)[-1]
        name = runtime_id.rsplit("-", 1)[0] if "-" in runtime_id else runtime_id
        if strip_harness and name.startswith("harness_"):
            name = name[len("harness_"):]
        return name

    def agent_name_for(self, arn: str, names: Dict[str, str]) -> str:
        """The AgentName a runtime's cost is billed under, with a fallback for
        runtimes the registry no longer knows (a recreated harness's old companion,
        a redeployed runtime's previous id)."""
        return names.get(arn) or self.runtime_name(arn, strip_harness=True)

    # --- targets -------------------------------------------------------------

    def runtime_arns(self, known: Iterable[str] = ()) -> List[str]:
        """Every runtime this stack should meter: registry records' runtime ARNs,
        harness companion runtimes, and anything already in this month's items
        (so a deleted runtime keeps its history in the window)."""
        arns = set(known)
        try:
            for record in self.registry.agent_records():
                if getattr(record, "agent_runtime_arn", None):
                    arns.add(record.agent_runtime_arn)
        except Exception:
            logger.warning("Collector could not list registry records", exc_info=True)
        try:
            for harness in self.harness.list_harnesses():
                if getattr(harness, "runtime_arn", None):
                    arns.add(harness.runtime_arn)
        except Exception:
            logger.warning("Collector could not list harnesses", exc_info=True)
        return sorted(arn for arn in arns if ":runtime/" in arn)

    def gateway_arns(self) -> List[str]:
        from services.registry_service import gateway_arn_of

        arns = set()
        try:
            for summary in self.registry.list_records(descriptor_type="MCP"):
                try:
                    detail = self.registry.get_record(summary.record_id)
                except Exception:
                    continue
                arn = gateway_arn_of(getattr(detail, "descriptor_content", None))
                if arn:
                    arns.add(arn)
        except Exception:
            logger.warning("Collector could not list gateway records", exc_info=True)
        return sorted(arns)

    def memory_arns(self) -> List[str]:
        arns = set()
        try:
            for harness in self.harness.list_harnesses():
                memory = getattr(harness, "memory", None)
                arn = getattr(memory, "arn", None) if memory else None
                if arn:
                    arns.add(arn)
        except Exception:
            logger.warning("Collector could not list harness memories", exc_info=True)
        return sorted(arns)

    # --- orchestration -------------------------------------------------------

    def run_once(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Yesterday and today for every target. Yesterday again because CloudWatch
        vends resource usage up to an hour late, so the last hour of a day lands
        after midnight."""
        now = now or clock.now()
        today = clock.date_of(now)
        yesterday = clock.date_of(now - timedelta(days=1))
        known = set()
        for arn_map in (self.resources(yesterday, today),):
            known.update(arn_map)
        arns = self.runtime_arns(known)
        gateways = self.gateway_arns()
        memories = self.memory_arns()
        report: Dict[str, Any] = {"today": today, "runtimes": len(arns), "gateways": len(gateways), "memories": len(memories)}
        if self.usage_logs_ensurer is not None:
            statuses: Dict[str, int] = {}
            for arn in arns:
                try:
                    status = self.usage_logs_ensurer(arn)
                except Exception:
                    status = "failed"
                statuses[status] = statuses.get(status, 0) + 1
            report["usage_logs"] = statuses
        for day in (yesterday, today):
            report[day] = {
                "runtime": self.collect_day(day, arns, today=today)["written"],
                "gateway": self.collect_gateways_day(day, gateways, today=today)["written"],
                "memory": self.collect_memories_day(day, memories, today=today)["written"],
            }
        if self.session_collector is not None:
            for day in (yesterday, today):
                try:
                    report[day]["sessions"] = self.session_collector(day)
                except Exception:
                    logger.warning("Session usage collection failed for %s", day, exc_info=True)
        return report

    # --- reconciliation inputs -------------------------------------------------

    def agent_name_by_arn(self) -> Dict[str, str]:
        """runtime ARN -> the name the AgentName cost tag carries.

        Registry record names are the harness/runtime names the deploy path tags
        with, and a harness companion runtime is tagged with the harness name.
        """
        names: Dict[str, str] = {}
        try:
            for record in self.registry.agent_records():
                name = getattr(record, "name", None)
                arn = getattr(record, "agent_runtime_arn", None)
                if name and arn:
                    names.setdefault(arn, name)
        except Exception:
            logger.warning("Could not read registry names", exc_info=True)
        try:
            for harness in self.harness.list_harnesses():
                arn = getattr(harness, "runtime_arn", None)
                name = getattr(harness, "harness_name", None)
                if arn and name:
                    names.setdefault(arn, name)
        except Exception:
            logger.warning("Could not read harness names", exc_info=True)
        return names

    def ledger_inputs(self, start_date: str, end_date: str):
        """What `BillingService.reconcile` compares the bill against.

        Returns `(agent_days, components)`: `agent_days[(agent_name, day)]` holds
        the collector's runtime figures for that day, and `components` sums our
        priced quantities per component over the window. Zero is a real figure
        here (a gateway that was called but priced at $0.000) — the collector only
        writes items for series that exist.
        """
        names = self.agent_name_by_arn()
        agent_days: Dict[tuple, Dict[str, int]] = {}
        components: Dict[str, int] = {}
        for arn, days in self.resources(start_date, end_date).items():
            name = self.agent_name_for(arn, names)
            for day, item in days.items():
                bucket = agent_days.setdefault((name, day), {})
                for key in ("runtime_cost_micros", "gb_hours_micro", "vcpu_hours_micro"):
                    if key in item:
                        bucket[key] = bucket.get(key, 0) + int(item[key])
                if "runtime_cost_micros" in item:
                    components["runtime"] = components.get("runtime", 0) + int(item["runtime_cost_micros"])
        for days in self.gateways(start_date, end_date).values():
            for item in days.values():
                if "gateway_cost_micros" in item:
                    components["gateway"] = components.get("gateway", 0) + int(item["gateway_cost_micros"])
        for days in self.memories(start_date, end_date).values():
            for item in days.values():
                if "memory_cost_micros" in item:
                    components["memory"] = components.get("memory", 0) + int(item["memory_cost_micros"])
        return agent_days, components

    def reconcile(self, now: Optional[datetime] = None, days: int = 30) -> Dict[str, Any]:
        """The six-hourly bill check over the dashboard's widest window."""
        if self.billing is None:
            return {}
        now = now or clock.now()
        end_date = clock.date_of(now)
        start_date = clock.date_of(now - timedelta(days=days - 1))
        agent_days, components = self.ledger_inputs(start_date, end_date)
        summary = self.billing.reconcile(
            start_date, end_date, repository=self.repository,
            ledger_agent_days=agent_days, ledger_components=components,
        )
        # A rate learned just now belongs to turns already written — the days
        # it was observed on, and any turn on that model since. Reload, then
        # bring the window's events to the rates in force on their days.
        if self.usage is not None:
            try:
                self.usage.load_learned_rates()
                summary["repriced"] = self.usage.reprice_events(start_date, end_date)
            except Exception:
                logger.warning("Repricing after reconciliation failed", exc_info=True)
        return summary

    def _acquire_lease(self, now: datetime) -> bool:
        """One collector per stack. A second server task sees a live lease and
        skips; a crashed holder's lease expires on its own."""
        pk, sk = "COLLECTOR#lease", "L"
        expires = (now + timedelta(seconds=self.lease_seconds)).isoformat()
        try:
            if self.repository.put_if_absent(pk, sk, {"holder": self._holder, "expires_at": expires}):
                return True
            current = self.repository.get(pk, sk) or {}
            if current.get("holder") == self._holder or str(current.get("expires_at", "")) < now.isoformat():
                self.repository.set_fields(pk, sk, {"holder": self._holder, "expires_at": expires})
                return True
            return False
        except Exception:
            logger.warning("Collector lease check failed; running anyway", exc_info=True)
            return True

    async def run_forever(self, interval_seconds: int) -> None:
        """The background loop. Nothing raised here may stop it."""
        reconciled_at: Optional[datetime] = None
        while True:
            try:
                now = clock.now()
                # Every replica, lease or not: a rate the lease holder learned
                # must reach the replica that prices the next turn.
                if self.usage is not None:
                    try:
                        await asyncio.to_thread(self.usage.load_learned_rates)
                    except Exception:
                        logger.warning("Learned rate refresh failed", exc_info=True)
                if self._acquire_lease(now):
                    report = await asyncio.to_thread(self.run_once, now)
                    logger.info("collector: %s", report)
                    if self.reconciler is not None and (
                        reconciled_at is None or now - reconciled_at >= timedelta(hours=6)
                    ):
                        try:
                            await asyncio.to_thread(self.reconciler, now)
                            reconciled_at = now
                        except Exception:
                            logger.warning("Reconciliation failed", exc_info=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Collector pass failed", exc_info=True)
            await asyncio.sleep(interval_seconds)
