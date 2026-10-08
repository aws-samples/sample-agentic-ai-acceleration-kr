"""CloudWatch reads for the insights dashboard.

**Dimension sets are discovered, never declared.** Measured 2026-08-16 against
the live account: `Invocations` for a runtime that had 7 invocations that day
returned *zero* datapoints when queried with `{Operation, Resource}`, and
returned the 7 when `{Name}` was added. CloudWatch treats each dimension
combination as its own series and the partial rollups are not populated here, so
a table of dimension names in a design document produces a permanently empty
dashboard with no error to notice. Everything below follows from that:

1. Enumerate the namespace once with `ListMetrics` and index each series by
   **every ARN appearing anywhere in its dimensions** — `Resource` and
   `HarnessId` both, in both the bare and ARN forms `HarnessId` takes.
2. Build `GetMetricData` from the discovered dimension sets **verbatim**, in one
   batched call (chunked at the API's 500-query ceiling). Never a fan-out per
   agent: parallel per-agent calls silently serialise behind botocore's
   connection pool, which is the trap that made registry listing slow.

**Every read here is metered, so the module counts its own queries.**
`GetMetricData` is billed at $0.01 per 1,000 *metrics requested*, not per call,
and batching does not reduce that count. Measured against the live account
2026-08-16, with nine agents in the registry: their 12 ARNs matched **208
discovered series** across the eight metrics then swept — 17 queries per ARN,
because every dimension set mentioning an ARN was probed on every sweep — plus 71
account-wide series for sessions and Memory tokens. 279 metrics per read, on a
one-minute poll, uncached: $0.0028 a request and $4.02 a day for one open tab,
times the number of tabs. Three things bring it down, and all three are
correctness arguments as much as cost ones:

* **Results are cached** (`cache_seconds`), keyed by the ARN set and a quantised
  window. Only the discovery sweep was cached before, which read like a cache
  and charged like none.
* **The winning dimension set is remembered** (`memo_seconds`), so a warm sweep
  probes one series per (agent, metric) instead of every candidate. Which
  rollups CloudWatch populates is a property of the namespace, not of the
  window, so this is re-derived on a timer rather than per request — and a memo
  that comes back empty is dropped, so a structural change re-probes.
* **Only metrics something renders are queried.** `Sessions` and `Throttles`
  were swept and never displayed.

Measured again after all three: 176 metrics on a cold sweep, **72 once the memo
is warm**, 0 inside the cache window. $0.0007 for a read a reader asked for,
against $4.02 a day for reads nobody asked for.

**One bucket per window, not one per day.** `Period` is the whole window, so
each query returns a single datapoint: a `Sum` is the window's sum either way,
but `p90` becomes the window's actual p90. Reducing daily p90 buckets with
`max()` — which is what this did — yields the worst day's p90, a different and
smaller-sample statistic that was being labelled as the window's. Verified
against the live account at both 604800s and 2592000s periods.

`usage_service` deliberately does not import this module. The two sources fail
independently and the merge belongs in the route, so a CloudWatch outage cannot
blank out figures we hold ourselves.
"""
import logging
import math
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import boto3

from core.config import AWS_REGION

logger = logging.getLogger(__name__)

NAMESPACE = "AWS/Bedrock-AgentCore"

# GetMetricData accepts at most 500 queries per call.
_MAX_QUERIES = 500

# Per-agent series and how to reduce them. Latency is a percentile because a
# mean hides the tail that makes a 90-second turn worth investigating.
#
# Nothing is swept "in case it is useful later": each entry is billed on every
# sweep, so a metric earns its place by being rendered or by feeding a figure
# that is. `Sessions` and `Throttles` were here and reached no pixel.
_AGENT_METRICS = (
    ("Invocations", "Sum", "invocations"),
    ("Latency", "p90", "latency_p90_ms"),
    ("SystemErrors", "Sum", "system_errors"),
    ("UserErrors", "Sum", "user_errors"),
    ("CPUUsed-vCPUHours", "Sum", "vcpu_hours"),
    ("MemoryUsed-GBHours", "Sum", "gb_hours"),
)

# The error rate's numerator and denominator. A ratio between series discovered
# at different dimension widths compares two different populations.
_ERROR_FIELDS = ("system_errors", "user_errors")


