"""
Tests for reconciling deployed AgentCore agents with the Agent Registry.

The AWS control plane is faked: these cover the diff logic (what counts as
already registered, what is skipped) rather than boto3 behaviour.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.harness import HarnessSummary  # noqa: E402
from models.registry import (  # noqa: E402
    AgentRuntimeSummary,
    DESCRIPTOR_A2A,
    DESCRIPTOR_MCP,
    GatewaySummary,
    RegistryRecordSummary,
    SyncAgentsRequest,
)
from services.agent_sync_service import AgentSyncService  # noqa: E402

RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:1:runtime/plain-1"
HARNESS_ARN = "arn:aws:bedrock-agentcore:us-east-1:1:harness/h-1"
COMPANION_ARN = "arn:aws:bedrock-agentcore:us-east-1:1:runtime/h-1-companion"
GATEWAY_ARN = "arn:aws:bedrock-agentcore:us-east-1:1:gateway/builtin-tools-abc123"
GATEWAY_URL = "https://builtin-tools-abc123.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"


class FakeRegistry:
    """Stands in for RegistryService, recording writes for assertions."""

    def __init__(self, records=None, runtimes=None, gateways=None, gateway_records=None):
        self._records = list(records or [])
        self._runtimes = list(runtimes or [])
        self._gateways = list(gateways or [])
        self._gateway_records = dict(gateway_records or {})
        self.created = []
        self.status_updates = []

    def agent_records(self):
        return list(self._records)

    def list_agent_runtimes(self):
        return list(self._runtimes)

    def list_gateways(self):
        return list(self._gateways)

    def gateway_arns(self):
        return dict(self._gateway_records)

    def create_record(self, req):
        self.created.append(req)
        record = RegistryRecordSummary(
            record_id=f"rec-{len(self.created)}",
            name=req.name,
            descriptor_type=req.descriptor_type,
            status="PENDING_APPROVAL",
            harness_arn=req.harness_arn,
            agent_runtime_arn=req.agent_runtime_arn,
        )
        # Gateway records are found by ARN, not by the agent-record index.
        if req.gateway_arn:
            self._gateway_records[req.gateway_arn] = record
        else:
            self._records.append(record)
        return record

    def update_status(self, record_id, action):
        self.status_updates.append((record_id, action))
        for index, record in enumerate(self._records):
            if record.record_id == record_id:
                updated = record.model_copy(update={"status": "DEPRECATED"})
                self._records[index] = updated
                return updated
        raise KeyError(record_id)


class FakeHarness:
    def __init__(self, harnesses=None):
        self._harnesses = list(harnesses or [])

    def list_harnesses(self):
        return list(self._harnesses)


def harness(status="READY", runtime_arn=COMPANION_ARN):
    return HarnessSummary(
        harness_id="h-1",
        harness_arn=HARNESS_ARN,
        harness_name="research_agent",
        status=status,
        runtime_arn=runtime_arn,
    )


def runtime(status="READY", server_protocol="HTTP", name="strands_agent", arn=RUNTIME_ARN):
    return AgentRuntimeSummary(
        name=name,
        agent_runtime_arn=arn,
        status=status,
        description="Hand-deployed runtime",
        server_protocol=server_protocol,
    )


def gateway(status="READY", authorizer_type="AWS_IAM", name="builtin_tools"):
    return GatewaySummary(
        name=name,
        gateway_id="builtin-tools-abc123",
        gateway_arn=GATEWAY_ARN,
        gateway_url=GATEWAY_URL,
        status=status,
        authorizer_type=authorizer_type,
    )


def service(records=None, runtimes=None, harnesses=None, gateways=None, gateway_records=None):
    return AgentSyncService(
        registry=FakeRegistry(
            records=records,
            runtimes=runtimes,
            gateways=gateways,
            gateway_records=gateway_records,
        ),
        harness=FakeHarness(harnesses=harnesses),
    )


def test_unregistered_runtime_and_harness_are_both_listed():
    svc = service(runtimes=[runtime()], harnesses=[harness()])
    targets = svc.list_deployed_targets()

    by_arn = {t.arn: t for t in targets}
    assert set(by_arn) == {HARNESS_ARN, RUNTIME_ARN}
    assert by_arn[HARNESS_ARN].kind == "harness"
    assert by_arn[RUNTIME_ARN].kind == "runtime"
    assert all(not t.registered for t in targets)
    assert all(t.reason is None for t in targets)


def test_companion_runtime_is_not_offered_as_its_own_agent():
    """A harness's companion runtime rejects InvokeAgentRuntime, so it isn't a target."""
    svc = service(
        runtimes=[
            runtime(),
            AgentRuntimeSummary(
                name="h-1-companion", agent_runtime_arn=COMPANION_ARN, status="READY"
            ),
        ],
        harnesses=[harness()],
    )
    arns = {t.arn for t in svc.list_deployed_targets()}
    assert COMPANION_ARN not in arns
    assert arns == {HARNESS_ARN, RUNTIME_ARN}


def test_companion_runtime_is_recognised_from_its_own_image():
    """The harness listing is not a reliable source for the companion ARN.

    ListHarnesses omits `environment`, so the companion ARN comes from a
    per-harness GetHarness — and a harness the listing has not caught up to yet,
    or whose hydration failed, contributes nothing to that set. The companion is
    still identifiable on its own: AWS runs it from a published harness image no
    hand-deployed runtime uses. Without this the runtime is registered as its own
    agent and chat dies at invoke time on InvokeAgentRuntime.
    """
    svc = service(
        runtimes=[
            runtime(),
            AgentRuntimeSummary(
                name="harness_research_agent",
                agent_runtime_arn=COMPANION_ARN,
                status="READY",
                container_uri="public.ecr.aws/i0n3d3i5/harness-us-east-1:latest",
            ),
        ],
        harnesses=[harness(runtime_arn=None)],
    )
    arns = {t.arn for t in svc.list_deployed_targets()}
    assert COMPANION_ARN not in arns
    assert arns == {HARNESS_ARN, RUNTIME_ARN}


def test_harness_counts_as_registered_when_only_its_runtime_arn_is_bound():
    record = RegistryRecordSummary(
        record_id="rec-existing",
        name="research_agent",
        descriptor_type=DESCRIPTOR_A2A,
        status="APPROVED",
        agent_runtime_arn=COMPANION_ARN,
    )
    svc = service(records=[record], harnesses=[harness()])
    target = next(t for t in svc.list_deployed_targets() if t.arn == HARNESS_ARN)

    assert target.registered
    assert target.record_id == "rec-existing"
    assert target.record_status == "APPROVED"


def test_not_ready_targets_carry_a_reason_and_are_skipped():
    svc = service(runtimes=[runtime(status="CREATING")], harnesses=[harness(status="CREATE_FAILED")])
    result = svc.sync(SyncAgentsRequest())

    assert result.registered == []
    assert {t.arn for t in result.skipped} == {HARNESS_ARN, RUNTIME_ARN}
    assert all(t.reason for t in result.skipped)


def test_mcp_protocol_runtime_is_listed_but_not_registered_as_an_agent():
    """
    A runtime deployed with `-p MCP` serves tools; it cannot answer a prompt. Binding
    chat to it produces a record that always fails at invoke time, so it must carry a
    reason instead of counting as an unregistered agent — otherwise the drift banner
    invites a click that creates a broken record. (Observed with bap_platform_status.)
    """
    svc = service(runtimes=[runtime(server_protocol="MCP")])
    target = next(t for t in svc.list_deployed_targets() if t.arn == RUNTIME_ARN)

    assert target.kind == "runtime"
    assert not target.registered
    assert "MCP" in (target.reason or "")

    result = svc.sync(SyncAgentsRequest())
    assert result.registered == []
    assert [t.arn for t in result.skipped] == [RUNTIME_ARN]


def test_sync_registers_runtime_against_its_own_arn():
    svc = service(runtimes=[runtime()])
    result = svc.sync(SyncAgentsRequest())

    assert len(result.registered) == 1
    req = svc.registry.created[0]
    assert req.descriptor_type == DESCRIPTOR_A2A
    assert req.agent_runtime_arn == RUNTIME_ARN
    assert req.harness_arn is None
    assert req.qualifier == "DEFAULT"
    assert req.submit_for_approval is True


def test_sync_registers_harness_with_both_arns_and_no_qualifier():
    """Harness records must invoke through InvokeHarness, so the harness ARN leads."""
    svc = service(harnesses=[harness()])
    svc.sync(SyncAgentsRequest())

    req = svc.registry.created[0]
    assert req.harness_arn == HARNESS_ARN
    assert req.agent_runtime_arn == COMPANION_ARN
    assert req.qualifier is None


def test_sync_is_idempotent():
    svc = service(runtimes=[runtime()], harnesses=[harness()])
    first = svc.sync(SyncAgentsRequest())
    second = svc.sync(SyncAgentsRequest())

    assert len(first.registered) == 2
    assert second.registered == []
    assert len(second.skipped) == 2
    assert len(svc.registry.created) == 2


def test_sync_honours_target_filter_by_arn_and_by_name():
    svc = service(runtimes=[runtime()], harnesses=[harness()])
    svc.sync(SyncAgentsRequest(targets=[RUNTIME_ARN]))
    assert [r.agent_runtime_arn for r in svc.registry.created] == [RUNTIME_ARN]

    # deploy.sh passes the deployed name, not the ARN it never reliably parses.
    svc2 = service(runtimes=[runtime()], harnesses=[harness()])
    svc2.sync(SyncAgentsRequest(targets=["research_agent"]))
    assert [r.harness_arn for r in svc2.registry.created] == [HARNESS_ARN]


def test_sync_reports_failures_without_aborting_the_rest():
    svc = service(runtimes=[runtime()], harnesses=[harness()])
    original = svc.registry.create_record

    def flaky(req):
        if req.harness_arn:
            raise RuntimeError("registry rejected the harness card")
        return original(req)

    svc.registry.create_record = flaky
    result = svc.sync(SyncAgentsRequest())

    assert len(result.registered) == 1
    assert len(result.failed) == 1
    assert "rejected" in result.failed[0].error


def deprecated_record(**kwargs):
    return RegistryRecordSummary(
        record_id="rec-old",
        name="strands_agent",
        descriptor_type=DESCRIPTOR_A2A,
        status="DEPRECATED",
        **kwargs,
    )


def test_deprecated_record_does_not_mask_a_live_deployment():
    """
    Regression: deprecation is terminal in AWS, so a deprecated record can never
    bind its deployment again. Counting it as `registered` made the deployment
    permanently unregisterable.
    """
    svc = service(
        records=[deprecated_record(agent_runtime_arn=RUNTIME_ARN)],
        runtimes=[runtime()],
    )
    target = next(t for t in svc.list_deployed_targets() if t.arn == RUNTIME_ARN)

    assert not target.registered
    assert target.retired
    # The retired record is still reported so the UI can explain the state.
    assert target.record_id == "rec-old"
    assert target.record_status == "DEPRECATED"


def test_bulk_sync_does_not_resurrect_a_deprecated_registration():
    """A curator's deprecation must not be undone by an unattended sync."""
    svc = service(
        records=[deprecated_record(agent_runtime_arn=RUNTIME_ARN)],
        runtimes=[runtime()],
    )
    result = svc.sync(SyncAgentsRequest())

    assert result.registered == []
    assert [t.arn for t in result.skipped] == [RUNTIME_ARN]
    assert svc.registry.created == []


