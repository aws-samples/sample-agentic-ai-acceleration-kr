"""The collector turns CloudWatch's vended quantities into priced day items.

Quantities are read per day (`Period=86400`) because that is how the bill is
cut, and the live account showed a whole-window period under-reporting. Every
write is a SET so a day re-read twice holds the last reading, not the sum. A
missing series stays missing — an agent CloudWatch knows nothing about is not
an agent that ran for free.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.collector_service import CollectorService  # noqa: E402

ARN = "arn:aws:bedrock-agentcore:us-east-1:1:runtime/a-1"


class Repo:
    def __init__(self):
        self.items = {}

    def set_fields(self, pk, sk, fields):
        self.items.setdefault((pk, sk), {}).update(fields)

    def query_prefix(self, pk, prefix):
        return [
            {"pk": p, "sk": s, **v}
            for (p, s), v in self.items.items()
            if p == pk and s.startswith(prefix)
        ]

    def put_if_absent(self, pk, sk, item):
        if (pk, sk) in self.items:
            return False
        self.items[(pk, sk)] = dict(item)
        return True

    def get(self, pk, sk):
        return self.items.get((pk, sk))


class CloudWatch:
    def __init__(self, values):
        self.values = values
        self.calls = []

    def get_metric_data(self, **kwargs):
        self.calls.append(kwargs)
        results = []
        for query in kwargs["MetricDataQueries"]:
            name = query["MetricStat"]["Metric"]["MetricName"]
            # A Method-dimensioned query is keyed `"<metric>:<method>"`, so a test
            # can give the gateway's tool requests a different count from its total.
            method = next((d["Value"] for d in query["MetricStat"]["Metric"]["Dimensions"] if d["Name"] == "Method"), None)
            if method is not None:
                name = f"{name}:{method}"
            if name in self.values:
                results.append({"Id": query["Id"], "Timestamps": [kwargs["StartTime"]],
                                "Values": [self.values[name]]})
            else:
                results.append({"Id": query["Id"], "Timestamps": [], "Values": []})
        return {"MetricDataResults": results}


class Pricing:
    def runtime_rates(self):
        return {"vcpu_hour": 0.0895, "gb_hour": 0.00945, "source": "price_list"}

    def rate_card(self):
        return {
            # The published row, verbatim: per invocation, not per thousand.
            "USE1-Gateway:Consumption-based:API-Invocations": {
                "component": "gateway", "usd": 0.000005, "unit": "Invocations",
            },
            "USE1-Memory:Consumption-based:Short-Term-Memory": {
                "component": "memory", "usd": 0.00025, "unit": "Events",
            },
            "USE1-Memory:Consumption-based:Long-Term-Memory-Retrieval": {
                "component": "memory", "usd": 0.0005, "unit": "Memory-Retrieved",
            },
        }


def service(repo, cloudwatch):
    return CollectorService(
        repository=repo, cloudwatch=cloudwatch, pricing=Pricing(),
        registry=None, harness=None, region="us-east-1",
    )


def test_day_period_is_86400_and_cost_is_quantity_times_rate():
    repo = Repo()
    cloudwatch = CloudWatch({"CPUUsed-vCPUHours": 0.3, "MemoryUsed-GBHours": 59.659, "Invocations": 100})

    service(repo, cloudwatch).collect_day("2026-08-25", [ARN], today="2026-09-23")

    stat = cloudwatch.calls[0]["MetricDataQueries"][0]["MetricStat"]
    assert stat["Period"] == 86400
    assert stat["Metric"]["Dimensions"] == [
        {"Name": "Service", "Value": "AgentCore.Runtime"},
        {"Name": "Resource", "Value": ARN},
    ]
    item = repo.items[("RESOURCES#2026-08", f"D#2026-08-25#R#{ARN}")]
    assert item["gb_hours_micro"] == 59_659_000
    assert item["vcpu_hours_micro"] == 300_000
    assert item["invocations"] == 100
    # 0.3 × 0.0895 + 59.659 × 0.00945 = 0.02685 + 0.56377755 = 0.59062755 USD
    assert item["runtime_cost_micros"] == 590_628
    assert item["rate_source"] == "price_list"
    assert item["complete"] is True


def test_today_is_overwritten_not_added():
    repo = Repo()
    service(repo, CloudWatch({"MemoryUsed-GBHours": 10.0})).collect_day("2026-09-23", [ARN], today="2026-09-23")
    service(repo, CloudWatch({"MemoryUsed-GBHours": 12.0})).collect_day("2026-09-23", [ARN], today="2026-09-23")

    item = repo.items[("RESOURCES#2026-09", f"D#2026-09-23#R#{ARN}")]
    assert item["gb_hours_micro"] == 12_000_000
    assert item["complete"] is False


def test_missing_series_leaves_the_day_absent_not_zero():
    repo = Repo()
    service(repo, CloudWatch({})).collect_day("2026-09-22", [ARN], today="2026-09-23")
    assert ("RESOURCES#2026-09", f"D#2026-09-22#R#{ARN}") not in repo.items


def test_gateway_invocations_are_priced_from_the_tool_requests_only():
    """The `Invocations` metric counts every MCP request; the bill counts the tool
    ones. Live 2026-09-20: 470 metered, 264 of them the two-message handshake,
    162 billed. The handshake stays in `invocations` and out of the price."""
    repo = Repo()
    gateway = "arn:aws:bedrock-agentcore:us-east-1:1:gateway/g-1"
    cw = CloudWatch({"Invocations": 2_000, "Invocations:tools/list": 1_200, "Invocations:tools/call": 300})
    service(repo, cw).collect_gateways_day("2026-09-22", [gateway], today="2026-09-23")
    item = repo.items[("GATEWAYS#2026-09", f"D#2026-09-22#G#{gateway}")]
    assert item["invocations"] == 2_000
    assert item["billable_requests"] == 1_500
    assert item["gateway_cost_micros"] == 7_500  # 1,500 × $0.000005
    methods = sorted(
        d["Value"] for q in cw.calls[0]["MetricDataQueries"]
        for d in q["MetricStat"]["Metric"]["Dimensions"] if d["Name"] == "Method"
    )
    assert methods == ["tools/call", "tools/list"]


def test_memory_events_and_retrievals_are_priced_but_storage_is_not():
    repo = Repo()
    memory = "arn:aws:bedrock-agentcore:us-east-1:1:memory/m-1"
    cloudwatch = CloudWatch({"CreationCount": 400, "Invocations": 100})
    service(repo, cloudwatch).collect_memories_day("2026-09-22", [memory], today="2026-09-23")
    item = repo.items[("MEMORIES#2026-09", f"D#2026-09-22#M#{memory}")]
    assert item["events"] == 400
    assert item["retrievals"] == 100
    # 400 × $0.00025 + 100 × $0.0005 = 0.10 + 0.05
    assert item["memory_cost_micros"] == 150_000
    assert item["storage_priced"] is False


def test_resources_read_back_grouped_by_arn_and_day():
    repo = Repo()
    svc = service(repo, CloudWatch({"MemoryUsed-GBHours": 1.0}))
    svc.collect_day("2026-09-22", [ARN], today="2026-09-23")
    svc.collect_day("2026-09-23", [ARN], today="2026-09-23")
    resources = svc.resources("2026-09-01", "2026-09-30")
    assert sorted(resources[ARN]) == ["2026-09-22", "2026-09-23"]
    assert resources[ARN]["2026-09-22"]["complete"] is True


class Registry:
    def agent_records(self):
        return [
            type("R", (), {"record_id": "rec-1", "name": "bap_default", "agent_runtime_arn": ARN, "harness_arn": None})(),
        ]


class Harness:
    def list_harnesses(self, with_tools=False):
        return []


def test_ledger_inputs_key_runtime_days_by_agent_name_and_sum_components():
    repo = Repo()
    svc = CollectorService(repository=repo, cloudwatch=CloudWatch({"MemoryUsed-GBHours": 1.0, "CPUUsed-vCPUHours": 0.0, "Invocations": 100, "Invocations:tools/call": 100}),
                           pricing=Pricing(), registry=Registry(), harness=Harness(), region="us-east-1")
    svc.collect_day("2026-09-22", [ARN], today="2026-09-23")
    gateway = "arn:aws:bedrock-agentcore:us-east-1:1:gateway/g-1"
    svc.collect_gateways_day("2026-09-22", [gateway], today="2026-09-23")

    agent_days, components = svc.ledger_inputs("2026-09-01", "2026-09-23")

    assert agent_days[("bap_default", "2026-09-22")]["gb_hours_micro"] == 1_000_000
    assert agent_days[("bap_default", "2026-09-22")]["runtime_cost_micros"] == 9_450
    assert components == {"runtime": 9_450, "gateway": 500}  # 100 × $0.000005


def test_invocations_and_errors_use_the_operation_dimension_set():
    """`Invocations`/`SystemErrors`/`UserErrors` are not dimensioned on Service;
    CloudWatch keys them by (Name=<runtime>::DEFAULT, Operation, Resource) and an
    inexact set returns nothing — which is why every row read `invocations: None`."""
    repo = Repo()
    cw = CloudWatch({"MemoryUsed-GBHours": 1.0, "Invocations": 42, "SystemErrors": 1})
    service(repo, cw).collect_day("2026-09-22", [ARN], today="2026-09-23")
    specs = {q["MetricStat"]["Metric"]["MetricName"]: q["MetricStat"]["Metric"]["Dimensions"]
             for q in cw.calls[0]["MetricDataQueries"]}
    assert specs["Invocations"] == [
        {"Name": "Name", "Value": "a::DEFAULT"},
        {"Name": "Operation", "Value": "InvokeAgentRuntime"},
        {"Name": "Resource", "Value": ARN},
    ]
    assert specs["SystemErrors"][1] == {"Name": "Operation", "Value": "InvokeAgentRuntime"}
    assert "SessionCount" not in specs
    item = repo.items[("RESOURCES#2026-09", f"D#2026-09-22#R#{ARN}")]
    assert item["invocations"] == 42 and item["system_errors"] == 1


def test_runtime_name_strips_the_id_suffix_and_harness_prefix():
    svc = service(Repo(), CloudWatch({}))
    assert svc.runtime_name("arn:aws:bedrock-agentcore:us-east-1:1:runtime/bap_default-AbCdE12345") == "bap_default"
    assert svc.runtime_name("arn:aws:bedrock-agentcore:us-east-1:1:runtime/harness_platform_ops-ZaBcD33445") == "harness_platform_ops"


def test_agent_name_falls_back_to_the_runtime_id_for_retired_runtimes():
    """A recreated harness gets a new companion runtime; the old one's days are
    still billed under the harness's AgentName tag. Without the fallback those
    days were reconciled under `harness_platform_ops-ZaBcD33445` and never met
    the bill's `platform_ops`."""
    svc = service(Repo(), CloudWatch({}))
    svc.registry = Registry(); svc.harness = Harness()
    old = "arn:aws:bedrock-agentcore:us-east-1:1:runtime/harness_platform_ops-0ldRuntime1"
    names = svc.agent_name_by_arn()
    assert names[ARN] == "bap_default"
    assert svc.agent_name_for(old, names) == "platform_ops"
    assert svc.agent_name_for("arn:aws:bedrock-agentcore:us-east-1:1:runtime/bap_default-FgHiJ67890", names) == "bap_default"
