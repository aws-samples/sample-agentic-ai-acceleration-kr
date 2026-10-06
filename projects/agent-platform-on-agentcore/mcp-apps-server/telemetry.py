"""CloudWatch 지표 → 앱이 그리는 `Telemetry`.

boto3 호출은 `fetch()` 한 곳에만 있고, 질의 조립(`build_queries`)과 응답 변환
(`shape_result`)은 순수 함수라 가짜 응답으로 테스트한다.

왜 SEARCH 식인가: 항목(모델·런타임·툴)이 몇 개인지 미리 모른다. SEARCH 하나가 차원값
전부를 시계열로 돌려주므로 뷰당 GetMetricData 한 번이면 된다. 스키마 부분은 **정확한
차원 집합**이어야 한다 — 같은 지표가 차원 조합별로 여러 번 존재해서(예: 런타임
Invocations 는 (Name,Operation,Resource) 와 (ComputeType,Name,Operation,Resource) 둘)
느슨하게 잡으면 두 배로 집계된다.

동적 라벨 `${PROP('Dim.X')}` 는 결과의 `Label` 을 차원값으로 만들어 항목 id 로 쓸 수
있게 한다 (2026-09-23 실측: 5개 SEARCH → 52 시계열, NextToken 없음).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple, TypedDict

VIEWS: Tuple[str, ...] = ("models", "agents", "tools")

# period → (bucket seconds, bucket count)
PERIODS: Dict[str, Tuple[int, int]] = {
    "1h": (300, 12),
    "24h": (3600, 24),
    "7d": (86400, 7),
}

# view → [(metric name, statistic)]. Order matters: query ids are `q{index}_{metric}`.
METRICS: Dict[str, List[Tuple[str, str]]] = {
    "models": [
        ("Invocations", "Sum"),
        ("InputTokenCount", "Sum"),
        ("OutputTokenCount", "Sum"),
        ("CacheReadInputTokenCount", "Sum"),
        ("CacheWriteInputTokenCount", "Sum"),
        ("InvocationLatency", "Average"),
        ("TimeToFirstToken", "Average"),
    ],
    "agents": [
        ("Invocations", "Sum"),
        ("Latency", "Average"),
        ("UserErrors", "Sum"),
        ("SystemErrors", "Sum"),
        ("Throttles", "Sum"),
    ],
    "tools": [
        ("Invocations", "Sum"),
        ("Latency", "Average"),
        ("Errors", "Sum"),
    ],
}

# view → (SEARCH schema + fixed dimension filters, label dimension)
_SEARCH: Dict[str, Tuple[str, str]] = {
    "models": ("{AWS/Bedrock,ModelId}", "ModelId"),
    # Harnesses run as runtimes too and show up here as `harness_<name>::DEFAULT`;
    # the (EndpointQualifier,HarnessId,Operation) schema returned nothing live.
    "agents": (
        '{AWS/Bedrock-AgentCore,Name,Operation,Resource} Operation="InvokeAgentRuntime"',
        "Name",
    ),
    "tools": (
        "{AWS/Bedrock-AgentCore,Method,Name,Operation,Protocol} "
        'Operation="InvokeGateway" Method="tools/call"',
        "Name",
    ),
}


class Series(TypedDict):
    id: str
    label: str
    kind: str  # model | runtime | harness | tool
    totals: Dict[str, float]
    points: Dict[str, List[float]]


class Telemetry(TypedDict):
    view: str
    period: str
    bucketSeconds: int
    buckets: List[str]
    series: List[Series]
    generatedAt: str
    note: str


def _validate(view: str, period: str) -> None:
    if view not in VIEWS:
        raise ValueError(f"unknown view {view!r}; expected one of {VIEWS}")
    if period not in PERIODS:
        raise ValueError(f"unknown period {period!r}; expected one of {tuple(PERIODS)}")


def _query_id(index: int, metric: str) -> str:
    return f"q{index}_{metric.lower()}"


def build_queries(view: str, period: str) -> List[Dict[str, Any]]:
    """One SEARCH query per metric of the view, all sharing the view's bucket size."""
    _validate(view, period)
    seconds, _ = PERIODS[period]
    schema, label_dim = _SEARCH[view]
    # The schema string is `{Namespace,Dim,...} Dim="v" ...`; the metric filter goes
    # right after the braces so the fixed filters stay at the end.
    brace_end = schema.index("}") + 1
    queries: List[Dict[str, Any]] = []
    for i, (metric, stat) in enumerate(METRICS[view]):
        expression = (
            f"SEARCH('{schema[:brace_end]} MetricName=\"{metric}\"{schema[brace_end:]}', "
            f"'{stat}', {seconds})"
        )
        queries.append(
            {
                "Id": _query_id(i, metric),
                "Expression": expression,
                "Label": f"${{PROP('Dim.{label_dim}')}}",
                "ReturnData": True,
            }
        )
    return queries


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def display_label(view: str, raw_id: str) -> Tuple[str, str]:
    """(label, kind) for a raw dimension value.

    Models keep their inference-profile prefix as `us/` or `global/` — the same model is
    reported under both and collapsing them would merge two distinct series.
    """
    if view == "models":
        parts = raw_id.split(".")
        if len(parts) >= 3:
            return f"{parts[0]}/{'.'.join(parts[2:])}", "model"
        return raw_id, "model"
    if view == "agents":
        name = raw_id.split("::", 1)[0]
        if name.startswith("harness_"):
            return name[len("harness_"):], "harness"
        return name, "runtime"
    # tools: `<gateway target>___<tool>`; bare names come from targets without a prefix.
    return raw_id.rsplit("___", 1)[-1], "tool"