def test_naming_a_retired_target_explicitly_re_registers_it():
    """Explicit intent creates a fresh record, since the old one cannot revive."""
    svc = service(
        records=[deprecated_record(agent_runtime_arn=RUNTIME_ARN)],
        runtimes=[runtime()],
    )
    result = svc.sync(SyncAgentsRequest(targets=[RUNTIME_ARN]))

    assert len(result.registered) == 1
    assert result.registered[0].record_id != "rec-old"
    assert svc.registry.created[0].agent_runtime_arn == RUNTIME_ARN


def test_a_live_record_wins_over_a_deprecated_one_for_the_same_arn():
    """After re-registration both records exist; the live one must be reported."""
    live = RegistryRecordSummary(
        record_id="rec-new",
        name="strands_agent",
        descriptor_type=DESCRIPTOR_A2A,
        status="APPROVED",
        agent_runtime_arn=RUNTIME_ARN,
    )
    svc = service(
        records=[deprecated_record(agent_runtime_arn=RUNTIME_ARN), live],
        runtimes=[runtime()],
    )
    target = next(t for t in svc.list_deployed_targets() if t.arn == RUNTIME_ARN)

    assert target.registered
    assert not target.retired
    assert target.record_id == "rec-new"


def test_deprecated_harness_record_is_detected_via_its_companion_runtime():
    svc = service(
        records=[deprecated_record(agent_runtime_arn=COMPANION_ARN)],
        harnesses=[harness()],
    )
    target = next(t for t in svc.list_deployed_targets() if t.arn == HARNESS_ARN)

    assert not target.registered
    assert target.retired


