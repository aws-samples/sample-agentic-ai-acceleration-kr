"""Policy decisions per gateway and mode, from the AWS/Bedrock-AgentCore metrics."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.collector_service import CollectorService  # noqa: E402

GW = "gw-abc123"


class Repo:
    def __init__(self):
        self.items = {}

    def set_fields(self, pk, sk, fields):
        self.items.setdefault((pk, sk), {}).update(fields)

    def query_prefix(self, pk, sk_prefix):
        return [dict(v, pk=p, sk=s) for (p, s), v in self.items.items() if p == pk and s.startswith(sk_prefix)]


def _dims(**kv):
    return [{"Name": k, "Value": v} for k, v in kv.items()]


# Live shape (2026-10-10): every decision is published under several rollups of
# the same count, and tools/list filtering is counted as PartiallyAuthorizeActions
# with a ToolName. Only AuthorizeAction + TargetResource + Mode + PolicyEngine is
# one tool call counted once.
CALL_ROLLUPS = [
    _dims(OperationName="AuthorizeAction", TargetResource=GW),
    _dims(OperationName="AuthorizeAction", TargetResource=GW, Mode="{mode}"),
    _dims(OperationName="AuthorizeAction", TargetResource=GW, PolicyEngine="pe-1"),
    _dims(OperationName="AuthorizeAction", TargetResource=GW, Mode="{mode}", PolicyEngine="pe-1"),
    # Allow decisions are also split by the permitting policy.
    _dims(OperationName="AuthorizeAction", TargetResource=GW, Mode="{mode}", PolicyEngine="pe-1", Policy="p-team"),
]
LIST_FILTERING = [
    _dims(OperationName="PartiallyAuthorizeActions", TargetResource=GW, ToolName="bap-platform-tools___lookup_salary", Mode="{mode}", PolicyEngine="pe-1"),
    _dims(OperationName="PartiallyAuthorizeActions", TargetResource=GW, ToolName="bap-platform-tools___calculate", Mode="{mode}"),
]
# Per mode, per metric: the real number of tool-call decisions.
CALLS = {("LOG_ONLY", "AllowDecisions"): 5, ("LOG_ONLY", "DenyDecisions"): 1,
         ("ENFORCE", "AllowDecisions"): 2, ("ENFORCE", "DenyDecisions"): 3}


class CloudWatch:
    """list_metrics returns every rollup that exists (paged); get_metric_data
    returns the same underlying count for each rollup of a decision."""

    def __init__(self):
        self.queries = []
        self.pages = 0

    def _all(self, metric):
        out = []
        for mode in ("LOG_ONLY", "ENFORCE"):
            for template in CALL_ROLLUPS + LIST_FILTERING:
                dims = [{"Name": d["Name"], "Value": d["Value"].replace("{mode}", mode)} for d in template]
                # A rollup without a Mode dimension is one metric across both
                # modes, so it exists once, not once per mode; emitting it only
                # on the first pass keeps list_metrics free of duplicates.
                if any(d["Name"] == "Mode" for d in dims) or mode == "LOG_ONLY":
                    out.append({"MetricName": metric, "Dimensions": dims})
        return out

    def list_metrics(self, Namespace, MetricName, Dimensions, NextToken=None):
        assert Dimensions == [{"Name": "TargetResource", "Value": GW}]
        metrics = self._all(MetricName)
        self.pages += 1
        half = len(metrics) // 2
        if NextToken is None:
            return {"Metrics": metrics[:half], "NextToken": "p2"}
        return {"Metrics": metrics[half:]}

    def get_metric_data(self, MetricDataQueries, StartTime, EndTime):
        self.queries.extend(MetricDataQueries)
        results = []
        for q in MetricDataQueries:
            name = q["MetricStat"]["Metric"]["MetricName"]
            dims = {d["Name"]: d["Value"] for d in q["MetricStat"]["Metric"]["Dimensions"]}
            if dims.get("OperationName") == "PartiallyAuthorizeActions":
                value = 9.0  # one per listed tool per listing — not calls
            elif "Mode" in dims:
                value = float(CALLS[(dims["Mode"], name)])
            else:
                value = float(sum(v for (m, n), v in CALLS.items() if n == name))
            results.append({"Id": q["Id"], "Values": [value] if value else []})
        return {"MetricDataResults": results}


def test_collect_policy_day_counts_only_the_authorize_action_rollup_per_mode():
    repo, cw = Repo(), CloudWatch()
    svc = CollectorService(repository=repo, cloudwatch=cw, pricing=None, registry=None, harness=None, region="us-east-1")
    out = svc.collect_policy_day("2026-10-10", [GW], today="2026-10-11")
    assert out == {"day": "2026-10-10", "written": 1}
    item = repo.items[("GATEWAY_POLICY#2026-10", f"D#2026-10-10#G#{GW}")]
    assert item["by_mode"] == {"LOG_ONLY": {"allow": 5, "deny": 1}, "ENFORCE": {"allow": 2, "deny": 3}}
    assert item["allow_decisions"] == 7 and item["deny_decisions"] == 4
    # AuthorizeAction has no ToolName; the per-tool split is the stream ledger's.
    assert item["deny_by_tool"] == {}
    assert item["complete"] is True
    # Exactly one set per (metric, mode) was queried, from both list pages.
    queried = sorted(
        (q["MetricStat"]["Metric"]["MetricName"], {d["Name"]: d["Value"] for d in q["MetricStat"]["Metric"]["Dimensions"]}["Mode"])
        for q in cw.queries
    )
    assert queried == sorted((m, mode) for m in ("AllowDecisions", "DenyDecisions") for mode in ("LOG_ONLY", "ENFORCE"))
    assert cw.pages == 4
    grouped = svc.policy_decisions("2026-10-01", "2026-10-31")
    assert grouped[GW]["2026-10-10"]["deny_decisions"] == 4


def test_no_gateway_ids_writes_nothing():
    svc = CollectorService(repository=Repo(), cloudwatch=CloudWatch(), pricing=None, registry=None, harness=None, region="us-east-1")
    assert svc.collect_policy_day("2026-10-10", [], today="2026-10-11") == {"day": "2026-10-10", "written": 0}
