"""CloudWatch reads, and the trap that makes them look like they work.

Measured 2026-08-16: `Invocations` with dimensions {Operation, Resource}
returned no datapoints for a runtime that had 7 invocations that day; adding
{Name} returned them. So a hardcoded dimension table produces a permanently
empty dashboard with no error anywhere. These tests pin the alternative — the
service discovers dimension sets with ListMetrics and replays them unchanged —
and the selection rules that keep the numbers honest.

They also pin what it costs. `GetMetricData` bills per metric requested, so the
three things that hold the bill down are behaviour, not optimisation: the result
cache, the memo of which dimension set answered, and a metric list containing
only what something renders. Measured before they existed: 279 metrics per page
poll, $4.02 a day per open tab; 72 after.
"""
import os
import sys
import threading
import time
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.telemetry_service import (  # noqa: E402
    NAMESPACE,
    TelemetryService,
    TelemetryUnavailable,
)

RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:1:runtime/bap_default-nSO"
HARNESS_ARN = "arn:aws:bedrock-agentcore:us-east-1:1:harness/writer-abc"
START = datetime(2026, 8, 15)
END = datetime(2026, 8, 16)


def dims(**pairs):
    return [{"Name": name, "Value": value} for name, value in pairs.items()]


class StubCloudWatch:
    """Answers ListMetrics from a canned list and GetMetricData from a map."""

    def __init__(self, metrics=None, values=None, fail_on=None):
        self.metrics = metrics or []
        self.values = values or {}
        self.fail_on = fail_on or set()
        self.list_calls = 0
        self.data_calls = []

    def list_metrics(self, **kwargs):
        if "list_metrics" in self.fail_on:
            raise RuntimeError("AccessDenied")
        self.list_calls += 1
        assert kwargs["Namespace"] == NAMESPACE
        token = kwargs.get("NextToken")
        # Two pages, so pagination is exercised rather than assumed.
        if token is None and len(self.metrics) > 1:
            return {"Metrics": self.metrics[:1], "NextToken": "page-2"}
        if token == "page-2":
            return {"Metrics": self.metrics[1:]}
        return {"Metrics": self.metrics}

    def get_metric_data(self, **kwargs):
        if "get_metric_data" in self.fail_on:
            raise RuntimeError("AccessDenied")
        self.data_calls.append(kwargs)
        results = []
        for query in kwargs["MetricDataQueries"]:
            values = self.values.get(query["Id"], [])
            results.append({
                "Id": query["Id"],
                "Timestamps": [START] * len(values),
                "Values": values,
            })
        return {"MetricDataResults": results}


def service_with(client):
    service = TelemetryService(region_name="us-east-1")
    service._client = client
    return service


def test_the_index_is_keyed_on_every_arn_in_a_series_dimensions():
    """Harness traffic is dimensioned on the companion runtime and carries the
    harness ARN in HarnessId — a record may know either one."""
    client = StubCloudWatch(metrics=[
        {"MetricName": "Invocations", "Dimensions": dims(
            Operation="InvokeAgentRuntimeCommand", Resource=RUNTIME_ARN,
            HarnessId=HARNESS_ARN, Name="harness_writer::DEFAULT",
        )},
        {"MetricName": "Sessions", "Dimensions": dims(
            Operation="InvokeAgentRuntime", Resource=RUNTIME_ARN,
            Name="harness_writer::DEFAULT",
        )},
    ])

    index = service_with(client).metric_index()

    assert RUNTIME_ARN in index
    assert HARNESS_ARN in index, (
        "indexing only on Resource halves the numbers for harness-backed agents"
    )


def test_list_metrics_is_paginated_to_exhaustion():
    client = StubCloudWatch(metrics=[
        {"MetricName": "Invocations", "Dimensions": dims(Resource=RUNTIME_ARN)},
        {"MetricName": "Sessions", "Dimensions": dims(Resource=HARNESS_ARN)},
    ])

    index = service_with(client).metric_index()

    assert client.list_calls == 2
    assert set(index) == {RUNTIME_ARN, HARNESS_ARN}


def test_the_index_is_cached_between_calls():
    client = StubCloudWatch(metrics=[
        {"MetricName": "Invocations", "Dimensions": dims(Resource=RUNTIME_ARN)},
    ])
    service = service_with(client)

    service.metric_index()
    service.metric_index()

    assert client.list_calls == 1