def test_gateway_is_listed_as_a_deployed_target():
    svc = service(gateways=[gateway()])
    target = next(t for t in svc.list_deployed_targets() if t.arn == GATEWAY_ARN)

    assert target.kind == "gateway"
    assert target.gateway_url == GATEWAY_URL
    assert not target.registered
    assert target.reason is None


def test_gateway_registers_as_an_mcp_record_not_an_agent():
    """
    A gateway serves tools rather than answering prompts. An A2A record would put
    it in the chat agent picker as something that cannot hold a conversation, and
    would keep it out of the harness composer's MCP catalog.
    """
    svc = service(gateways=[gateway()])
    result = svc.sync(SyncAgentsRequest())

    assert len(result.registered) == 1
    req = svc.registry.created[0]
    assert req.descriptor_type == DESCRIPTOR_MCP
    assert req.gateway_arn == GATEWAY_ARN
    assert req.remote_url == GATEWAY_URL
    # An MCP descriptor needs a full semver, unlike the A2A "1.0" agent cards.
    assert req.version == "1.0.0"
    assert req.agent_runtime_arn is None
    assert req.harness_arn is None
    assert req.qualifier is None


def test_gateway_sync_is_idempotent():
    svc = service(gateways=[gateway()])
    first = svc.sync(SyncAgentsRequest())
    second = svc.sync(SyncAgentsRequest())

    assert len(first.registered) == 1
    assert second.registered == []
    assert [t.arn for t in second.skipped] == [GATEWAY_ARN]
    assert len(svc.registry.created) == 1


