"""One `SystemErrors` alarm per discovered AgentCore runtime.

**Why a script and not terraform.** The runtimes these alarms watch are created
by `harness_service.create_harness` at request time, so terraform never learns
their ids. Their dimension sets cannot be written down either: CloudWatch does
not populate partial rollups in this namespace, so a set assembled from a table
in a document returns zero datapoints with no error and the alarm sits in
INSUFFICIENT_DATA looking like health (measured 2026-08-16 — `{Operation,
Resource}` gave 0 datapoints for a runtime that had 7 invocations that day;
adding `{Name}` gave the 7).

**Why it reuses `metric_index()`.** The dashboard already sweeps `ListMetrics`
once and caches it. A second implementation here would be a second thing to keep
correct, and per-agent `ListMetrics` calls would serialise behind botocore's
ten-connection pool — the trap that made registry listing slow.

**Why it is scoped to the registry by default.** The metric index covers the whole
account, and this account is shared. Measured 2026-08-16: an unscoped run wanted
**46** alarms, most of them other teams' runtimes (`cardnews_agent`,
`ks_sap_agent`, `strands_*`, `ecommerce_analytics`), one of them a *gateway* and
one a single *gateway tool* (`builtin-tools___stop_code_session`). Being paged
because someone else's agent erred is noise, and CloudWatch charges $0.10 per
alarm per month for the privilege.

So the ARNs are intersected with the ones the registry binds — the same
`agent_records_by_arn()` index the insights dashboard uses to decide which agents
are ours. `--all` disables the filter for the rare case of wanting the account.

Idempotent. Every alarm it writes is named `ap-insights-…` and every delete is
filtered to that prefix, so it cannot touch an alarm it did not create. Run it
after creating agents:

    python -m scripts.sync_agent_alarms --topic-arn "$(terraform output -raw alerts_topic_arn)" --apply
"""
import argparse
import hashlib
import json
import logging
import sys
from typing import Any, Dict, List, Optional

import boto3

from services.registry_service import RegistryService
from services.telemetry_service import (
    NAMESPACE,
    TelemetryService,
    TelemetryUnavailable,
)

logger = logging.getLogger(__name__)

ALARM_PREFIX = "ap-insights-"

# One error in five minutes is worth a look for an agent platform: invocations are
# human-initiated and low-volume, so a rate threshold would need a denominator
# that is usually near zero.
_PERIOD = 300
_THRESHOLD = 1.0
_EVALUATION_PERIODS = 1


def _readable(dimensions: List[Dict[str, str]]) -> str:
    """A console-legible label: the agent, then the call path.

    `Name` is the human-facing form CloudWatch already uses
    (`bap_default::DEFAULT`). `Operation` is appended because one agent has a
    series per call path, and two alarms differing only by a sha256 suffix are
    indistinguishable to whoever is woken by one.
    """
    values = {d["Name"]: d["Value"] for d in dimensions}
    label = values.get("Name") or values.get("Resource", "")
    label = label.rsplit("/", 1)[-1].replace("::", "-") or "unknown"
    operation = values.get("Operation")
    return f"{label}-{operation}" if operation else label


def alarm_name(dimensions: List[Dict[str, str]]) -> str:
    """A stable name per dimension set, so a re-run updates instead of duplicating.

    The digest is over the sorted dimension set rather than over a resource id:
    one runtime legitimately has several series, and two of them must not collide
    onto one alarm name.
    """
    canonical = json.dumps(
        sorted((d["Name"], d["Value"]) for d in dimensions), sort_keys=True
    )
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:10]
    return f"{ALARM_PREFIX}{_readable(dimensions)}-{digest}"


def desired_alarms(
    index: Dict[str, List[Dict[str, Any]]], metric_name: str = "SystemErrors"
) -> Dict[str, Dict[str, Any]]:
    """Alarm name -> the series to alarm on.

    **One alarm per (agent, call path).** Measured 2026-08-16 for one harness
    agent: eight `SystemErrors` series, differing by `Operation`
    (`InvokeAgentRuntime` versus `InvokeAgentRuntimeCommand`), by whether
    `EndpointQualifier`/`HarnessId` are present, and by whether `Resource` is
    present at all — the harness-keyed series carry no `Resource`. Grouping on
    `Resource or HarnessId`, as this function first did, split one agent's traffic
    across two groups and produced two same-named alarms per harness agent.

    `Name` is the agent (`harness_academic_writer::DEFAULT`) and it appears on
    every series, so it is the grouping key. `Operation` joins it because the two
    call paths are genuinely different failures — our harness path is
    `InvokeAgentRuntimeCommand` and a direct runtime invoke is
    `InvokeAgentRuntime`. Collapsing them would leave one path unwatched to save
    $0.10 a month.

    Within a group the **most specific** dimension set wins. This is the opposite
    of the widest-rollup rule `agent_metrics` uses for reading, and for the same
    reason: only the specific set has datapoints, and an alarm on an empty series
    never fires. Ties break on the sorted canonical form so a re-run does not
    churn alarm names.
    """
    by_agent: Dict[Any, Dict[str, Any]] = {}
    for entries in index.values():
        for entry in entries:
            if entry.get("MetricName") != metric_name:
                continue
            dimensions = entry.get("Dimensions") or []
            if not dimensions:
                continue
            values = {d["Name"]: d["Value"] for d in dimensions}
            # A series without `Name` is one of the partial rollups fact 18
            # measured as returning zero datapoints. Alarming on it costs $0.10 a
            # month to watch nothing, and it is also the only shape that would
            # split one agent across two groups, because `Name` is the single
            # dimension every populated series carries.
            if "Name" not in values:
                continue
            key = (values["Name"], values.get("Operation", ""))
            canonical = sorted((d["Name"], d["Value"]) for d in dimensions)
            current = by_agent.get(key)
            if current is None or (len(dimensions), canonical) > (
                len(current["Dimensions"]),
                sorted((d["Name"], d["Value"]) for d in current["Dimensions"]),
            ):
                by_agent[key] = {"Dimensions": dimensions}

    return {alarm_name(spec["Dimensions"]): spec for spec in by_agent.values()}


