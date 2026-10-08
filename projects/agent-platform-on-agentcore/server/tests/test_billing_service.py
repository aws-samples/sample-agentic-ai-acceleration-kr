"""Cost Explorer reads for the billed tier.

Cost Explorer is charged per request ($0.01, fact 31) against data that refreshes
a few times a day, so the cache is not an optimisation here — it is the feature.
These tests hold a stub `ce` client that counts calls.
"""
from services.billing_service import (
    BillingService,
    BillingUnavailable,
    component_of,
)


def group(usage_type, cost, quantity, unit="Hrs"):
    """One USAGE_TYPE group.

    `unit` matters: the vCPU-hour and GB-hour quantities are the billed twins of
    two different estimate inputs, and the service tells them apart by unit
    rather than by parsing the usage-type string. A stub that gave every group
    the same unit would let a broken split pass.
    """
    return {
        "Keys": [usage_type],
        "Metrics": {
            "UnblendedCost": {"Amount": str(cost), "Unit": "USD"},
            "UsageQuantity": {"Amount": str(quantity), "Unit": unit},
        },
    }


class StubCostExplorer:
    """Records every request and replays a canned response.

    Shaped from a real 2026-08-16 `GetCostAndUsage` call, including the string
    amounts and the `Estimated` flag — the API returns numbers as strings, and a
    stub that returns floats would hide a `float(...)` that production needs.
    """

    def __init__(self, pages=None, error=None):
        self.requests = []
        self.error = error
        self.pages = pages if pages is not None else [
            {
                "ResultsByTime": [
                    {
                        "TimePeriod": {"Start": "2026-08-15", "End": "2026-08-16"},
                        "Groups": [
                            group("USE1-Runtime:Consumption-based:Memory", 4.2176, 446.3055, "GB-Hours"),
                            group("USE1-Runtime:Consumption-based:vCPU", 0.4695, 5.2458, "vCPU-Hours"),
                            group("USE1-Gateway:Consumption-based:API-Invocations", 0.0096, 1917, "Requests"),
                        ],
                        "Estimated": True,
                    },
                    {
                        "TimePeriod": {"Start": "2026-08-16", "End": "2026-08-17"},
                        "Groups": [
                            group("USE1-Runtime:Consumption-based:vCPU", 0.1, 1.0, "vCPU-Hours"),
                        ],
                        "Estimated": True,
                    },
                ]
            }
        ]

    def get_cost_and_usage(self, **params):
        self.requests.append(params)
        if self.error:
            raise self.error
        page = self.pages[min(len(self.requests) - 1, len(self.pages) - 1)]
        return page


def service(stub, **kwargs):
    svc = BillingService(region_name="us-east-1", **kwargs)
    svc._client = stub
    return svc


def test_costs_are_grouped_by_component_and_summed():
    stub = StubCostExplorer()
    result = service(stub).agentcore_costs("2026-08-15", "2026-08-16")

    assert round(result["total"], 4) == 4.7967
    assert round(result["by_component"]["runtime"]["cost"], 4) == 4.7871
    assert round(result["by_component"]["gateway"]["cost"], 4) == 0.0096
    # vCPU-hours and GB-hours are kept apart: they are the billed twins of two
    # different estimate inputs and must be comparable one to one.
    assert round(result["by_component"]["runtime"]["vcpu_hours"], 4) == 6.2458
    assert round(result["by_component"]["runtime"]["gb_hours"], 4) == 446.3055
    assert result["estimated"] is True


def test_end_date_is_sent_exclusive():
    """CE's TimePeriod.End excludes the last day, so it must be advanced.

    Sending the window's own end date silently drops today from every figure —
    the failure looks like "billing is just lower than expected".
    """
    stub = StubCostExplorer()
    service(stub).agentcore_costs("2026-08-09", "2026-08-16")

    period = stub.requests[0]["TimePeriod"]
    assert period == {"Start": "2026-08-09", "End": "2026-08-17"}


def test_the_service_filter_is_always_applied():
    """This is a shared account (fact 27): unfiltered, the card shows $16k of
    someone else's EC2 and SageMaker."""
    stub = StubCostExplorer()
    service(stub).agentcore_costs("2026-08-15", "2026-08-16")

    filters = stub.requests[0]["Filter"]["And"]
    dimensions = {
        f["Dimensions"]["Key"]: f["Dimensions"]["Values"]
        for f in filters
        if "Dimensions" in f
    }
    assert dimensions["SERVICE"] == ["Amazon Bedrock AgentCore"]
    assert dimensions["REGION"] == ["us-east-1"]
    assert stub.requests[0]["GroupBy"] == [
        {"Type": "DIMENSION", "Key": "USAGE_TYPE"}
    ]


