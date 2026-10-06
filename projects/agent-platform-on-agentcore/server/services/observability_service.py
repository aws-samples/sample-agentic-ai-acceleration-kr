"""AgentCore Runtime vended USAGE_LOGS: enable them, and fold them per session.

**What this buys.** CloudWatch's vended metrics say what a *runtime* consumed per
day. The vended `USAGE_LOGS` say what each *session* consumed, at one-second
granularity — and AgentCore's session id is this platform's thread id (the
`runtimeSessionId` the server derives from it), so a usage log row is the
runtime cost of one conversation. That is the only route to per-thread and
per-user runtime cost; without it the runtime tier stays a per-agent total.

**Enabling is a delivery, per runtime.** CloudWatch Logs vended delivery needs a
delivery *source* naming the resource and log type, a delivery *destination*
(the log group, created by terraform), and a *delivery* joining them. All three
are idempotent here: describe first, create only what is missing. Harness
companion runtimes are runtimes, so `harness_service` calls `ensure_usage_logs`
once it knows the companion ARN, and the collector calls it for every runtime it
meters so a runtime deployed outside this server is covered too.

**Reading is one Logs Insights query per pass** over the business day so far,
grouped by session and runtime. The per-session item is a SET of that total, so
re-running never adds a slice twice. The docs put the vended lag at up to 60
minutes; `collected_at` is stamped so a reader can see how fresh a session is.

Everything is best-effort: a runtime whose delivery cannot be created is logged
and the page reports session attribution as unavailable, never as zero.
"""
import logging
import time
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable, Dict, List, Optional

from botocore.exceptions import ClientError

from core import clock
from repositories.usage_repository import month_shard

logger = logging.getLogger(__name__)

LOG_TYPE = "USAGE_LOGS"

_MICRO = Decimal(1_000_000)

# Session rows: the two consumption fields and the elapsed seconds summed per
# (session, runtime). The vended rows nest their fields — `attributes.session.id`,
# `attributes.time_elapsed_seconds`, `metrics.agent.runtime.*` — one row per
# second of session life (measured 2026-09-23; the docs list the leaf names only).
_SESSION_QUERY = (
    "stats sum(`metrics.agent.runtime.vcpu.hours.used`) as vcpu, "
    "sum(`metrics.agent.runtime.memory.gb_hours.used`) as gb, "
    "sum(`attributes.time_elapsed_seconds`) as elapsed "
    "by `attributes.session.id` as session_id, resource_arn"
)

# A keep-warm ping opens a session whose id AgentCore names `keepwarm-…`. It is
# runtime cost nobody's thread caused, so it is flagged: the page shows it as the
# price of staying warm and the per-user attribution leaves it out.
_KEEPWARM_PREFIX = "keepwarm-"


