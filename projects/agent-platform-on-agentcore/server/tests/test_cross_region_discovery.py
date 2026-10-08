"""Runtimes deployed in another region can be registered and invoked from here.

Runtimes are region-scoped, and the platform used to ask only its own region's
control plane — a runtime deployed elsewhere never appeared as a registration
candidate, and even a hand-registered one was invoked against the platform's
region and failed. Discovery now fans out over AGENT_RUNTIME_DISCOVERY_REGIONS,
and the invoke region is read from each ARN rather than assumed.
"""
import os
import sys
from types import SimpleNamespace

from botocore.exceptions import ClientError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import registry_service  # noqa: E402
from services.registry_service import RegistryService  # noqa: E402
from services.streaming_service import StreamingService  # noqa: E402


class _Control:
    def __init__(self, region, runtimes, fail=False):
        self.region = region
        self._runtimes = runtimes
        self._fail = fail
        self.described = []

    def list_agent_runtimes(self, **params):
        if self._fail:
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "nope"}},
                "ListAgentRuntimes",
            )
        return {
            "agentRuntimes": [
                {
                    "agentRuntimeId": rid,
                    "agentRuntimeName": rid,
                    "agentRuntimeArn": f"arn:aws:bedrock-agentcore:{self.region}:1:runtime/{rid}",
                    "status": "READY",
                }
                for rid in self._runtimes
            ]
        }

    def get_agent_runtime(self, agentRuntimeId):
        self.described.append(agentRuntimeId)
        return {
            "protocolConfiguration": {"serverProtocol": "HTTP"},
            "environmentVariables": {"MODEL_ID": f"model-in-{self.region}"},
        }


def _service(monkeypatch, controls, extra_regions):
    monkeypatch.setattr(registry_service, "AGENT_RUNTIME_DISCOVERY_REGIONS", extra_regions)
    svc = RegistryService(registry_id="reg", region="ap-northeast-1")
    svc._agentcore_control = controls["ap-northeast-1"]
    svc._region_controls = {r: c for r, c in controls.items() if r != "ap-northeast-1"}
    return svc


def test_runtimes_from_extra_regions_are_offered(monkeypatch):
    controls = {
        "ap-northeast-1": _Control("ap-northeast-1", ["tokyo"]),
        "ap-northeast-2": _Control("ap-northeast-2", ["seoul"]),
    }
    svc = _service(monkeypatch, controls, ["ap-northeast-2"])

    runtimes = {r.name: r for r in svc.list_agent_runtimes()}

    assert set(runtimes) == {"tokyo", "seoul"}
    assert ":ap-northeast-2:" in runtimes["seoul"].agent_runtime_arn


def test_facts_are_read_from_the_runtime_s_own_region(monkeypatch):
    """GetAgentRuntime for a Seoul runtime must go to Seoul, not to Tokyo."""
    controls = {
        "ap-northeast-1": _Control("ap-northeast-1", ["tokyo"]),
        "ap-northeast-2": _Control("ap-northeast-2", ["seoul"]),
    }
    svc = _service(monkeypatch, controls, ["ap-northeast-2"])

    runtimes = {r.name: r for r in svc.list_agent_runtimes()}

    assert controls["ap-northeast-2"].described == ["seoul"]
    assert controls["ap-northeast-1"].described == ["tokyo"]
    assert runtimes["seoul"].model_id == "model-in-ap-northeast-2"


def test_an_unlistable_region_is_skipped_not_fatal(monkeypatch):
    controls = {
        "ap-northeast-1": _Control("ap-northeast-1", ["tokyo"]),
        "us-east-1": _Control("us-east-1", ["virginia"], fail=True),
    }
    svc = _service(monkeypatch, controls, ["us-east-1"])

    assert [r.name for r in svc.list_agent_runtimes()] == ["tokyo"]


def test_own_region_is_not_scanned_twice(monkeypatch):
    svc = _service(
        monkeypatch,
        {"ap-northeast-1": _Control("ap-northeast-1", ["tokyo"])},
        ["ap-northeast-1", "ap-northeast-1"],
    )
    assert svc._discovery_regions() == ["ap-northeast-1"]


# ── invoke region ────────────────────────────────────────────────────────────

def _streaming():
    svc = StreamingService.__new__(StreamingService)
    svc.agentcore_client = SimpleNamespace(
        region_name="ap-northeast-1",
        agent_runtime_arn="arn:aws:bedrock-agentcore:ap-northeast-1:1:runtime/default",
        qualifier="DEFAULT",
    )
    return svc


def test_runtime_client_targets_the_arn_s_region():
    client = _streaming()._get_agent_client(
        {"agent_runtime_arn": "arn:aws:bedrock-agentcore:ap-northeast-2:1:runtime/seoul"}
    )
    assert client.region_name == "ap-northeast-2"


def test_harness_client_targets_the_arn_s_region():
    client = _streaming()._get_agent_client(
        {"harness_arn": "arn:aws:bedrock-agentcore:us-west-2:1:harness/hx"}
    )
    assert client.region_name == "us-west-2"


def test_same_region_and_malformed_arns_fall_back_to_the_platform_region():
    svc = _streaming()
    same = svc._get_agent_client(
        {"agent_runtime_arn": "arn:aws:bedrock-agentcore:ap-northeast-1:1:runtime/x"}
    )
    odd = svc._get_agent_client({"qualifier": "PROD"})
    assert same.region_name == "ap-northeast-1"
    assert odd.region_name == "ap-northeast-1"