def test_a_second_read_of_the_same_window_costs_nothing():
    stub = StubCostExplorer()
    svc = service(stub)
    svc.agentcore_costs("2026-08-15", "2026-08-16")
    svc.agentcore_costs("2026-08-15", "2026-08-16")

    assert len(stub.requests) == 1


def test_a_different_window_is_a_different_cache_entry():
    stub = StubCostExplorer()
    svc = service(stub)
    svc.agentcore_costs("2026-08-15", "2026-08-16")
    svc.agentcore_costs("2026-07-17", "2026-08-16")

    assert len(stub.requests) == 2


def test_a_failure_raises_billing_unavailable_and_is_not_cached():
    """A failed read must not poison the cache: the usual cause is a missing
    permission that someone is in the middle of adding."""
    stub = StubCostExplorer(error=RuntimeError("AccessDeniedException"))
    svc = service(stub)
    for _ in range(2):
        try:
            svc.agentcore_costs("2026-08-15", "2026-08-16")
        except BillingUnavailable:
            pass
        else:
            raise AssertionError("expected BillingUnavailable")
    assert len(stub.requests) == 2


def test_paginated_responses_are_followed():
    pages = [
        {
            "ResultsByTime": [
                {
                    "TimePeriod": {"Start": "2026-08-15", "End": "2026-08-16"},
                    "Groups": [group("USE1-Runtime:Consumption-based:vCPU", 1.0, 1.0)],
                    "Estimated": False,
                }
            ],
            "NextPageToken": "more",
        },
        {
            "ResultsByTime": [
                {
                    "TimePeriod": {"Start": "2026-08-16", "End": "2026-08-17"},
                    "Groups": [group("USE1-Runtime:Consumption-based:vCPU", 2.0, 2.0)],
                    "Estimated": False,
                }
            ]
        },
    ]
    stub = StubCostExplorer(pages=pages)
    result = service(stub).agentcore_costs("2026-08-15", "2026-08-16")

    assert len(stub.requests) == 2
    assert stub.requests[1]["NextPageToken"] == "more"
    assert result["total"] == 3.0


def test_one_date_stays_one_column_when_its_groups_span_two_pages():
    """Cost Explorer paginates *groups*, so one `TimePeriod` can arrive twice.

    Appending a `daily` row per page occurrence drew the same date as two columns,
    each holding a fraction of that day's cost — a chart saying spending halved on
    a day it did not. The total was right the whole time, which is what kept this
    invisible.
    """
    same_day = {"Start": "2026-08-15", "End": "2026-08-16"}
    pages = [
        {
            "ResultsByTime": [{
                "TimePeriod": same_day,
                "Groups": [group("USE1-Runtime:Consumption-based:vCPU", 1.0, 1.0)],
                "Estimated": False,
            }],
            "NextPageToken": "more",
        },
        {
            "ResultsByTime": [{
                "TimePeriod": same_day,
                "Groups": [group("USE1-Gateway:Consumption-based:API-Invocations",
                                 2.0, 1.0, "Requests")],
                "Estimated": False,
            }]
        },
    ]
    result = service(StubCostExplorer(pages=pages)).agentcore_costs(
        "2026-08-15", "2026-08-16"
    )

    assert result["daily"] == [{"date": "2026-08-15", "cost": 3.0}]
    assert result["total"] == 3.0


def test_only_usage_charges_are_counted():
    """The CE *API* includes credits, refunds and tax unless told otherwise.

    The console excludes them by default and this module was written against what
    the console showed, so a credit landing on AgentCore would have quietly pulled
    the billed total below what the account was charged for usage.
    """
    stub = StubCostExplorer()
    service(stub).agentcore_costs("2026-08-15", "2026-08-16")

    filters = stub.requests[0]["Filter"]["And"]
    dimensions = {
        f["Dimensions"]["Key"]: f["Dimensions"]["Values"]
        for f in filters
        if "Dimensions" in f
    }
    assert dimensions["RECORD_TYPE"] == ["Usage"]


def test_the_total_states_the_scope_it_was_read_at():
    """The figure is one region's, and the page cannot say so unless it is told.

    `agentcore_costs` filters on REGION, so charges AWS books without one are
    outside this total. Carrying the scope in the payload is what lets the panel
    label it instead of implying the whole account.
    """
    stub = StubCostExplorer()
    result = service(stub).agentcore_costs("2026-08-15", "2026-08-16")

    # Verify filters were applied correctly, but tolerate both Dimensions and other clause types
    filters = stub.requests[0]["Filter"]["And"]
    dimensions = {
        f["Dimensions"]["Key"]: f["Dimensions"]["Values"]
        for f in filters
        if "Dimensions" in f
    }
    assert dimensions.get("RECORD_TYPE") == ["Usage"]

    assert result["region"] == "us-east-1"
    assert result["record_types"] == ["Usage"]