def test_registered_gateway_reports_its_record():
    record = RegistryRecordSummary(
        record_id="rec-gw",
        name="builtin_tools",
        descriptor_type=DESCRIPTOR_MCP,
        status="APPROVED",
    )
    svc = service(gateways=[gateway()], gateway_records={GATEWAY_ARN: record})
    target = next(t for t in svc.list_deployed_targets() if t.arn == GATEWAY_ARN)

    assert target.registered
    assert target.record_id == "rec-gw"
    assert target.record_status == "APPROVED"


def test_not_ready_gateway_is_skipped():
    svc = service(gateways=[gateway(status="UPDATING")])
    result = svc.sync(SyncAgentsRequest())

    assert result.registered == []
    assert [t.arn for t in result.skipped] == [GATEWAY_ARN]


def test_jwt_gateway_is_listed_but_not_registered():
    """
    A harness signs outbound gateway calls with SigV4 from its execution role, so a
    CUSTOM_JWT gateway answers 401 at the first tool call. The harness still reaches
    READY, which is why this is caught here rather than at compose time: registering
    such a gateway would advertise a tool surface nothing here can call.
    """
    svc = service(gateways=[gateway(authorizer_type="CUSTOM_JWT")])
    target = next(t for t in svc.list_deployed_targets() if t.arn == GATEWAY_ARN)

    assert target.kind == "gateway"
    assert not target.registered
    assert "SigV4" in (target.reason or "")

    result = svc.sync(SyncAgentsRequest())
    assert result.registered == []
    assert [t.arn for t in result.skipped] == [GATEWAY_ARN]
    assert svc.registry.created == []


def test_naming_a_jwt_gateway_explicitly_still_does_not_register_it():
    """
    Unlike a deprecated record, this is not a curator's decision to override: the
    resulting record would be unusable no matter who asked for it.
    """
    svc = service(gateways=[gateway(authorizer_type="CUSTOM_JWT")])
    result = svc.sync(SyncAgentsRequest(targets=[GATEWAY_ARN]))

    assert result.registered == []
    assert svc.registry.created == []


def test_gateway_with_unknown_authorizer_is_not_registered():
    """ListGateways may omit the authorizer; guessing AWS_IAM would create a 401 record."""
    svc = service(gateways=[gateway(authorizer_type=None)])
    result = svc.sync(SyncAgentsRequest())

    assert result.registered == []
    assert [t.arn for t in result.skipped] == [GATEWAY_ARN]


def test_gateway_and_agents_register_side_by_side():
    """One sync covers both kinds, each with the descriptor type that fits it."""
    svc = service(runtimes=[runtime()], harnesses=[harness()], gateways=[gateway()])
    result = svc.sync(SyncAgentsRequest())

    by_type = {}
    for req in svc.registry.created:
        by_type.setdefault(req.descriptor_type, []).append(req.name)
    assert len(result.registered) == 3
    assert by_type[DESCRIPTOR_MCP] == ["builtin_tools"]
    assert sorted(by_type[DESCRIPTOR_A2A]) == ["research_agent", "strands_agent"]


