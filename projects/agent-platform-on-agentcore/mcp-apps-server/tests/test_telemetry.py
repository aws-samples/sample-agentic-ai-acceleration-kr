"""telemetry.py의 순수 함수를 가짜 GetMetricData 응답으로 검증한다.

실행 (mcp-apps-server/ 에서):
    uv run --with 'mcp>=1.26.0' --with boto3 --with pytest --with anyio python -m pytest tests -q
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import telemetry as tm  # noqa: E402


def test_models_24h_builds_one_search_per_metric_with_hourly_period():
    queries = tm.build_queries("models", "24h")
    assert len(queries) == 7
    ids = [q["Id"] for q in queries]
    assert len(set(ids)) == 7 and all(i[0].islower() for i in ids)
    for q in queries:
        assert q["ReturnData"] is True
        assert q["Label"] == "${PROP('Dim.ModelId')}"
        assert "SEARCH('{AWS/Bedrock,ModelId} MetricName=\"" in q["Expression"]
        assert q["Expression"].endswith(", 3600)")
    stats = {q["Expression"].rsplit("'", 2)[1] for q in queries}
    assert stats == {"Sum", "Average"}


def test_agents_query_is_pinned_to_the_exact_dimension_set():
    """(Name,Operation,Resource)와 (ComputeType,Name,Operation,Resource)가 같은 지표를
    두 번 내므로 스키마를 정확히 못 박아야 이중 집계가 안 된다."""
    [inv] = [q for q in tm.build_queries("agents", "1h") if 'MetricName="Invocations"' in q["Expression"]]
    assert inv["Expression"].startswith(
        "SEARCH('{AWS/Bedrock-AgentCore,Name,Operation,Resource} MetricName=\"Invocations\" "
        'Operation="InvokeAgentRuntime"\', \'Sum\', 300)'
    )
    assert inv["Label"] == "${PROP('Dim.Name')}"


def test_tools_query_filters_gateway_tool_calls():
    [inv] = [q for q in tm.build_queries("tools", "7d") if 'MetricName="Invocations"' in q["Expression"]]
    assert 'Operation="InvokeGateway" Method="tools/call"' in inv["Expression"]
    assert "{AWS/Bedrock-AgentCore,Method,Name,Operation,Protocol}" in inv["Expression"]
    assert inv["Expression"].endswith(", 86400)")


def test_unknown_view_or_period_is_rejected():
    with pytest.raises(ValueError):
        tm.build_queries("costs", "24h")
    with pytest.raises(ValueError):
        tm.build_queries("models", "30d")


START = datetime(2026, 9, 23, 0, 0, tzinfo=timezone.utc)
NOW = START + timedelta(hours=24)


def _result(query_id: str, label: str, points: dict[int, float]) -> dict:
    """{bucket index: value}. CloudWatch는 값이 없는 버킷을 아예 안 보낸다."""
    ts = [START + timedelta(hours=i) for i in points]
    return {
        "Id": query_id,
        "Label": label,
        "Timestamps": ts,
        "Values": [points[i] for i in points],
        "StatusCode": "Complete",
    }


def test_points_are_aligned_to_buckets_and_zero_filled():
    results = [
        _result("q1_inputtokencount", "us.anthropic.claude-opus-5", {23: 10.0, 0: 5.0}),
    ]
    out = tm.shape_result("models", "24h", results, START, NOW)
    assert out["view"] == "models" and out["period"] == "24h" and out["bucketSeconds"] == 3600
    assert len(out["buckets"]) == 24
    assert out["buckets"][0] == "2026-09-23T00:00:00Z" and out["buckets"][-1] == "2026-09-23T23:00:00Z"
    [s] = out["series"]
    assert s["id"] == "us.anthropic.claude-opus-5"
    pts = s["points"]["InputTokenCount"]
    assert len(pts) == 24 and pts[0] == 5.0 and pts[23] == 10.0 and sum(pts) == 15.0
    # Metrics the query returned nothing for are present, all zero.
    assert s["points"]["Invocations"] == [0.0] * 24
    assert s["totals"]["InputTokenCount"] == 15.0


def test_same_label_from_two_resources_is_merged():
    """같은 이름의 런타임이 Resource(ARN)가 다른 인스턴스 둘로 잡힌다(실측
    harness_platform_ops::DEFAULT × 2). Sum은 더하고 Average는 값 있는 것끼리 평균."""
    results = [
        _result("q0_invocations", "harness_platform_ops::DEFAULT", {1: 5.0}),
        _result("q0_invocations", "harness_platform_ops::DEFAULT", {1: 3.0, 2: 1.0}),
        _result("q1_latency", "harness_platform_ops::DEFAULT", {1: 100.0}),
        _result("q1_latency", "harness_platform_ops::DEFAULT", {1: 300.0}),
    ]
    out = tm.shape_result("agents", "24h", results, START, NOW)
    [s] = out["series"]
    assert s["points"]["Invocations"][1] == 8.0 and s["points"]["Invocations"][2] == 1.0
    assert s["totals"]["Invocations"] == 9.0
    assert s["points"]["Latency"][1] == 200.0
    assert s["totals"]["Latency"] == 200.0
    assert s["kind"] == "harness" and s["label"] == "platform_ops"


def test_average_total_is_the_mean_of_buckets_that_have_data():
    results = [_result("q5_invocationlatency", "us.anthropic.claude-opus-5", {0: 100.0, 5: 300.0})]
    out = tm.shape_result("models", "24h", results, START, NOW)
    [s] = out["series"]
    assert s["totals"]["InvocationLatency"] == 200.0
    assert s["points"]["InvocationLatency"][1] == 0.0


def test_series_with_no_data_at_all_are_dropped_and_sorted_by_invocations():
    results = [
        _result("q0_invocations", "us.anthropic.claude-fable-5", {}),
        _result("q0_invocations", "us.anthropic.claude-opus-5", {0: 2.0}),
        _result("q0_invocations", "global.anthropic.claude-sonnet-5", {0: 7.0}),
    ]
    out = tm.shape_result("models", "24h", results, START, NOW)
    assert [s["id"] for s in out["series"]] == [
        "global.anthropic.claude-sonnet-5",
        "us.anthropic.claude-opus-5",
    ]


@pytest.mark.parametrize(
    "view,raw,label,kind",
    [
        ("models", "us.anthropic.claude-opus-5", "us/claude-opus-5", "model"),
        ("models", "global.anthropic.claude-haiku-4-5-20251001-v1:0", "global/claude-haiku-4-5-20251001-v1:0", "model"),
        ("models", "global.openai.gpt-5.6-sol", "global/gpt-5.6-sol", "model"),
        ("agents", "bap_default::DEFAULT", "bap_default", "runtime"),
        ("agents", "harness_platform_ops::DEFAULT", "platform_ops", "harness"),
        ("tools", "bap-platform-tools___calculate", "calculate", "tool"),
        ("tools", "WebSearch", "WebSearch", "tool"),
    ],
)
def test_display_label(view, raw, label, kind):
    assert tm.display_label(view, raw) == (label, kind)


def test_note_summarises_for_clients_without_apps():
    results = [_result("q0_invocations", "us.anthropic.claude-opus-5", {0: 2.0})]
    out = tm.shape_result("models", "24h", results, START, NOW)
    assert "1 models" in out["note"] and "24h" in out["note"]
    assert out["generatedAt"] == "2026-09-24T00:00:00Z"


class _StubCloudWatch:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def get_metric_data(self, **kwargs):
        self.calls.append(kwargs)
        return self.pages.pop(0)


def test_fetch_aligns_start_to_bucket_and_follows_next_token():
    now = datetime(2026, 9, 23, 12, 34, 56, tzinfo=timezone.utc)
    page1 = {"MetricDataResults": [], "NextToken": "t2"}
    page2 = {"MetricDataResults": [_result("q0_invocations", "bap_default::DEFAULT", {})]}
    cw = _StubCloudWatch([page1, page2])

    out = tm.fetch(cw, "agents", "1h", now=now)

    assert len(cw.calls) == 2 and cw.calls[1]["NextToken"] == "t2"
    first = cw.calls[0]
    assert first["ScanBy"] == "TimestampAscending"
    assert first["EndTime"] == now
    # 12 buckets of 300s ending at the bucket that contains `now`: start is aligned.
    assert first["StartTime"] == datetime(2026, 9, 23, 11, 35, 0, tzinfo=timezone.utc)
    assert len(first["MetricDataQueries"]) == 5
    assert out["buckets"][0] == "2026-09-23T11:35:00Z" and len(out["buckets"]) == 12