def test_unknown_usage_types_land_in_other_rather_than_vanishing():
    stub = StubCostExplorer(pages=[{
        "ResultsByTime": [{
            "TimePeriod": {"Start": "2026-08-15", "End": "2026-08-16"},
            "Groups": [group("USE1-SomethingAWSAddedLastWeek", 9.0, 1.0)],
            "Estimated": False,
        }]
    }])
    result = service(stub).agentcore_costs("2026-08-15", "2026-08-16")

    assert result["by_component"]["other"]["cost"] == 9.0
    assert result["total"] == 9.0


def test_component_of_maps_every_measured_usage_type():
    """The exact strings observed on 2026-08-16, region prefixes included."""
    assert component_of("USE1-Runtime:Consumption-based:vCPU") == "runtime"
    assert component_of("APN2-Runtime:Consumption-based:Memory") == "runtime"
    assert component_of("USE1-Memory:Consumption-based:Short-Term-Memory") == "memory"
    assert component_of("USW2-Memory:Consumption-based:LTM-Storage:Built-in-memory") == "memory"
    assert component_of("USE1-Gateway:Consumption-based:API-Invocations") == "gateway"
    assert component_of("USE1-BrowserTool:Consumption-based:vCPU") == "browser"
    assert component_of("USE1-CodeInterpreter:Consumption-based:Memory") == "code_interpreter"
    assert component_of("USE1-WebSearchTool:Consumption-based:Queries") == "web_search"
    assert component_of("USE1-Knowledge-Base:Consumption-based:Retrieval") == "knowledge_base"
    assert component_of("USE1-Evaluations:Consumption-based:BuiltIn-Input:Tier1") == "evaluations"
    assert component_of("DataTransfer-Regional-Bytes") == "data_transfer"
    assert component_of("USE1-CloudFront-Out-Bytes") == "data_transfer"


def _tag_clauses(filter_block):
    return [c for c in filter_block["And"] if "Tags" in c]


def test_agentcore_total_is_scoped_to_our_platform():
    stub = StubCostExplorer()
    service(stub, platform="bap").agentcore_costs("2026-08-15", "2026-08-16")
    tags = _tag_clauses(stub.requests[0]["Filter"])
    assert tags == [{"Tags": {"Key": "Platform", "Values": ["bap"]}}]


def test_per_agent_is_scoped_to_our_platform():
    stub = StubCostExplorer(pages=[{"ResultsByTime": []}])
    service(stub, platform="bap").per_agent_costs("2026-08-15", "2026-08-16")
    tags = _tag_clauses(stub.requests[0]["Filter"])
    assert tags == [{"Tags": {"Key": "Platform", "Values": ["bap"]}}]


def test_model_rates_are_never_platform_scoped():
    """A rate is not tenant-specific; scoping it would shrink the sample it is
    averaged over and price our tokens at a rate the account never blended."""
    stub = StubCostExplorer(pages=[{"ResultsByTime": []}])
    service(stub, platform="bap").model_rates("2026-08-15", "2026-08-16")
    assert _tag_clauses(stub.requests[0]["Filter"]) == []


def test_model_costs_by_agent_groups_on_agentname_over_bedrock_service():
    pages = [{"ResultsByTime": [{"Groups": [
        {"Keys": ["AgentName$writer"], "Metrics": {"UnblendedCost": {"Amount": "1.50", "Unit": "USD"}}},
        {"Keys": ["AgentName$"], "Metrics": {"UnblendedCost": {"Amount": "9.00", "Unit": "USD"}}},
    ]}]}]
    stub = StubCostExplorer(pages=pages)
    result = service(stub, platform="bap").model_costs_by_agent("2026-08-15", "2026-08-16")
    assert result == {"writer": 1.5}
    # Bedrock service, grouped by the AgentName tag, NOT platform-scoped (tag is ours).
    dims = {f["Dimensions"]["Key"]: f["Dimensions"]["Values"]
            for f in stub.requests[0]["Filter"]["And"] if "Dimensions" in f}
    assert dims["SERVICE"] == ["Amazon Bedrock", "Amazon Bedrock Service"]
    assert stub.requests[0]["GroupBy"] == [{"Type": "TAG", "Key": "AgentName"}]
