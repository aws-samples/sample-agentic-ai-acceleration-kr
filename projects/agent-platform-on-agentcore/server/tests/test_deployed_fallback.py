"""With the registry off, the chattable agents are the deployed resources."""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.agent_sync_service import AgentSyncService  # noqa: E402


class _Registry:
    def list_agent_runtimes(self):
        return [
            SimpleNamespace(name="solo", agent_runtime_arn="arn:...:runtime/solo",
                            status="READY", description="d", server_protocol="HTTP",
                            container_uri="1234.dkr.ecr/x:latest"),
            SimpleNamespace(name="companion", agent_runtime_arn="arn:...:runtime/comp",
                            status="READY", description=None, server_protocol="HTTP",
                            container_uri="public.ecr.aws/abc/harness-us-east-1:latest"),
            SimpleNamespace(name="tools", agent_runtime_arn="arn:...:runtime/tools",
                            status="READY", description=None, server_protocol="MCP",
                            container_uri="1234.dkr.ecr/y:latest"),
        ]


class _Harness:
    def list_harnesses(self):
        return [
            SimpleNamespace(harness_name="hx", harness_arn="arn:...:harness/hx",
                            runtime_arn="arn:...:runtime/comp", status="ACTIVE"),
            SimpleNamespace(harness_name="creating", harness_arn="arn:...:harness/cr",
                            runtime_arn=None, status="CREATING"),
        ]


def _svc():
    return AgentSyncService(registry=_Registry(), harness=_Harness())


def test_harness_and_solo_runtime_are_chattable():
    names = {r.name for r in _svc().deployed_agent_records()}
    # companion excluded, MCP excluded, CREATING excluded
    assert names == {"hx", "solo"}


def test_synthetic_ids_and_binding():
    records = {r.name: r for r in _svc().deployed_agent_records()}
    hx = records["hx"]
    assert hx.record_id == "deployed:arn:...:harness/hx"
    assert hx.harness_arn == "arn:...:harness/hx"
    assert hx.agent_runtime_arn == "arn:...:runtime/comp"
    assert hx.status == "APPROVED"
    assert hx.source == "deployed"
    solo = records["solo"]
    assert solo.record_id == "deployed:arn:...:runtime/solo"
    assert solo.agent_runtime_arn == "arn:...:runtime/solo"
    assert solo.harness_arn is None
    assert solo.qualifier == "DEFAULT"


class _TaggedHarness(_Harness):
    def __init__(self, tags):
        self.tags = tags

    def team_tag_of(self, arn):
        value = self.tags[arn]
        if isinstance(value, Exception):
            raise value
        return value


def test_harness_record_carries_its_team_tag():
    # Final review C1: without the team a teamed harness listed (and bound) as shared.
    svc = AgentSyncService(registry=_Registry(), harness=_TaggedHarness({"arn:...:harness/hx": "finance"}))
    records = {r.name: r for r in svc.deployed_agent_records()}
    assert records["hx"].custom_metadata == {"team": "finance"}
    assert records["hx"].visibility_known is True
    assert records["solo"].visibility_known is True and records["solo"].custom_metadata is None


def test_unreadable_tags_mark_the_harness_record_unknown():
    svc = AgentSyncService(
        registry=_Registry(), harness=_TaggedHarness({"arn:...:harness/hx": RuntimeError("AccessDenied")})
    )
    hx = {r.name: r for r in svc.deployed_agent_records()}["hx"]
    assert hx.custom_metadata is None and hx.visibility_known is False


def test_shared_harness_tag_is_known_and_teamless():
    svc = AgentSyncService(registry=_Registry(), harness=_TaggedHarness({"arn:...:harness/hx": None}))
    hx = {r.name: r for r in svc.deployed_agent_records()}["hx"]
    assert hx.custom_metadata is None and hx.visibility_known is True