def _micro(value: Any) -> int:
    return int((Decimal(str(value)) * _MICRO).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _usd_micros(quantity: Any, usd_per_unit: Any) -> int:
    return int(
        (Decimal(str(quantity)) * Decimal(str(usd_per_unit)) * _MICRO).quantize(
            Decimal(1), rounding=ROUND_HALF_UP
        )
    )


class ObservabilityService:
    """Vended USAGE_LOGS delivery and per-session usage collection."""

    def __init__(
        self,
        *,
        logs: Any,
        log_group: str,
        destination_arn: str,
        project: str,
        repository: Any,
        runtime_rates: Callable[[], Dict[str, Any]],
        query_timeout_seconds: float = 25.0,
    ):
        self.logs = logs
        self.log_group = log_group
        self.destination_arn = destination_arn
        self.project = project
        self.repository = repository
        self.runtime_rates = runtime_rates
        self.query_timeout_seconds = query_timeout_seconds
        # Per-process memo so the collector does not describe every source on
        # every pass. Reset on failure so a transient error is retried.
        self._ensured: Dict[str, str] = {}
        # The log group's creation instant, read once. Logs Insights rejects a
        # window that ends before it (MalformedQueryException), so days before the
        # group existed are skipped and the creation day is queried from then.
        self._created_at: Optional[datetime] = None

    @property
    def enabled(self) -> bool:
        return bool(self.log_group and self.destination_arn)

    # --- delivery --------------------------------------------------------------

    def source_name(self, runtime_arn: str) -> str:
        runtime_id = runtime_arn.rsplit("/", 1)[-1]
        return f"{self.project}-usage-{runtime_id}"

    def ensure_usage_logs(self, runtime_arn: str) -> str:
        """Make sure `runtime_arn` delivers USAGE_LOGS to our log group.

        Returns `"off"` (no destination configured), `"exists"`, `"created"`,
        `"unsupported"` (the service rejected the log type for this resource) or
        `"failed"`. Never raises: this runs from harness creation and from the
        collector, and neither may die on an observability setting.
        """
        if not self.enabled or not runtime_arn:
            return "off"
        memo = self._ensured.get(runtime_arn)
        if memo in ("exists", "created"):
            return "exists"

        name = self.source_name(runtime_arn)
        try:
            existing = {
                source.get("name")
                for source in self.logs.describe_delivery_sources().get("deliverySources", [])
            }
            created = False
            if name not in existing:
                self.logs.put_delivery_source(
                    name=name, resourceArn=runtime_arn, logType=LOG_TYPE
                )
                created = True
            delivered = {
                delivery.get("deliverySourceName")
                for delivery in self.logs.describe_deliveries().get("deliveries", [])
            }
            if name not in delivered:
                self.logs.create_delivery(
                    deliverySourceName=name, deliveryDestinationArn=self.destination_arn
                )
                created = True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("ValidationException", "InvalidParameterException"):
                logger.warning("USAGE_LOGS not accepted for %s: %s", runtime_arn, exc)
                self._ensured[runtime_arn] = "unsupported"
                return "unsupported"
            if code in ("ConflictException", "ResourceAlreadyExistsException"):
                self._ensured[runtime_arn] = "exists"
                return "exists"
            logger.warning("USAGE_LOGS delivery for %s failed", runtime_arn, exc_info=True)
            return "failed"
        except Exception:
            logger.warning("USAGE_LOGS delivery for %s failed", runtime_arn, exc_info=True)
            return "failed"

        status = "created" if created else "exists"
        self._ensured[runtime_arn] = status
        return status

    # --- sessions --------------------------------------------------------------

    def _run_query(self, start: datetime, end: datetime) -> List[Dict[str, str]]:
        started = self.logs.start_query(
            logGroupName=self.log_group,
            startTime=int(start.timestamp()),
            endTime=int(end.timestamp()),
            queryString=_SESSION_QUERY,
        )
        query_id = started["queryId"]
        deadline = time.monotonic() + self.query_timeout_seconds
        while True:
            response = self.logs.get_query_results(queryId=query_id)
            status = response.get("status")
            if status == "Complete":
                return [
                    {cell["field"]: cell["value"] for cell in row if "field" in cell}
                    for row in response.get("results", [])
                ]
            if status in ("Failed", "Cancelled", "Timeout"):
                raise RuntimeError(f"Logs Insights query {status}")
            if time.monotonic() > deadline:
                raise TimeoutError("Logs Insights query did not finish")
            time.sleep(0.5)

    def _log_group_created_at(self) -> Optional[datetime]:
        if self._created_at is not None:
            return self._created_at
        try:
            groups = self.logs.describe_log_groups(logGroupNamePrefix=self.log_group).get("logGroups", [])
            for group in groups:
                if group.get("logGroupName") == self.log_group and group.get("creationTime"):
                    self._created_at = datetime.fromtimestamp(int(group["creationTime"]) / 1000, tz=timezone.utc)
                    return self._created_at
        except Exception:
            logger.info("Could not read the usage log group's creation time", exc_info=True)
        return None

    def collect_sessions(self, day: str) -> int:
        """SET each session's running total for `day`. Returns items written.

        The window is the business day from 00:00 to now (or to its end for a
        past day), so every pass writes the day's whole total for a session and
        the item never accumulates slices.
        """
        if not self.enabled:
            return 0
        start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=clock.ZONE)
        end = min(start + timedelta(days=1), clock.now())
        created = self._log_group_created_at()
        if created is not None:
            if end <= created:
                return 0
            start = max(start, created)
        try:
            rows = self._run_query(start, end)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "MalformedQueryException":
                logger.info("Session query for %s rejected: %s", day, exc)
                return 0
            raise
        rates = self.runtime_rates()
        shard = month_shard(day)
        collected_at = clock.now().isoformat()
        written = 0
        for row in rows:
            session_id = row.get("session_id") or row.get("session.id") or ""
            runtime_arn = row.get("resource_arn") or ""
            if not session_id or not runtime_arn:
                continue
            vcpu = float(row.get("vcpu") or 0.0)
            gb = float(row.get("gb") or 0.0)
            fields: Dict[str, Any] = {
                "vcpu_hours_micro": _micro(vcpu),
                "gb_hours_micro": _micro(gb),
                "elapsed_seconds": int(float(row.get("elapsed") or 0)),
                "keepwarm": session_id.startswith(_KEEPWARM_PREFIX),
                "collected_at": collected_at,
                "complete": end < clock.now() and day < clock.date_of(clock.now()),
            }
            if rates.get("vcpu_hour") is not None and rates.get("gb_hour") is not None:
                fields["runtime_cost_micros"] = _usd_micros(vcpu, rates["vcpu_hour"]) + _usd_micros(
                    gb, rates["gb_hour"]
                )
                fields["rate_source"] = rates.get("source", "unknown")
            self.repository.set_fields(
                f"SESSIONS#{shard}", f"D#{day}#S#{session_id}#R#{runtime_arn}", fields
            )
            written += 1
        return written