def test_discovered_dimension_sets_are_replayed_verbatim():
    """The measurement that motivates the whole design: a dimension set the
    service invents returns nothing, so it may only send back what it found."""
    discovered = dims(
        Operation="InvokeAgentRuntime", Resource=RUNTIME_ARN,
        Name="bap_default::DEFAULT",
    )
    client = StubCloudWatch(
        metrics=[{"MetricName": "Invocations", "Dimensions": discovered}],
        values={"q0": [7.0]},
    )

    service_with(client).agent_metrics([RUNTIME_ARN], START, END)

    sent = client.data_calls[0]["MetricDataQueries"][0]["MetricStat"]["Metric"]
    assert sent["Dimensions"] == discovered
    assert sent["Namespace"] == NAMESPACE


def test_every_query_goes_out_in_one_batched_call():
    """Never a fan-out per agent: parallel per-agent calls silently serialise
    behind botocore's connection pool."""
    client = StubCloudWatch(metrics=[
        {"MetricName": name, "Dimensions": dims(Resource=arn, Name="x")}
        for arn in (RUNTIME_ARN, HARNESS_ARN)
        for name in ("Invocations", "Latency")
    ])

    service_with(client).agent_metrics([RUNTIME_ARN, HARNESS_ARN], START, END)

    assert len(client.data_calls) == 1
    assert len(client.data_calls[0]["MetricDataQueries"]) == 4


def test_only_metrics_something_renders_are_swept():
    """Each metric in the sweep is billed on every read, so an unrendered one is
    a standing charge for a number nobody sees. Sessions and Throttles were."""
    client = StubCloudWatch(metrics=[
        {"MetricName": name, "Dimensions": dims(Resource=RUNTIME_ARN)}
        for name in (
            "Invocations", "Latency", "SystemErrors", "UserErrors",
            "CPUUsed-vCPUHours", "MemoryUsed-GBHours", "Sessions", "Throttles",
        )
    ])

    service_with(client).agent_metrics([RUNTIME_ARN], START, END)

    asked = {
        query["MetricStat"]["Metric"]["MetricName"]
        for query in client.data_calls[0]["MetricDataQueries"]
    }
    assert "Sessions" not in asked and "Throttles" not in asked
    assert len(asked) == 6


def test_the_window_is_one_bucket_so_the_percentile_is_the_windows_own():
    """Daily buckets reduced with max() give the worst day's p90, a different
    statistic from the window's — and the label said the window's. Verified
    against the live account at 604800s and 2592000s periods."""
    client = StubCloudWatch(metrics=[
        {"MetricName": "Latency", "Dimensions": dims(Resource=RUNTIME_ARN)},
    ])

    service_with(client).agent_metrics([RUNTIME_ARN], START, END)

    query = client.data_calls[0]["MetricDataQueries"][0]
    assert query["MetricStat"]["Period"] == int((END - START).total_seconds())


def test_queries_are_chunked_at_the_api_ceiling():
    """GetMetricData accepts at most 500 queries per call."""
    arns = [f"{RUNTIME_ARN}-{index}" for index in range(300)]
    client = StubCloudWatch(metrics=[
        {"MetricName": name, "Dimensions": dims(Resource=arn, Name="x")}
        for arn in arns
        for name in ("Invocations", "Latency")
    ])

    service_with(client).agent_metrics(arns, START, END)

    assert len(client.data_calls) == 2
    assert all(
        len(call["MetricDataQueries"]) <= 500 for call in client.data_calls
    )


def test_the_widest_series_that_returned_data_wins():
    """A 2-dimension rollup contains its 3-dimension children. Taking the most
    specific drops siblings; summing both double counts."""
    wide = dims(Operation="InvokeAgentRuntime", Resource=RUNTIME_ARN)
    narrow = dims(
        Operation="InvokeAgentRuntime", Resource=RUNTIME_ARN, Name="a::DEFAULT",
    )
    client = StubCloudWatch(
        metrics=[
            {"MetricName": "Invocations", "Dimensions": wide},
            {"MetricName": "Invocations", "Dimensions": narrow},
        ],
        values={"q0": [10.0], "q1": [4.0]},
    )

    metrics = service_with(client).agent_metrics([RUNTIME_ARN], START, END)

    assert metrics[RUNTIME_ARN]["invocations"] == 10.0