def shape_result(
    view: str,
    period: str,
    results: List[Dict[str, Any]],
    start: datetime,
    now: datetime,
) -> Telemetry:
    """Align raw MetricDataResults to buckets, merge same-label series, drop empty ones."""
    _validate(view, period)
    seconds, count = PERIODS[period]
    metrics = METRICS[view]
    stat_of = dict(metrics)
    metric_of_query = {_query_id(i, m): m for i, (m, _) in enumerate(metrics)}
    buckets = [start + timedelta(seconds=seconds * i) for i in range(count)]

    # label -> metric -> bucket index -> values (several resources may share a label)
    raw: Dict[str, Dict[str, Dict[int, List[float]]]] = {}
    for r in results:
        metric = metric_of_query.get(r["Id"])
        if metric is None:
            continue
        per_metric = raw.setdefault(r["Label"], {}).setdefault(metric, {})
        for ts, value in zip(r["Timestamps"], r["Values"]):
            idx = int((ts - start).total_seconds() // seconds)
            if 0 <= idx < count:
                per_metric.setdefault(idx, []).append(float(value))

    series: List[Series] = []
    for raw_id, per_metric in raw.items():
        points: Dict[str, List[float]] = {}
        totals: Dict[str, float] = {}
        has_data = False
        for metric, stat in metrics:
            values_by_idx = per_metric.get(metric, {})
            row = [0.0] * count
            for idx, values in values_by_idx.items():
                has_data = True
                row[idx] = sum(values) if stat == "Sum" else sum(values) / len(values)
            points[metric] = row
            if stat == "Sum":
                totals[metric] = sum(row)
            else:
                # Plain mean of the buckets that have data. Weighting by invocations
                # would need every metric to line up; the demo does not need it.
                present = [row[i] for i in values_by_idx]
                totals[metric] = sum(present) / len(present) if present else 0.0
        if not has_data:
            continue
        label, kind = display_label(view, raw_id)
        series.append(
            {"id": raw_id, "label": label, "kind": kind, "totals": totals, "points": points}
        )

    series.sort(key=lambda s: (-s["totals"].get("Invocations", 0.0), s["label"]))

    invocations = int(sum(s["totals"].get("Invocations", 0.0) for s in series))
    # Read by the model (and by clients without apps). Live, Haiku followed the app with a
    # 405-line HTML "dashboard" artifact restating the same numbers — so the note says
    # plainly what the user already sees and what not to do.
    note = (
        f"{len(series)} {view} with activity in the last {period}, "
        f"{invocations} invocations in total. The user already sees this as an interactive "
        f"dashboard in the chat (chart, table, tabs, refresh). Do not create an artifact or "
        f"repeat the table; reply with one or two sentences about what stands out."
    )
    return {
        "view": view,
        "period": period,
        "bucketSeconds": seconds,
        "buckets": [_iso(b) for b in buckets],
        "series": series,
        "generatedAt": _iso(now),
        "note": note,
    }


def window(period: str, now: datetime) -> Tuple[datetime, datetime]:
    """(start, end): `count` buckets of `seconds`, the last one containing `now`."""
    seconds, count = PERIODS[period]
    epoch = int(now.timestamp())
    last_bucket_start = epoch - (epoch % seconds)
    start = datetime.fromtimestamp(last_bucket_start - seconds * (count - 1), tz=timezone.utc)
    return start, now


def fetch(client: Any, view: str, period: str, now: datetime | None = None) -> Telemetry:
    """One GetMetricData round (plus NextToken pages) shaped for the app.

    `client` is anything with `get_metric_data(**kwargs) -> dict` — the boto3 CloudWatch
    client in production, a stub in tests.
    """
    _validate(view, period)
    now = now or datetime.now(timezone.utc)
    start, end = window(period, now)
    kwargs: Dict[str, Any] = {
        "MetricDataQueries": build_queries(view, period),
        "StartTime": start,
        "EndTime": end,
        "ScanBy": "TimestampAscending",
    }
    results: List[Dict[str, Any]] = []
    while True:
        page = client.get_metric_data(**kwargs)
        results.extend(page.get("MetricDataResults", []))
        token = page.get("NextToken")
        if not token:
            break
        kwargs = {**kwargs, "NextToken": token}
    return shape_result(view, period, results, start, now)
