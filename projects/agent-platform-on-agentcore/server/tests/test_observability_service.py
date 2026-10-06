"""USAGE_LOGS delivery is made idempotently, and session usage is folded per thread.

A runtime's vended `USAGE_LOGS` carry per-session vCPU/GB-hours at one-second
granularity, and the session id is this platform's thread id. Enabling them is
a delivery source + a delivery per runtime; both are created only when absent.
Reading them is one Logs Insights query per pass whose window is the whole
business day so far, so the per-session item is a SET of the day's total, never
an ADD of a slice.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from botocore.exceptions import ClientError  # noqa: E402

from services.observability_service import ObservabilityService  # noqa: E402

RUNTIME = "arn:aws:bedrock-agentcore:us-east-1:1:runtime/bap_default-AbCdE12345"
DESTINATION = "arn:aws:logs:us-east-1:1:delivery-destination:bap-usage-logs"


def client_error(code):
    return ClientError({"Error": {"Code": code, "Message": code}}, "op")


class Logs:
    def __init__(self, sources=(), deliveries=(), put_error=None):
        self.sources = list(sources)
        self.deliveries = list(deliveries)
        self.put_error = put_error
        self.calls = []

    def describe_delivery_sources(self, **kwargs):
        return {"deliverySources": [{"name": n, "logType": "USAGE_LOGS"} for n in self.sources]}

    def put_delivery_source(self, **kwargs):
        self.calls.append(("put_delivery_source", kwargs))
        if self.put_error:
            raise self.put_error
        self.sources.append(kwargs["name"])
        return {"deliverySource": {"name": kwargs["name"]}}

    def describe_deliveries(self, **kwargs):
        return {"deliveries": [{"deliverySourceName": n, "deliveryDestinationArn": DESTINATION} for n in self.deliveries]}

    def create_delivery(self, **kwargs):
        self.calls.append(("create_delivery", kwargs))
        self.deliveries.append(kwargs["deliverySourceName"])
        return {"delivery": {"id": "d-1"}}

    created_ms = 0

    def describe_log_groups(self, **kwargs):
        return {"logGroups": [{"logGroupName": kwargs.get("logGroupNamePrefix"), "creationTime": self.created_ms}]}

    def start_query(self, **kwargs):
        self.calls.append(("start_query", kwargs))
        if kwargs["endTime"] * 1000 < self.created_ms:
            raise ClientError({"Error": {"Code": "MalformedQueryException", "Message": "before creation"}}, "StartQuery")
        return {"queryId": "q-1"}

    def get_query_results(self, **kwargs):
        # The shape Logs Insights returns for the real vended rows (measured
        # 2026-09-23): the alias `session_id`, plus a keep-warm session and an
        # empty row, which the real service emits.
        return {
            "status": "Complete",
            "results": [
                [
                    {"field": "session_id", "value": "thread-1"},
                    {"field": "resource_arn", "value": RUNTIME},
                    {"field": "vcpu", "value": "0.0125"},
                    {"field": "gb", "value": "0.5"},
                    {"field": "elapsed", "value": "120"},
                ],
                [
                    {"field": "session_id", "value": "keepwarm-bap-default-000000000000000"},
                    {"field": "resource_arn", "value": RUNTIME},
                    {"field": "vcpu", "value": "0.004836"},
                    {"field": "gb", "value": "0.91"},
                    {"field": "elapsed", "value": "1321"},
                ],
                [],
            ],
        }


class Repo:
    def __init__(self):
        self.items = {}

    def set_fields(self, pk, sk, fields):
        self.items.setdefault((pk, sk), {}).update(fields)


def service(logs, repo=None):
    return ObservabilityService(
        logs=logs, log_group="/aws/vendedlogs/bedrock-agentcore/runtime/USAGE_LOGS/bap",
        destination_arn=DESTINATION, project="bap", repository=repo or Repo(),
        runtime_rates=lambda: {"vcpu_hour": 0.0895, "gb_hour": 0.00945, "source": "price_list"},
    )


def test_ensure_creates_source_and_delivery_for_a_new_runtime():
    logs = Logs()
    assert service(logs).ensure_usage_logs(RUNTIME) == "created"
    names = [call[0] for call in logs.calls]
    assert names == ["put_delivery_source", "create_delivery"]
    put = logs.calls[0][1]
    assert put["logType"] == "USAGE_LOGS"
    assert put["resourceArn"] == RUNTIME
    assert put["name"] == "bap-usage-bap_default-AbCdE12345"
    create = logs.calls[1][1]
    assert create["deliverySourceName"] == put["name"]
    assert create["deliveryDestinationArn"] == DESTINATION


def test_ensure_is_a_no_op_when_both_exist():
    logs = Logs(sources=["bap-usage-bap_default-AbCdE12345"], deliveries=["bap-usage-bap_default-AbCdE12345"])
    assert service(logs).ensure_usage_logs(RUNTIME) == "exists"
    assert logs.calls == []


def test_ensure_reports_an_unsupported_log_type_instead_of_raising():
    logs = Logs(put_error=client_error("ValidationException"))
    assert service(logs).ensure_usage_logs(RUNTIME) == "unsupported"


def test_ensure_without_a_destination_is_off():
    svc = ObservabilityService(logs=Logs(), log_group="", destination_arn="", project="bap",
                               repository=Repo(), runtime_rates=lambda: {})
    assert svc.enabled is False
    assert svc.ensure_usage_logs(RUNTIME) == "off"


def test_collect_sessions_sets_the_days_total_per_session_and_runtime():
    logs = Logs()
    repo = Repo()
    written = service(logs, repo).collect_sessions("2026-09-23")
    assert written == 2
    item = repo.items[("SESSIONS#2026-09", f"D#2026-09-23#S#thread-1#R#{RUNTIME}")]
    assert item["vcpu_hours_micro"] == 12_500
    assert item["gb_hours_micro"] == 500_000
    assert item["elapsed_seconds"] == 120
    assert item["keepwarm"] is False
    # 0.0125 × 0.0895 + 0.5 × 0.00945 = 0.00111875 + 0.004725 = 0.00584375
    assert item["runtime_cost_micros"] == 5_844
    warm = repo.items[("SESSIONS#2026-09", f"D#2026-09-23#S#keepwarm-bap-default-000000000000000#R#{RUNTIME}")]
    assert warm["keepwarm"] is True
    query = logs.calls[0][1]
    assert query["logGroupName"].endswith("/bap")
    # The vended rows nest their fields: attributes.session.id, metrics.agent.runtime.*.
    assert "`attributes.session.id` as session_id" in query["queryString"]
    assert "`metrics.agent.runtime.vcpu.hours.used`" in query["queryString"]
    assert "sum(`attributes.time_elapsed_seconds`)" in query["queryString"]


def test_a_day_before_the_log_group_existed_is_skipped_not_queried():
    """Logs Insights rejects a window that ends before the group's creation time
    (MalformedQueryException, measured live on the first pass after deploy)."""
    logs = Logs()
    logs.created_ms = 1_790_000_000_000  # 2026-09-21T14:13Z
    repo = Repo()
    assert service(logs, repo).collect_sessions("2026-09-20") == 0
    assert logs.calls == []
    # A day that straddles creation is queried from the creation moment.
    written = service(logs, repo).collect_sessions("2026-09-21")
    assert written == 2
    query = [c for c in logs.calls if c[0] == "start_query"][0][1]
    assert query["startTime"] * 1000 >= logs.created_ms
