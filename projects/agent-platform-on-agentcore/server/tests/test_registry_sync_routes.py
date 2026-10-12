"""
Route-level tests for the deployed-agent sync endpoints.

Covers the HTTP contract the web UI and deploy.sh depend on: the payload shape,
and that a missing configuration surfaces as 503 rather than a 500.
"""
import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.auth as auth  # noqa: E402
from models.harness import HarnessSummary  # noqa: E402
from models.registry import AgentRuntimeSummary, GatewaySummary  # noqa: E402
from services.registry_service import RegistryNotConfigured  # noqa: E402
import routes.registry as registry_routes  # noqa: E402

RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:1:runtime/plain-1"
HARNESS_ARN = "arn:aws:bedrock-agentcore:us-east-1:1:harness/h-1"
GATEWAY_ARN = "arn:aws:bedrock-agentcore:us-east-1:1:gateway/builtin-tools-abc123"


class StubRegistry:
    def __init__(self, error=None, records=None, gateways=None):
        self._error = error
        self._records = list(records or [])
        self._gateways = [] if gateways is None else list(gateways)
        self.created = []

    def agent_records(self):
        if self._error:
            raise self._error
        return list(self._records)

    def list_agent_runtimes(self):
        if self._error:
            raise self._error
        return [
            AgentRuntimeSummary(
                name="strands_agent", agent_runtime_arn=RUNTIME_ARN, status="READY"
            )
        ]

    def list_gateways(self):
        if self._error:
            raise self._error
        return list(self._gateways)

    def gateway_arns(self):
        if self._error:
            raise self._error
        return {}

    def create_record(self, req, owner=None):
        from models.registry import RegistryRecordSummary

        self.created.append(req)
        return RegistryRecordSummary(
            record_id="rec-1",
            name=req.name,
            descriptor_type=req.descriptor_type,
            status="PENDING_APPROVAL",
            harness_arn=req.harness_arn,
            agent_runtime_arn=req.agent_runtime_arn,
        )


class StubHarness:
    def __init__(self, error=None):
        self._error = error

    def list_harnesses(self):
        if self._error:
            raise self._error
        return [
            HarnessSummary(
                harness_id="h-1",
                harness_arn=HARNESS_ARN,
                harness_name="research_agent",
                status="READY",
                runtime_arn=None,
            )
        ]


@pytest.fixture
def client(monkeypatch):
    """App wired to the real routes but a stubbed AWS layer."""
    from services.agent_sync_service import AgentSyncService

    def build(error=None, records=None, gateways=None, knowledge=None):
        service = AgentSyncService(
            registry=StubRegistry(error=error, records=records, gateways=gateways),
            harness=StubHarness(error=error),
            knowledge=knowledge,
        )
        monkeypatch.setattr(registry_routes, "_sync", lambda: service)
        app = FastAPI()
        app.include_router(registry_routes.router)
        # Bypass token verification; these tests are about the payload shape.
        ADMIN = auth.AuthUser(username="alice", groups=["admin"])
        app.dependency_overrides[auth.current_user] = lambda: ADMIN
        return TestClient(app, raise_server_exceptions=False), service

    return build


def test_deployed_endpoint_reports_targets_and_drift_count(client):
    http, _ = client()
    resp = http.get("/api/registry/deployed")

    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2
    assert body["unregistered"] == 2
    assert body["retired"] == 0
    assert {t["kind"] for t in body["targets"]} == {"runtime", "harness"}


def test_drift_count_excludes_retired_so_it_matches_bulk_sync(client):
    """
    The badge must not promise more than "Register all" delivers: a deprecated
    registration is reported separately, not as pending drift.
    """
    from models.registry import DESCRIPTOR_A2A, RegistryRecordSummary

    retired = RegistryRecordSummary(
        record_id="rec-old",
        name="strands_agent",
        descriptor_type=DESCRIPTOR_A2A,
        status="DEPRECATED",
        agent_runtime_arn=RUNTIME_ARN,
    )
    http, service = client(records=[retired])

    body = http.get("/api/registry/deployed").json()
    assert body["unregistered"] == 1  # the harness only
    assert body["retired"] == 1

    registered = http.post("/api/registry/sync").json()["registered"]
    assert len(registered) == body["unregistered"]
    assert all(r["agent_runtime_arn"] != RUNTIME_ARN for r in registered)