def test_a_wide_series_with_no_data_falls_through_to_the_narrow_one():
    """This is the measured case: {Operation, Resource} is empty and only the
    Name-dimensioned series has datapoints."""
    wide = dims(Operation="InvokeAgentRuntime", Resource=RUNTIME_ARN)
    narrow = dims(
        Operation="InvokeAgentRuntime", Resource=RUNTIME_ARN, Name="a::DEFAULT",
    )
    client = StubCloudWatch(
        metrics=[
            {"MetricName": "Invocations", "Dimensions": wide},
            {"MetricName": "Invocations", "Dimensions": narrow},
        ],
        values={"q0": [], "q1": [7.0]},
    )

    metrics = service_with(client).agent_metrics([RUNTIME_ARN], START, END)

    assert metrics[RUNTIME_ARN]["invocations"] == 7.0


def test_latency_is_a_p90_and_counters_are_sums():
    client = StubCloudWatch(
        metrics=[
            {"MetricName": "Invocations", "Dimensions": dims(Resource=RUNTIME_ARN)},
            {"MetricName": "Latency", "Dimensions": dims(Resource=RUNTIME_ARN)},
        ],
        values={"q0": [3.0, 4.0], "q1": [1200.0, 900.0]},
    )

    service = service_with(client)
    metrics = service.agent_metrics([RUNTIME_ARN], START, END)

    stats = {
        query["Id"]: query["MetricStat"]["Stat"]
        for query in client.data_calls[0]["MetricDataQueries"]
    }
    assert set(stats.values()) == {"Sum", "p90"}
    assert metrics[RUNTIME_ARN]["invocations"] == 7.0, "counters accumulate"
    assert metrics[RUNTIME_ARN]["latency_p90_ms"] == 1200.0, "latency takes the peak"


def test_an_agent_with_no_discovered_series_is_absent_not_zero():
    client = StubCloudWatch(metrics=[])

    metrics = service_with(client).agent_metrics([RUNTIME_ARN], START, END)

    assert metrics == {}


def test_a_permission_failure_raises_telemetry_unavailable():
    """The route turns this into sources.cloudwatch=false, never a 5xx."""
    client = StubCloudWatch(fail_on={"list_metrics"})

    try:
        service_with(client).agent_metrics([RUNTIME_ARN], START, END)
    except TelemetryUnavailable:
        return
    raise AssertionError("expected TelemetryUnavailable")


def test_account_wide_series_are_not_read_at_all():
    """`ActiveSessionCount` and Memory's token usage are dimensioned per service,
    so in a shared account they count other tenants. They were on the KPI row
    labelled as ours; the fix was to stop reading them, not to relabel."""
    service = TelemetryService(region_name="us-east-1")

    assert not hasattr(service, "active_sessions")
    assert not hasattr(service, "memory_tokens")


def test_an_identical_read_is_served_from_the_cache():
    """The charge is per metric requested, so a second poll inside the TTL must
    cost nothing. Only the ListMetrics sweep used to be cached, which read like a
    cache and billed like none."""
    client = StubCloudWatch(
        metrics=[
            {"MetricName": "Invocations", "Dimensions": dims(Resource=RUNTIME_ARN)},
        ],
        values={"q0": [7.0]},
    )
    service = service_with(client)

    first = service.agent_metrics([RUNTIME_ARN], START, END)
    second = service.agent_metrics([RUNTIME_ARN], START, END)

    assert len(client.data_calls) == 1
    assert first == second


def test_an_expired_cache_reads_again():
    client = StubCloudWatch(
        metrics=[
            {"MetricName": "Invocations", "Dimensions": dims(Resource=RUNTIME_ARN)},
        ],
        values={"q0": [7.0]},
    )
    service = TelemetryService(region_name="us-east-1", cache_seconds=0)
    service._client = client

    service.agent_metrics([RUNTIME_ARN], START, END)
    service.agent_metrics([RUNTIME_ARN], START, END)

    assert len(client.data_calls) == 2