class TelemetryUnavailable(Exception):
    """CloudWatch could not be read — no permission, or nothing vended yet.

    The routes turn this into `sources.cloudwatch: false` and a collapsed card,
    never a 5xx: our own figures are still correct and still worth showing.
    """


class TelemetryService:
    """Vended AgentCore metrics, discovered then batched."""

    def __init__(
        self,
        region_name: Optional[str] = None,
        cache_seconds: int = 300,
        memo_seconds: int = 3600,
    ):
        self.region_name = region_name or AWS_REGION
        self._client = None
        self._cache_seconds = cache_seconds
        # The memo outlives the result cache on purpose: it records which
        # rollups CloudWatch populates, which changes when an agent is created
        # or retired, not when the window moves.
        self._memo_seconds = memo_seconds
        self._series: Optional[List[Dict[str, Any]]] = None
        self._series_at = 0.0
        self._index: Optional[Dict[str, List[Dict[str, Any]]]] = None
        self._index_at = 0.0
        # (arns, window) -> (read at, metrics). Bounded by the two windows the
        # UI offers times the agents in the registry, so it is not evicted.
        self._metrics: Dict[Any, Tuple[float, Dict[str, Dict[str, Any]]]] = {}
        # (arn, field) -> (memoised at, the dimension set that answered)
        self._winners: Dict[Tuple[str, str], Tuple[float, Tuple[Any, ...]]] = {}
        self._lock = threading.Lock()

    @property
    def client(self):
        if self._client is None:
            with self._lock:
                if self._client is None:
                    self._client = boto3.client(
                        "cloudwatch", region_name=self.region_name
                    )
        return self._client

    # --- discovery ----------------------------------------------------------

    def _all_series(self) -> List[Dict[str, Any]]:
        """Every series in the namespace, paginated to exhaustion.

        Cached for the whole TTL to avoid repeated ListMetrics calls across
        multiple public methods. Sequential: about five calls for ~2,335 series.
        Parallelising five calls would buy nothing and would put the connection-
        pool trap back in. The lock is held for the entire check-sweep-store
        cycle to prevent concurrent threads from duplicating the sweep.
        """
        client = self.client

        with self._lock:
            fresh = (
                self._series is not None
                and time.monotonic() - self._series_at < self._cache_seconds
            )
            if fresh:
                return list(self._series)

            series: List[Dict[str, Any]] = []
            params: Dict[str, Any] = {"Namespace": NAMESPACE}
            while True:
                try:
                    response = client.list_metrics(**params)
                except Exception as exc:
                    raise TelemetryUnavailable(str(exc)) from exc
                series.extend(response.get("Metrics", []))
                token = response.get("NextToken")
                if not token:
                    break
                params["NextToken"] = token

            self._series = series
            self._series_at = time.monotonic()
            return list(series)

    def metric_index(self) -> Dict[str, List[Dict[str, Any]]]:
        """ARN -> the series that mention it, cached for `cache_seconds`.

        Keyed on every ARN in the dimensions rather than on `Resource` alone:
        harness traffic is dimensioned on the companion runtime and carries the
        harness ARN in `HarnessId`, and a registry record may only know one of
        the two.
        """
        with self._lock:
            fresh = (
                self._index is not None
                and time.monotonic() - self._index_at < self._cache_seconds
            )
            if fresh:
                return self._index or {}

        index: Dict[str, List[Dict[str, Any]]] = {}
        for entry in self._all_series():
            dimensions = entry.get("Dimensions") or []
            for dimension in dimensions:
                value = dimension.get("Value") or ""
                if value.startswith("arn:"):
                    index.setdefault(value, []).append(entry)

        with self._lock:
            self._index = index
            self._index_at = time.monotonic()
        return index

    # --- batched reads ------------------------------------------------------

    @staticmethod
    def _period(start: datetime, end: datetime) -> int:
        """One bucket spanning the window, rounded up to a whole minute.

        A single datapoint per query is what makes `p90` the window's p90 rather
        than the worst day's. Periods must be a multiple of 60; both windows the
        UI offers were verified against the live account.
        """
        seconds = max(60.0, (end - start).total_seconds())
        return int(math.ceil(seconds / 60.0) * 60)

    @staticmethod
    def _signature(dimensions: List[Dict[str, Any]]) -> Tuple[Any, ...]:
        """A dimension set as a hashable key, order-insensitive."""
        return tuple(
            sorted((d.get("Name"), d.get("Value")) for d in dimensions)
        )

    def _run(self, queries: List[Dict[str, Any]], start, end) -> Dict[str, List[float]]:
        """One `GetMetricData` per 500 queries; returns values keyed by query id."""
        values: Dict[str, List[float]] = {}
        for offset in range(0, len(queries), _MAX_QUERIES):
            chunk = queries[offset : offset + _MAX_QUERIES]
            try:
                response = self.client.get_metric_data(
                    MetricDataQueries=chunk, StartTime=start, EndTime=end
                )
            except Exception as exc:
                raise TelemetryUnavailable(str(exc)) from exc
            for result in response.get("MetricDataResults", []):
                values[result["Id"]] = [float(v) for v in result.get("Values", [])]
        return values

    @staticmethod
    def _reduce(values: List[float], stat: str) -> float:
        """One window bucket, so this is a passthrough for the normal case.

        Kept for the case where CloudWatch splits the window anyway: a `Sum`
        still sums, and a percentile takes the worst bucket rather than
        averaging percentiles, which is not a statistic at all.
        """
        if not values:
            return 0.0
        if len(values) == 1:
            return values[0]
        return max(values) if stat == "p90" else sum(values)

    def _candidates(
        self,
        arn: str,
        field: str,
        entries: List[Dict[str, Any]],
        winners: Dict[Tuple[str, str], Tuple[float, Tuple[Any, ...]]],
        now: float,
    ) -> List[Dict[str, Any]]:
        """The series to probe for one (agent, metric) — memoised winner first.

        A warm memo cuts the sweep from every dimension set that mentions the ARN
        (17 per ARN, measured) to one. `winners` is a snapshot taken before the
        sweep so the plan cannot change halfway through it.
        """
        memo = winners.get((arn, field))
        if memo and now - memo[0] < self._memo_seconds:
            signature = memo[1]
            remembered = [
                entry
                for entry in entries
                if self._signature(entry.get("Dimensions") or []) == signature
            ]
            if remembered:
                return remembered[:1]
        return entries

    def agent_metrics(
        self, arns: List[str], start: datetime, end: datetime
    ) -> Dict[str, Dict[str, Any]]:
        """Vended figures per ARN, for the ARNs that have any.

        An ARN with no discovered series is **absent from the result**, not
        present with zeros — "we have no telemetry for this agent" and "this
        agent did nothing" are different answers and the UI renders them
        differently. The same rule applies field by field.

        Where several discovered series match one (ARN, metric), all are queried
        and the value from the series with the **fewest dimensions that returned
        data** wins. Fewest dimensions is the widest rollup, so it already
        contains the others; taking the most specific would drop its siblings,
        and summing them would count a rollup against its own children. The
        winner is then remembered so the next sweep asks for one series instead
        of all of them.

        `error_rate` is computed here rather than by the caller because only
        here is it known whether the numerator and the denominator came from the
        same series. When they did not, the field is **absent**: a ratio across
        two dimension widths compares two different populations and can exceed
        1, which is how it read on the dashboard.

        The result is cached for `cache_seconds` against the ARN set and a
        quantised window, because every field in it is billed per sweep.
        """
        cache_key = (
            tuple(sorted(arns)),
            int(start.timestamp()) // 60,
            int(end.timestamp()) // max(1, self._cache_seconds),
        )
        with self._lock:
            cached = self._metrics.get(cache_key)
            if cached and time.monotonic() - cached[0] < self._cache_seconds:
                return {arn: dict(fields) for arn, fields in cached[1].items()}

        index = self.metric_index()
        now = time.monotonic()
        period = self._period(start, end)
        with self._lock:
            winners = dict(self._winners)
        queries: List[Dict[str, Any]] = []
        # query id -> (arn, field, stat, dimension count, signature)
        provenance: Dict[str, Any] = {}

        for arn in arns:
            entries = index.get(arn, [])
            if not entries:
                continue
            for metric_name, stat, field in _AGENT_METRICS:
                matching = [
                    entry for entry in entries
                    if entry.get("MetricName") == metric_name
                ]
                for entry in self._candidates(arn, field, matching, winners, now):
                    query_id = f"q{len(queries)}"
                    dimensions = entry.get("Dimensions") or []
                    queries.append({
                        "Id": query_id,
                        "MetricStat": {
                            "Metric": {
                                "Namespace": NAMESPACE,
                                "MetricName": metric_name,
                                "Dimensions": dimensions,
                            },
                            "Period": period,
                            "Stat": stat,
                        },
                        "ReturnData": True,
                    })
                    provenance[query_id] = (
                        arn,
                        field,
                        stat,
                        len(dimensions),
                        self._signature(dimensions),
                    )

        if not queries:
            return {}

        raw = self._run(queries, start, end)

        # Best candidate per (arn, field): had data, then fewest dimensions.
        best: Dict[Any, Any] = {}
        probed: set = set()
        for query_id, (arn, field, stat, width, signature) in provenance.items():
            probed.add((arn, field))
            values = raw.get(query_id, [])
            if not values:
                continue
            key = (arn, field)
            if key not in best or width < best[key][0]:
                best[key] = (width, self._reduce(values, stat), signature)

        with self._lock:
            for key in probed:
                if key in best:
                    self._winners[key] = (now, best[key][2])
                else:
                    # The remembered series answered nothing. It may have been
                    # the wrong one all along, so re-probe every candidate next
                    # sweep rather than reporting absence forever.
                    self._winners.pop(key, None)

        # `Any`, not `float`: `error_basis` is the list of series the rate
        # stands on, and it travels beside the numbers it qualifies.
        metrics: Dict[str, Dict[str, Any]] = {}
        signatures: Dict[str, Dict[str, Tuple[Any, ...]]] = {}
        for (arn, field), (_, value, signature) in best.items():
            metrics.setdefault(arn, {})[field] = value
            signatures.setdefault(arn, {})[field] = signature

        probed_by_arn: Dict[str, set] = {}
        for arn, field in probed:
            probed_by_arn.setdefault(arn, set()).add(field)

        for arn, fields in metrics.items():
            rate = self._error_rate(
                fields, signatures[arn], probed_by_arn.get(arn, set())
            )
            if rate is not None:
                fields["error_rate"] = rate["rate"]
                # Which of the two error series the numerator actually holds. The
                # UI marks a partial basis; without it, half the errors read as all
                # of them.
                fields["error_basis"] = rate["basis"]

        with self._lock:
            self._metrics[cache_key] = (
                time.monotonic(),
                {arn: dict(fields) for arn, fields in metrics.items()},
            )
        return metrics

    @staticmethod
    def _error_rate(
        fields: Dict[str, Any],
        signatures: Dict[str, Tuple[Any, ...]],
        probed: set,
    ) -> Optional[Dict[str, Any]]:
        """Errors over invocations, with the series the numerator stands on.

        Absent, not zero, when the denominator is missing or zero: an agent
        CloudWatch reported no invocations for has no error rate, and a division
        by nothing is not a clean bill of health.

        **A field that was probed and came back empty is a zero; a field that was
        never probed is unknown.** For a counter those are genuinely different: the
        first means CloudWatch had the series and nothing happened, the second means
        the series does not exist for this agent — which is the normal state for an
        agent that has never had a user error, so refusing the rate would blank it
        for healthy agents. What was not allowed to continue is reporting a
        one-series numerator as if it were both, so `basis` names what was counted
        and the UI marks a partial one.
        """
        invocations = fields.get("invocations") or 0.0
        if not invocations:
            return None
        denominator_signature = signatures.get("invocations")
        errors = 0.0
        basis: List[str] = []
        for field in _ERROR_FIELDS:
            if field not in fields:
                # Probed and empty is zero errors, and adds nothing but its name.
                if field in probed:
                    basis.append(field)
                continue
            if signatures.get(field) != denominator_signature:
                return None
            errors += fields[field]
            basis.append(field)
        if not basis:
            return None
        return {"rate": round(errors / invocations, 4), "basis": basis}