def registry_arns(registry) -> List[str]:
    """Every ARN the registry binds an agent record to.

    `agent_records_by_arn()` already indexes both forms a harness-backed record
    carries — its harness ARN and its companion runtime ARN — which is exactly the
    ambiguity that would otherwise make this filter miss half the series.
    """
    return list(registry.agent_records_by_arn().keys())


def scope_to(
    index: Dict[str, List[Dict[str, Any]]], arns: List[str]
) -> Dict[str, List[Dict[str, Any]]]:
    """`index` restricted to the ARNs we own.

    An empty `arns` yields an empty index rather than the whole account: "the
    registry told us about nothing" must not silently become "alarm on
    everything", which is the 46-alarm outcome arrived at without noticing.
    """
    allowed = set(arns)
    return {arn: series for arn, series in index.items() if arn in allowed}


def _existing(cloudwatch) -> List[str]:
    """The alarms this script owns.

    The prefix is applied **twice**: once as `AlarmNamePrefix` so CloudWatch does
    not page through the whole account, and again here on what comes back. The
    second filter is what makes "we cannot delete someone else's alarm" a property
    of this function rather than a promise made by the API — this is a shared
    account, the caller of this list is a delete, and the cost of the redundancy
    is one condition.
    """
    names: List[str] = []
    paginator = cloudwatch.get_paginator("describe_alarms")
    for page in paginator.paginate(AlarmNamePrefix=ALARM_PREFIX):
        names.extend(
            alarm["AlarmName"]
            for alarm in page.get("MetricAlarms", [])
            if alarm.get("AlarmName", "").startswith(ALARM_PREFIX)
        )
    return names


def sync(
    cloudwatch,
    index: Dict[str, List[Dict[str, Any]]],
    topic_arn: str,
    apply: bool = False,
    metric_name: str = "SystemErrors",
) -> Dict[str, List[str]]:
    """Bring the `ap-insights-*` alarms in line with what CloudWatch knows."""
    desired = desired_alarms(index, metric_name=metric_name)
    existing = _existing(cloudwatch)

    created = sorted(set(desired) - set(existing))
    kept = sorted(set(desired) & set(existing))
    # Only ever prefixed names: `_existing` filters on the prefix, so this
    # difference cannot contain an alarm the script did not create.
    deleted = sorted(set(existing) - set(desired))

    if apply:
        for name, spec in desired.items():
            # PutMetricAlarm is an upsert, so kept alarms are refreshed too: the
            # topic ARN or threshold may have changed since they were written.
            cloudwatch.put_metric_alarm(
                AlarmName=name,
                Namespace=NAMESPACE,
                MetricName=metric_name,
                Dimensions=spec["Dimensions"],
                Statistic="Sum",
                Period=_PERIOD,
                EvaluationPeriods=_EVALUATION_PERIODS,
                Threshold=_THRESHOLD,
                ComparisonOperator="GreaterThanOrEqualToThreshold",
                # A quiet agent has no datapoints at all; silence is not failure.
                TreatMissingData="notBreaching",
                AlarmDescription=(
                    f"AgentCore {metric_name} on {_readable(spec['Dimensions'])}."
                ),
                AlarmActions=[topic_arn],
                OKActions=[topic_arn],
            )
        if deleted:
            cloudwatch.delete_alarms(AlarmNames=deleted)

    return {"created": created, "kept": kept, "deleted": deleted}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topic-arn", required=True)
    parser.add_argument("--region", default=None)
    parser.add_argument("--metric", default="SystemErrors")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Do not scope to the registry. Alarms on every AgentCore series in "
        "the account, including other teams' — 46 of them when measured.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the changes. Without it, only the plan is printed.",
    )
    args = parser.parse_args(argv)

    telemetry = TelemetryService(region_name=args.region)
    try:
        index = telemetry.metric_index()
    except TelemetryUnavailable as exc:
        print(f"CloudWatch could not be read: {exc}", file=sys.stderr)
        return 1

    if not args.all:
        try:
            index = scope_to(index, registry_arns(RegistryService()))
        except Exception as exc:
            print(
                f"Could not read the registry to scope the alarms: {exc}\n"
                "Re-run with --all to alarm on every AgentCore series in the "
                "account, or fix the registry configuration first.",
                file=sys.stderr,
            )
            return 1

    cloudwatch = boto3.client("cloudwatch", region_name=telemetry.region_name)
    result = sync(
        cloudwatch, index, args.topic_arn, apply=args.apply, metric_name=args.metric
    )
    print(json.dumps(result, indent=2))
    if not args.apply:
        print("(dry run — nothing written; pass --apply)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
