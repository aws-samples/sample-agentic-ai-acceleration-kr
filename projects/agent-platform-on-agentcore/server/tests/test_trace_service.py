"""Span queries: both destinations, and a timeout that is not an error.

Fact 6 says spans land in *either* the shared `aws/spans` group or a per-agent
stream under `/aws/bedrock-agentcore/runtimes/...`, depending on Region and agent
creation date — so querying one and stopping shows an empty panel in some
deployments while working fine in others, which is the worst kind of bug to
diagnose.

And Logs Insights is asynchronous: StartQuery then poll, several seconds. A query
that has not finished is "ask again", so it must come back as a result the UI can
render a retry button next to — never as a 5xx, which would put SWR into backoff
and take the retry out of the user's hands.
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.trace_service import TraceService, TracesUnavailable  # noqa: E402

START = datetime(2026, 8, 16, 10, 0, 0)
END = datetime(2026, 8, 16, 10, 5, 0)
SHARED = "aws/spans"
PER_RUNTIME = "/aws/bedrock-agentcore/runtimes/bap_default-nSO-DEFAULT"


class StubLogs:
    """Answers DescribeLogGroups from a list and StartQuery from a script."""

    def __init__(self, groups=None, results=None, statuses=None, fail_on=None):
        self.groups = groups or []
        self.results = results or {}
        # Per query id, the sequence of statuses GetQueryResults reports.
        self.statuses = statuses or {}
        self.fail_on = fail_on or set()
        self.started = []
        self.polls = 0

    def describe_log_groups(self, **kwargs):
        if "describe" in self.fail_on:
            raise RuntimeError("AccessDeniedException")
        prefix = kwargs.get("logGroupNamePrefix", "")
        return {
            "logGroups": [
                {"logGroupName": name}
                for name in self.groups
                if name.startswith(prefix)
            ]
        }

    def start_query(self, **kwargs):
        if "start" in self.fail_on:
            raise RuntimeError("AccessDeniedException")
        self.started.append(kwargs)
        return {"queryId": f"q-{len(self.started)}"}

    def get_query_results(self, queryId):
        self.polls += 1
        sequence = self.statuses.get(queryId, ["Complete"])
        status = sequence[min(self.polls - 1, len(sequence) - 1)]
        return {"status": status, "results": self.results.get(queryId, [])}


def row(**fields):
    return [{"field": name, "value": value} for name, value in fields.items()]


def service_with(client, poll_seconds=10.0):
    service = TraceService(region_name="us-east-1", poll_seconds=poll_seconds,
                           sleep=lambda _seconds: None)
    service._client = client
    return service


def test_the_session_id_is_derived_from_the_thread_id():
    """The join key exists already — nothing new is stored to make traces work."""
    service = service_with(StubLogs())

    assert service.session_id_for("t-abc") == service.session_id_for("t-abc")
    assert service.session_id_for("t-abc") != service.session_id_for("t-xyz")


def test_both_span_destinations_are_discovered():
    service = service_with(StubLogs(groups=[SHARED, PER_RUNTIME, "/aws/ecs/other"]))

    groups = service.span_log_groups()

    assert SHARED in groups
    assert PER_RUNTIME in groups
    assert "/aws/ecs/other" not in groups


def test_a_missing_destination_is_simply_not_queried():
    """StartQuery against a log group that does not exist throws, so only
    discovered groups are asked."""
    service = service_with(StubLogs(groups=[SHARED]))

    assert service.span_log_groups() == [SHARED]


def test_both_discovered_groups_are_queried():
    client = StubLogs(
        groups=[SHARED, PER_RUNTIME],
        results={"q-1": [], "q-2": [row(name="model", durationMs="900")]},
    )

    result = service_with(client).spans_for_session("sess-1", START, END)

    queried_groups = {req["logGroupName"] for req in client.started}
    assert queried_groups == {SHARED, PER_RUNTIME}, (
        "one destination alone shows an empty panel in some deployments"
    )
    assert result["status"] == "ok"
    assert result["log_group"] == PER_RUNTIME


def test_an_incomplete_query_times_out_into_a_status_not_an_exception():
    client = StubLogs(
        groups=[SHARED],
        statuses={"q-1": ["Running", "Running", "Running", "Running", "Running"]},
    )

    result = service_with(client, poll_seconds=1.0).spans_for_session(
        "sess-1", START, END
    )

    assert result["status"] == "timeout"
    assert result["spans"] == []


def test_no_spans_anywhere_is_empty_not_timeout():
    """Distinct answers: "not ready yet, retry" and "there are none" lead the
    user to do different things."""
    client = StubLogs(groups=[SHARED, PER_RUNTIME], results={"q-1": [], "q-2": []})

    result = service_with(client).spans_for_session("sess-1", START, END)

    assert result["status"] == "empty"


def test_spans_are_returned_sorted_by_start_time():
    client = StubLogs(
        groups=[SHARED],
        results={"q-1": [
            row(name="tool", startTimeUnixNano="1786837203000000000", durationMs="1500"),
            row(name="model", startTimeUnixNano="1786837201000000000", durationMs="900"),
        ]},
    )

    spans = service_with(client).spans_for_session("sess-1", START, END)["spans"]

    assert [span["name"] for span in spans] == ["model", "tool"]


def test_a_row_missing_a_duration_survives_rather_than_dropping_the_turn():
    """One malformed span must not cost the user the whole timeline."""
    client = StubLogs(
        groups=[SHARED],
        results={"q-1": [
            row(name="model", startTimeUnixNano="1786837201000000000"),
            row(name="tool", startTimeUnixNano="1786837203000000000", durationMs="1500"),
        ]},
    )

    spans = service_with(client).spans_for_session("sess-1", START, END)["spans"]

    assert len(spans) == 2
    assert spans[0]["duration_ms"] is None


def test_a_query_that_fails_on_one_group_still_reports_the_other():
    class PartlyFailing(StubLogs):
        def start_query(self, **kwargs):
            if kwargs["logGroupName"] == SHARED:
                raise RuntimeError("ResourceNotFoundException")
            return super().start_query(**kwargs)

    client = PartlyFailing(
        groups=[SHARED, PER_RUNTIME],
        results={"q-1": [row(name="model", durationMs="900")]},
    )

    result = service_with(client).spans_for_session("sess-1", START, END)

    assert result["status"] == "ok"


def test_no_permission_at_all_raises_traces_unavailable():
    """The route turns this into sources.traces=false, never a 5xx."""
    client = StubLogs(fail_on={"describe"})

    try:
        service_with(client).spans_for_session("sess-1", START, END)
    except TracesUnavailable:
        return
    raise AssertionError("expected TracesUnavailable")


def test_the_query_filters_on_the_session_id():
    client = StubLogs(groups=[SHARED], results={"q-1": []})

    service_with(client).spans_for_session("sess-abc", START, END)

    assert "sess-abc" in client.started[0]["queryString"]


def test_spans_with_mixed_timestamp_formats_sort_correctly():
    """Test _sort_key handles both nanosecond and string timestamp formats."""
    client = StubLogs(
        groups=[SHARED],
        results={"q-1": [
            row(name="with_string", __timestamp="2026-08-16 10:00:03"),
            row(name="with_nano", startTimeUnixNano="1786837201000000000"),
        ]},
    )

    spans = service_with(client).spans_for_session("sess-1", START, END)["spans"]

    assert len(spans) == 2
    assert spans[0]["name"] == "with_nano"
    assert spans[1]["name"] == "with_string"