def test_sync_endpoint_accepts_an_empty_body(client):
    """deploy.sh and the UI both POST without required fields."""
    http, service = client()
    resp = http.post("/api/registry/sync")

    assert resp.status_code == 200
    assert len(resp.json()["registered"]) == 2
    assert len(service.registry.created) == 2


def test_sync_endpoint_filters_by_name(client):
    http, service = client()
    resp = http.post("/api/registry/sync", json={"targets": ["strands_agent"]})

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["registered"]) == 1
    assert body["registered"][0]["agent_runtime_arn"] == RUNTIME_ARN


def test_deployed_endpoint_includes_gateways_in_the_drift_count(client):
    gateway = GatewaySummary(
        name="builtin_tools",
        gateway_id="builtin-tools-abc123",
        gateway_arn=GATEWAY_ARN,
        gateway_url="https://builtin-tools-abc123.gateway.example.com/mcp",
        status="READY",
        authorizer_type="AWS_IAM",
    )
    http, service = client(gateways=[gateway])

    body = http.get("/api/registry/deployed").json()
    assert body["count"] == 3
    assert body["unregistered"] == 3
    assert {t["kind"] for t in body["targets"]} == {"runtime", "harness", "gateway"}

    registered = http.post("/api/registry/sync").json()["registered"]
    assert len(registered) == body["unregistered"]
    assert [r.descriptor_type for r in service.registry.created].count("MCP") == 1


def test_gateway_listing_hides_knowledge_bases(client):
    """/gateways feeds the register dialog's picker, not just the drift view.

    The endpoint used to call the registry directly, bypassing the exclusion the
    sync listing applies — so an admin could hand-register another user's
    knowledge base as a public MCP record.
    """
    kb_arn = "arn:aws:bedrock-agentcore:us-east-1:1:gateway/product-docs-a1b2c3d4"

    def summary(name, gateway_id, arn):
        return GatewaySummary(
            name=name,
            gateway_id=gateway_id,
            gateway_arn=arn,
            gateway_url=f"https://{gateway_id}.gateway.example.com/mcp",
            status="READY",
            authorizer_type="AWS_IAM",
        )

    class StubKnowledge:
        def all_gateway_arns(self):
            return {kb_arn}

    http, _ = client(
        gateways=[
            summary("product-docs-a1b2c3d4", "product-docs-a1b2c3d4", kb_arn),
            summary("builtin_tools", "builtin-tools-abc123", GATEWAY_ARN),
        ],
        knowledge=StubKnowledge(),
    )

    body = http.get("/api/registry/gateways").json()

    assert [g["gateway_arn"] for g in body["gateways"]] == [GATEWAY_ARN]
    assert body["count"] == 1


def test_runtime_listing_hides_harness_companions(monkeypatch):
    """/runtimes feeds the register dialog's A2A picker.

    Offering a harness's companion runtime there lets an admin hand-register an
    agent that AgentCore refuses to invoke — the record looks fine until the first
    prompt, which answers ValidationException ("managed by a harness").
    """
    companion = AgentRuntimeSummary(
        name="harness_research_agent",
        agent_runtime_arn="arn:aws:bedrock-agentcore:us-east-1:1:runtime/h-1-companion",
        status="READY",
        container_uri="public.ecr.aws/i0n3d3i5/harness-us-east-1:latest",
    )
    plain = AgentRuntimeSummary(
        name="strands_agent",
        agent_runtime_arn=RUNTIME_ARN,
        status="READY",
        container_uri="1.dkr.ecr.us-east-1.amazonaws.com/strands_agent:latest",
    )

    class Stub:
        def list_agent_runtimes(self):
            return [companion, plain]

    monkeypatch.setattr(registry_routes, "_registry", lambda: Stub())
    app = FastAPI()
    app.include_router(registry_routes.router)
    app.dependency_overrides[auth.current_user] = lambda: auth.AuthUser(
        username="alice", groups=["admin"]
    )
    body = TestClient(app).get("/api/registry/runtimes").json()

    assert [r["agent_runtime_arn"] for r in body["runtimes"]] == [RUNTIME_ARN]
    assert body["count"] == 1


def test_unconfigured_registry_returns_503_not_500(client):
    http, _ = client(error=RegistryNotConfigured("AGENT_REGISTRY_ID is not configured"))

    assert http.get("/api/registry/deployed").status_code == 503
    assert http.post("/api/registry/sync").status_code == 503


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
