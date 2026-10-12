"""Per-item Gets are skipped while the List reports the same revision.

The listings behind Registry, Usage and the deployed-targets picker fan out one
Get per item because the List APIs omit what they need. Live, that was 54 AWS
calls per `GET /api/registry/deployed`, 46 of them Gets. These tests pin the
contract that lets them be skipped without going stale: the List always runs,
and a Get is repeated exactly when the List says the item changed.
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.registry import RegistryRecordSummary  # noqa: E402
from services import registry_service as rs  # noqa: E402
from services.harness_service import HarnessService  # noqa: E402
from services.version_memo import VersionMemo, revision_key  # noqa: E402

T1 = datetime(2026, 10, 11, 1, 0, tzinfo=timezone.utc)
T2 = datetime(2026, 10, 11, 2, 0, tzinfo=timezone.utc)


# --- the memo -------------------------------------------------------------------


def test_memo_hits_only_on_an_equal_revision():
    memo = VersionMemo()
    memo.put("h-1", revision_key("1", "t1", "READY"), "answer")
    assert memo.get("h-1", revision_key("1", "t1", "READY")) == "answer"
    assert memo.get("h-1", revision_key("1", "t2", "READY")) is None
    assert memo.get("h-1", revision_key("1", "t1", "UPDATING")) is None


def test_an_item_the_list_gives_no_revision_for_is_never_memoised():
    # An all-None key would compare equal forever and pin the first answer.
    memo = VersionMemo()
    key = revision_key(None, None, None)
    assert key is None
    memo.put("h-1", key, "answer")
    assert memo.get("h-1", key) is None


# --- harnesses ------------------------------------------------------------------


class FakeHarnessControl:
    def __init__(self):
        self.listed = []
        self.gets = 0
        self.fail_get = False

    def list_harnesses(self, **kwargs):
        return {"harnesses": self.listed}

    def get_harness(self, harnessId):
        self.gets += 1
        if self.fail_get:
            raise RuntimeError("throttled")
        item = next(h for h in self.listed if h["harnessId"] == harnessId)
        return {"harness": {**item, "environment": {"agentCoreRuntimeEnvironment": {
            "agentRuntimeArn": f"arn:runtime/{harnessId}-{item['harnessVersion']}"}}}}


def _harness_service():
    svc = HarnessService.__new__(HarnessService)
    svc._control = FakeHarnessControl()
    svc._hydrated = VersionMemo()
    return svc


def _listed(harness_id, version="1", updated=T1, status="READY"):
    return {"harnessId": harness_id, "arn": f"arn:harness/{harness_id}", "harnessName": harness_id,
            "harnessVersion": version, "updatedAt": updated, "status": status}


def test_unchanged_harnesses_are_not_read_again():
    svc = _harness_service()
    svc._control.listed = [_listed("a"), _listed("b")]
    first = svc.list_harnesses()
    second = svc.list_harnesses()
    assert svc._control.gets == 2
    assert [h.runtime_arn for h in second] == [h.runtime_arn for h in first] == [
        "arn:runtime/a-1", "arn:runtime/b-1"]


def test_an_updated_harness_is_read_again_and_only_that_one():
    svc = _harness_service()
    svc._control.listed = [_listed("a"), _listed("b")]
    svc.list_harnesses()
    svc._control.listed = [_listed("a", version="2", updated=T2), _listed("b")]
    out = svc.list_harnesses()
    assert svc._control.gets == 3
    assert out[0].runtime_arn == "arn:runtime/a-2"


def test_a_status_change_alone_forces_a_read():
    # UpdateHarness is asynchronous: a Get made while UPDATING must not be served
    # after it settles, even if version and updatedAt did not move.
    svc = _harness_service()
    svc._control.listed = [_listed("a", status="UPDATING")]
    svc.list_harnesses()
    svc._control.listed = [_listed("a", status="READY")]
    svc.list_harnesses()
    assert svc._control.gets == 2


def test_a_failed_read_is_retried_on_the_next_listing():
    svc = _harness_service()
    svc._control.listed = [_listed("a")]
    svc._control.fail_get = True
    assert svc.list_harnesses()[0].runtime_arn is None
    svc._control.fail_get = False
    assert svc.list_harnesses()[0].runtime_arn == "arn:runtime/a-1"
    assert svc._control.gets == 2


def test_a_newly_created_harness_is_listed_at_once():
    # The List itself is never memoised: no TTL to wait out after a create.
    svc = _harness_service()
    svc._control.listed = [_listed("a")]
    svc.list_harnesses()
    svc._control.listed = [_listed("a"), _listed("new")]
    assert [h.harness_id for h in svc.list_harnesses()] == ["a", "new"]


# --- runtimes and gateways --------------------------------------------------------


class FakeAgentCoreControl:
    def __init__(self):
        self.runtimes = []
        self.gateways = []
        self.calls = {"get_agent_runtime": 0, "get_gateway": 0}

    def list_agent_runtimes(self, **kwargs):
        return {"agentRuntimes": self.runtimes}

    def get_agent_runtime(self, agentRuntimeId):
        self.calls["get_agent_runtime"] += 1
        item = next(r for r in self.runtimes if r["agentRuntimeId"] == agentRuntimeId)
        return {"environmentVariables": {"MODEL_ID": f"model-v{item['agentRuntimeVersion']}"}}

    def list_gateways(self, **kwargs):
        return {"items": self.gateways}

    def get_gateway(self, gatewayIdentifier):
        self.calls["get_gateway"] += 1
        return {"gatewayArn": f"arn:gateway/{gatewayIdentifier}",
                "gatewayUrl": f"https://{gatewayIdentifier}.example/mcp"}


def _registry_service():
    svc = rs.RegistryService(registry_id="r", region="us-east-1")
    svc._agentcore_control = FakeAgentCoreControl()
    return svc


def _runtime(runtime_id, version="1", updated=T1):
    return {"agentRuntimeId": runtime_id, "agentRuntimeName": runtime_id,
            "agentRuntimeArn": f"arn:runtime/{runtime_id}", "agentRuntimeVersion": version,
            "lastUpdatedAt": updated, "status": "READY"}


def test_runtime_model_is_read_once_per_version(monkeypatch):
    monkeypatch.setattr(rs, "AGENT_RUNTIME_DISCOVERY_REGIONS", [])
    svc = _registry_service()
    control = svc._agentcore_control
    control.runtimes = [_runtime("r1"), _runtime("r2")]
    svc.list_agent_runtimes()
    svc.list_agent_runtimes()
    assert control.calls["get_agent_runtime"] == 2

    # A redeploy bumps the version: its MODEL_ID may have changed with it.
    control.runtimes = [_runtime("r1", version="2", updated=T2), _runtime("r2")]
    out = svc.list_agent_runtimes()
    assert control.calls["get_agent_runtime"] == 3
    assert out[0].model_id == "model-v2"


def test_gateways_are_described_once_per_revision():
    svc = _registry_service()
    control = svc._agentcore_control
    control.gateways = [{"gatewayId": "g1", "name": "g1", "updatedAt": T1, "status": "READY"}]
    svc.list_gateways()
    out = svc.list_gateways()
    assert control.calls["get_gateway"] == 1
    assert out[0].gateway_arn == "arn:gateway/g1"

    control.gateways = [{"gatewayId": "g1", "name": "g1", "updatedAt": T2, "status": "READY"}]
    svc.list_gateways()
    assert control.calls["get_gateway"] == 2


# --- registry records ---------------------------------------------------------------


def test_a_never_approved_record_is_read_once_per_revision():
    svc = _registry_service()
    reads = []
    svc.batch_get_discoverable = lambda ids: {}
    svc.get_record = lambda record_id: reads.append(record_id) or rs.RegistryRecordDetail(
        record_id=record_id, name=record_id, status="DRAFT", custom_metadata={"team": "hr"})

    def listed(updated):
        return RegistryRecordSummary(record_id="d1", name="d1", status="DRAFT",
                                     version="1", updated_at=updated)

    svc._hydrate_all([listed("t1")])
    out = svc._hydrate_all([listed("t1")])
    assert reads == ["d1"] and out[0].team == "hr"

    # An edit (or approval) moves updatedAt or status: read the new revision.
    svc._hydrate_all([listed("t2")])
    assert reads == ["d1", "d1"]
