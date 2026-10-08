"""
Listing harnesses must carry the companion runtime ARN.

ListHarnesses omits `environment` (like `tools`, it is a GetHarness-only
field), so a summary built from the listing alone has runtime_arn=None. The
sync service excludes companion runtimes from the deployed-targets view by
that ARN — with it missing, every harness's companion was offered as its own
agent and got registered as a directly-invokable runtime, which AgentCore
rejects at invoke time ("managed by a harness and cannot be invoked directly").
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.harness_service import HarnessService  # noqa: E402

HARNESS_ID = "agent_x-abc123"
HARNESS_ARN = f"arn:aws:bedrock-agentcore:us-east-1:1:harness/{HARNESS_ID}"
RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:1:runtime/harness_agent_x-r1"


class ListingControl:
    """ListHarnesses answers the summary shape AWS actually returns: no
    environment, no tools. GetHarness carries the full record."""

    def __init__(self, get_fails=False):
        self.get_fails = get_fails
        self.get_calls = []

    def list_harnesses(self, **params):
        return {
            "harnesses": [
                {
                    "harnessId": HARNESS_ID,
                    "arn": HARNESS_ARN,
                    "harnessName": "agent_x",
                    "status": "READY",
                }
            ]
        }

    def get_harness(self, harnessId):
        self.get_calls.append(harnessId)
        if self.get_fails:
            raise RuntimeError("throttled")
        return {
            "harness": {
                "harnessId": HARNESS_ID,
                "arn": HARNESS_ARN,
                "harnessName": "agent_x",
                "status": "READY",
                "environment": {
                    "agentCoreRuntimeEnvironment": {"agentRuntimeArn": RUNTIME_ARN}
                },
            }
        }


def service_with(control):
    service = HarnessService(
        registry=object(),
        region="us-east-1",
        execution_role_arn="arn:aws:iam::1:role/harness",
    )
    service._control = control
    return service


def test_listing_hydrates_the_companion_runtime_arn():
    control = ListingControl()
    summaries = service_with(control).list_harnesses()

    assert control.get_calls == [HARNESS_ID]
    assert summaries[0].runtime_arn == RUNTIME_ARN


def test_listing_survives_a_failed_hydration():
    """A GetHarness failure degrades that row, not the whole listing."""
    summaries = service_with(ListingControl(get_fails=True)).list_harnesses()

    assert len(summaries) == 1
    assert summaries[0].harness_arn == HARNESS_ARN
    assert summaries[0].runtime_arn is None


class TargetControl:
    """ListGatewayTargets, paginated, keyed by the gateway id in the ARN."""

    def __init__(self):
        self.pages = [
            {"items": [{"name": "platform-tools"}], "nextToken": "t2"},
            {"items": [{"name": "web-search"}, {"name": None}]},
        ]
        self.calls = []

    def list_gateway_targets(self, **params):
        self.calls.append(params)
        return self.pages[len(self.calls) - 1]


def test_gateway_target_names_paginates_and_uses_the_arn_id():
    control = TargetControl()
    arn = "arn:aws:bedrock-agentcore:us-east-1:1:gateway/ks-agent-platform-gateway-gwexample03"

    names = service_with(control).gateway_target_names(arn)

    assert names == ["platform-tools", "web-search"]
    # Addressed by the id in the ARN, and the second page followed the token.
    assert control.calls[0]["gatewayIdentifier"] == "ks-agent-platform-gateway-gwexample03"
    assert control.calls[1]["nextToken"] == "t2"


def test_gateway_target_names_are_cached_per_arn():
    """The names change only on redeploy but are read on every gateway Usage open,
    so a second read inside the TTL is served from cache, not another API call."""
    control = TargetControl()
    service = service_with(control)
    arn = "arn:aws:bedrock-agentcore:us-east-1:1:gateway/g-abc"

    first = service.gateway_target_names(arn)
    calls_after_first = len(control.calls)
    second = service.gateway_target_names(arn)

    assert first == second == ["platform-tools", "web-search"]
    assert len(control.calls) == calls_after_first  # no further API calls