def test_a_warm_memo_asks_for_one_series_instead_of_every_candidate():
    """Measured on the live account: the registry's 12 ARNs matched 208 series
    across the metric list, because every dimension set mentioning an ARN was
    probed on every sweep. Which rollups CloudWatch populates is a property of
    the namespace, not of the window."""
    wide = dims(Operation="InvokeAgentRuntime", Resource=RUNTIME_ARN)
    narrow = dims(
        Operation="InvokeAgentRuntime", Resource=RUNTIME_ARN, Name="a::DEFAULT",
    )
    client = StubCloudWatch(
        metrics=[
            {"MetricName": "Invocations", "Dimensions": wide},
            {"MetricName": "Invocations", "Dimensions": narrow},
        ],
        values={"q0": [], "q1": [7.0]},
    )
    service = service_with(client)

    service.agent_metrics([RUNTIME_ARN], START, END)
    # Query ids restart per sweep, so the stub's single answer moves to q0.
    client.values = {"q0": [7.0]}
    # A different window, so the result cache cannot answer it.
    later = service.agent_metrics(
        [RUNTIME_ARN], START - timedelta(days=7), START
    )

    assert len(client.data_calls[0]["MetricDataQueries"]) == 2
    assert len(client.data_calls[1]["MetricDataQueries"]) == 1, (
        "the second sweep should ask only the series that answered"
    )
    sent = client.data_calls[1]["MetricDataQueries"][0]["MetricStat"]["Metric"]
    assert sent["Dimensions"] == narrow
    assert later[RUNTIME_ARN]["invocations"] == 7.0


def test_a_memo_that_stops_answering_is_dropped_rather_than_trusted():
    """If the remembered series returns nothing, it may have been the wrong one.
    Re-probing everything next sweep costs one sweep; trusting it forever reports
    an agent as untelemetered for as long as the process lives."""
    wide = dims(Operation="InvokeAgentRuntime", Resource=RUNTIME_ARN)
    narrow = dims(
        Operation="InvokeAgentRuntime", Resource=RUNTIME_ARN, Name="a::DEFAULT",
    )
    client = StubCloudWatch(
        metrics=[
            {"MetricName": "Invocations", "Dimensions": wide},
            {"MetricName": "Invocations", "Dimensions": narrow},
        ],
        values={"q0": [], "q1": [7.0]},
    )
    service = service_with(client)

    service.agent_metrics([RUNTIME_ARN], START, END)
    # The memo is warm but the window it is asked about has no data at all.
    client.values = {}
    service.agent_metrics([RUNTIME_ARN], START - timedelta(days=7), START)
    client.values = {"q0": [], "q1": [3.0]}
    revived = service.agent_metrics(
        [RUNTIME_ARN], START - timedelta(days=14), START - timedelta(days=7)
    )

    assert len(client.data_calls[2]["MetricDataQueries"]) == 2
    assert revived[RUNTIME_ARN]["invocations"] == 3.0


def test_the_error_rate_is_computed_where_the_provenance_is():
    client = StubCloudWatch(
        metrics=[
            {"MetricName": "Invocations", "Dimensions": dims(Resource=RUNTIME_ARN)},
            {"MetricName": "SystemErrors", "Dimensions": dims(Resource=RUNTIME_ARN)},
            {"MetricName": "UserErrors", "Dimensions": dims(Resource=RUNTIME_ARN)},
        ],
        values={"q0": [10.0], "q1": [1.0], "q2": [1.0]},
    )

    metrics = service_with(client).agent_metrics([RUNTIME_ARN], START, END)

    assert metrics[RUNTIME_ARN]["error_rate"] == 0.2


def test_an_error_rate_across_two_dimension_widths_is_not_reported():
    """The selection rule runs per field, so the numerator and the denominator can
    land on different rollups — different populations, and a ratio that read over
    100% on the dashboard."""
    client = StubCloudWatch(
        metrics=[
            # Invocations answers only on the narrow series...
            {"MetricName": "Invocations", "Dimensions": dims(
                Operation="InvokeAgentRuntime", Resource=RUNTIME_ARN)},
            {"MetricName": "Invocations", "Dimensions": dims(
                Operation="InvokeAgentRuntime", Resource=RUNTIME_ARN,
                Name="a::DEFAULT")},
            # ...while SystemErrors answers on the wide one.
            {"MetricName": "SystemErrors", "Dimensions": dims(
                Operation="InvokeAgentRuntime", Resource=RUNTIME_ARN)},
        ],
        values={"q0": [], "q1": [4.0], "q2": [9.0]},
    )

    metrics = service_with(client).agent_metrics([RUNTIME_ARN], START, END)

    assert metrics[RUNTIME_ARN]["invocations"] == 4.0
    assert "error_rate" not in metrics[RUNTIME_ARN], (
        "9 errors over 4 invocations is 225%, which is two populations, not a rate"
    )