def test_gateway_filter_by_arn_and_by_name():
    svc = service(runtimes=[runtime()], gateways=[gateway()])
    svc.sync(SyncAgentsRequest(targets=[GATEWAY_ARN]))
    assert [r.gateway_arn for r in svc.registry.created] == [GATEWAY_ARN]

    svc2 = service(runtimes=[runtime()], gateways=[gateway()])
    svc2.sync(SyncAgentsRequest(targets=["builtin_tools"]))
    assert [r.gateway_arn for r in svc2.registry.created] == [GATEWAY_ARN]


def test_unknown_target_filter_registers_nothing():
    svc = service(runtimes=[runtime()])
    result = svc.sync(SyncAgentsRequest(targets=["does-not-exist"]))

    assert result.registered == []
    assert result.skipped == []
    assert result.failed == []


KB_GATEWAY_ARN = "arn:aws:bedrock-agentcore:us-east-1:1:gateway/product-docs-a1b2c3d4"


class FakeKnowledgeRepository:
    """Stands in for KnowledgeRepository; only the gateway index is used here."""

    def __init__(self, arns=None, error=None):
        self._arns = set(arns or [])
        self._error = error

    def all_gateway_arns(self):
        if self._error:
            raise self._error
        return set(self._arns)


def kb_gateway():
    return GatewaySummary(
        name="product-docs-a1b2c3d4",
        gateway_id="product-docs-a1b2c3d4",
        gateway_arn=KB_GATEWAY_ARN,
        gateway_url="https://product-docs-a1b2c3d4.gateway.example.com/mcp",
        status="READY",
        authorizer_type="AWS_IAM",
    )


def sync_with_knowledge(knowledge, gateways=None):
    return AgentSyncService(
        registry=FakeRegistry(gateways=gateways or []),
        harness=FakeHarness(),
        knowledge=knowledge,
    )


def test_knowledge_base_gateways_are_not_offered_as_registry_targets():
    """
    Each knowledge base owns a gateway. Registering it would publish a private
    knowledge base into the catalog every user's harness composer reads, and an
    admin pressing "Register all" would do it for every user at once.
    """
    svc = sync_with_knowledge(
        FakeKnowledgeRepository(arns=[KB_GATEWAY_ARN]),
        gateways=[kb_gateway(), gateway()],
    )

    arns = {t.arn for t in svc.list_deployed_targets()}

    assert KB_GATEWAY_ARN not in arns
    assert GATEWAY_ARN in arns


def test_a_knowledge_gateway_is_not_registered_even_when_named_explicitly():
    """Excluded from the listing means excluded from sync, filter or no filter."""
    svc = sync_with_knowledge(
        FakeKnowledgeRepository(arns=[KB_GATEWAY_ARN]), gateways=[kb_gateway()]
    )

    response = svc.sync(SyncAgentsRequest(targets=[KB_GATEWAY_ARN]))

    assert response.registered == []
    assert svc.registry.created == []


def test_without_the_knowledge_feature_nothing_changes():
    """An environment with no knowledge table behaves exactly as before."""
    svc = sync_with_knowledge(None, gateways=[gateway()])

    assert GATEWAY_ARN in {t.arn for t in svc.list_deployed_targets()}


def test_an_unreadable_knowledge_table_does_not_break_the_sync_listing():
    """
    Failing open here shows a few extra gateways; failing closed would take the
    whole registry page down. The listing is read-only, so the cost of the former
    is a confusing row, not a wrong write.
    """
    from botocore.exceptions import ClientError

    svc = sync_with_knowledge(
        FakeKnowledgeRepository(
            error=ClientError({"Error": {"Code": "AccessDeniedException"}}, "Scan")
        ),
        gateways=[gateway()],
    )

    assert GATEWAY_ARN in {t.arn for t in svc.list_deployed_targets()}


def test_knowledge_base_gateways_are_not_offered_for_manual_registration():
    """The register dialog's gateway picker reads this listing.

    Excluding knowledge bases from the bulk sync alone leaves the hand-register
    path open, and one hand-registered record publishes a private knowledge base
    just as widely as a bulk one.
    """
    svc = sync_with_knowledge(
        FakeKnowledgeRepository(arns=[KB_GATEWAY_ARN]),
        gateways=[kb_gateway(), gateway()],
    )

    arns = {g.gateway_arn for g in svc.composable_gateways()}

    assert KB_GATEWAY_ARN not in arns
    assert GATEWAY_ARN in arns


def test_composable_gateways_without_the_knowledge_feature_are_unchanged():
    svc = sync_with_knowledge(None, gateways=[gateway()])

    assert [g.gateway_arn for g in svc.composable_gateways()] == [GATEWAY_ARN]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