def test_no_invocations_means_no_error_rate_rather_than_zero():
    """CloudWatch reporting nothing is not a clean bill of health."""
    client = StubCloudWatch(
        metrics=[
            {"MetricName": "SystemErrors", "Dimensions": dims(Resource=RUNTIME_ARN)},
        ],
        values={"q0": [2.0]},
    )

    metrics = service_with(client).agent_metrics([RUNTIME_ARN], START, END)

    assert "error_rate" not in metrics[RUNTIME_ARN]


def test_an_error_rate_missing_one_of_its_two_series_says_which_it_counted():
    """`SystemErrors` and `UserErrors` are separate series and either can be absent.

    The numerator simply skipped the missing one, so a rate built on half the errors
    was published as *the* error rate — the same silence this module refuses
    everywhere else. The rate is still worth having (an agent that never had a user
    error has no such series at all), so it is reported with its basis stated rather
    than withheld.
    """
    client = StubCloudWatch(
        metrics=[
            {"MetricName": "Invocations", "Dimensions": dims(Resource=RUNTIME_ARN)},
            {"MetricName": "SystemErrors", "Dimensions": dims(Resource=RUNTIME_ARN)},
        ],
        values={"q0": [10.0], "q1": [1.0]},
    )

    metrics = service_with(client).agent_metrics([RUNTIME_ARN], START, END)

    assert metrics[RUNTIME_ARN]["error_rate"] == 0.1
    assert metrics[RUNTIME_ARN]["error_basis"] == ["system_errors"]


def test_a_complete_error_rate_names_both_series():
    client = StubCloudWatch(
        metrics=[
            {"MetricName": "Invocations", "Dimensions": dims(Resource=RUNTIME_ARN)},
            {"MetricName": "SystemErrors", "Dimensions": dims(Resource=RUNTIME_ARN)},
            {"MetricName": "UserErrors", "Dimensions": dims(Resource=RUNTIME_ARN)},
        ],
        # UserErrors was asked for and answered nothing, which for a counter is a
        # genuine zero — not the same thing as never having been published.
        values={"q0": [10.0], "q1": [1.0], "q2": []},
    )

    metrics = service_with(client).agent_metrics([RUNTIME_ARN], START, END)

    assert metrics[RUNTIME_ARN]["error_rate"] == 0.1
    assert metrics[RUNTIME_ARN]["error_basis"] == ["system_errors", "user_errors"]


def test_concurrent_calls_do_not_duplicate_the_sweep():
    """Lock is held for the entire check-sweep-store cycle, so concurrent
    threads that both see 'not fresh' block on the lock and find it fresh when
    their turn comes. Without the lock, they each duplicate the slow sweep."""

    class SlowStubCloudWatch(StubCloudWatch):
        def list_metrics(self, **kwargs):
            time.sleep(0.05)  # Block briefly to ensure concurrency
            return super().list_metrics(**kwargs)

    client = SlowStubCloudWatch(metrics=[
        {"MetricName": "Invocations", "Dimensions": dims(Resource=RUNTIME_ARN)},
        {"MetricName": "Sessions", "Dimensions": dims(Resource=HARNESS_ARN)},
    ])

    service = TelemetryService(region_name="us-east-1")
    service._client = client

    results = []
    errors = []

    def call_series():
        try:
            results.append(service._all_series())
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=call_series) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(results) == 3
    assert client.list_calls == 2, (
        "paginated enumeration (2 pages) should happen once, not three times"
    )


def test_no_deadlock_with_lazy_client_construction():
    """Regression test: _all_series() must not deadlock on first call when
    client is not pre-constructed. The client property takes self._lock,
    and _all_series() holds it across the sweep. If client is acquired
    inside the lock, threading.Lock's non-reentrancy causes a deadlock."""

    mock_client = MagicMock()
    mock_client.list_metrics.return_value = {
        "Metrics": [
            {"MetricName": "Invocations", "Dimensions": dims(Resource=RUNTIME_ARN)},
        ]
    }

    with patch("services.telemetry_service.boto3.client", return_value=mock_client):
        service = TelemetryService(region_name="us-east-1")

        result = []
        error = []

        def call_series():
            try:
                result.append(service._all_series())
            except Exception as e:
                error.append(e)

        thread = threading.Thread(target=call_series, daemon=True)
        thread.start()
        thread.join(timeout=5)

        assert not error, f"Unexpected error: {error[0] if error else None}"
        assert not thread.is_alive(), (
            "Thread deadlocked (did not complete within 5 seconds) — "
            "likely client construction conflict with cache lock"
        )
        assert len(result) == 1
        assert len(result[0]) == 1
