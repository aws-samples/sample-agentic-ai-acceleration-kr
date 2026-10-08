"""
Tests for the four-step AWS resource chain behind a knowledge base.

The chain takes three to six minutes and runs in a FastAPI background task, so an
ECS restart — deploy, scale-in, failed health check — can land between any two
steps. These tests drive the provisioner the way a restart does: stop after a
step, throw the in-memory driver away, and call `advance` again on nothing but
the DynamoDB record. Nothing may be created twice.

The AWS clients are stubs. What is being tested is the ordering, the
find-then-create fallbacks and what gets written to the record — not boto3.
"""
import os
import sys

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import services.knowledge_provisioner as provisioner_module  # noqa: E402
from models.knowledge import (  # noqa: E402
    SOURCE_S3,
    STATUS_CREATE_FAILED,
    STATUS_CREATING,
    STATUS_READY,
    KnowledgeBaseRecord,
    gateway_name_for,
    now_iso,
)
from services.knowledge_provisioner import (  # noqa: E402
    KnowledgeNotConfigured,
    KnowledgeProvisioner,
)

KB_KEY = "kb_product-docs_a1b2c3d4"
SERVICE_ROLE = "arn:aws:iam::111122223333:role/ap-kb-service"
GATEWAY_ROLE = "arn:aws:iam::111122223333:role/ap-kb-gateway"


def client_error(code, message="denied", operation="Create"):
    return ClientError({"Error": {"Code": code, "Message": message}}, operation)


class FakeRepository:
    """Stands in for KnowledgeRepository; same get/update contract."""

    def __init__(self, record):
        self.records = {record.kb_key: record.model_copy()}

    def get(self, kb_key):
        record = self.records.get(kb_key)
        return record.model_copy() if record else None

    def update(self, kb_key, **fields):
        record = self.records[kb_key]
        updated = record.model_copy(update={**fields, "updated_at": now_iso()})
        # The repository maps empty strings back to None, so mirror that here —
        # teardown relies on it to know a resource is gone.
        for field, value in fields.items():
            if value == "":
                setattr(updated, field, None)
        self.records[kb_key] = updated
        return updated.model_copy()


def _page(items, token):
    """One item per page, so the pagination loops are actually exercised."""
    start = int(token or 0)
    page = items[start:start + 1]
    result = {"items": page}
    if start + 1 < len(items):
        result["next"] = str(start + 1)
    return result


class FakeAgent:
    """The bedrock-agent calls the provisioner makes."""

    def __init__(self):
        self.knowledge_bases = {}
        self.data_sources = {}
        self.calls = []
        self.errors = {}
        self.connector_parameters = []
        # Sync jobs, keyed by id. Teardown has to stop a running one before
        # DeleteDataSource will accept.
        self.jobs = {}
        self.stopped = []

    def seed_job(self, job_id, status="IN_PROGRESS"):
        self.jobs[job_id] = {"ingestionJobId": job_id, "status": status}
        return job_id

    def _maybe_fail(self, operation):
        self.calls.append(operation)
        error = self.errors.get(operation)
        if error:
            if isinstance(error, list):
                if error:
                    raise error.pop(0)
            else:
                raise error

    def seed_knowledge_base(self, name, status="ACTIVE"):
        kb_id = f"KB{len(self.knowledge_bases) + 1:08d}"
        self.knowledge_bases[kb_id] = {
            "knowledgeBaseId": kb_id, "name": name, "status": status
        }
        return kb_id

    def seed_data_source(self, kb_id, name, status="AVAILABLE"):
        ds_id = f"DS{len(self.data_sources) + 1:08d}"
        self.data_sources[ds_id] = {
            "knowledgeBaseId": kb_id, "dataSourceId": ds_id,
            "name": name, "status": status,
        }
        return ds_id

    def create_knowledge_base(self, **kwargs):
        self._maybe_fail("create_knowledge_base")
        assert kwargs["knowledgeBaseConfiguration"] == {
            "type": "MANAGED",
            "managedKnowledgeBaseConfiguration": {"embeddingModelType": "MANAGED"},
        }
        assert kwargs["roleArn"] == SERVICE_ROLE
        assert 33 <= len(kwargs["clientToken"]) <= 256
        kb_id = self.seed_knowledge_base(kwargs["name"])
        return {"knowledgeBase": self.knowledge_bases[kb_id]}

    def list_knowledge_bases(self, **kwargs):
        self.calls.append("list_knowledge_bases")
        page = _page(list(self.knowledge_bases.values()), kwargs.get("nextToken"))
        out = {"knowledgeBaseSummaries": page["items"]}
        if "next" in page:
            out["nextToken"] = page["next"]
        return out

    def get_knowledge_base(self, knowledgeBaseId):
        self.calls.append("get_knowledge_base")
        return {"knowledgeBase": self.knowledge_bases[knowledgeBaseId]}

    def delete_knowledge_base(self, knowledgeBaseId):
        self._maybe_fail("delete_knowledge_base")
        self.knowledge_bases.pop(knowledgeBaseId, None)
        return {}

    def create_data_source(self, **kwargs):
        self._maybe_fail("create_data_source")
        config = kwargs["dataSourceConfiguration"]
        assert config["type"] == "MANAGED_KNOWLEDGE_BASE_CONNECTOR"
        # Recorded rather than asserted inline: which connector is correct depends
        # on the record's source type, so the assertion belongs in the test.
        self.connector_parameters.append(
            config["managedKnowledgeBaseConnectorConfiguration"][
                "connectorParameters"
            ]
        )
        ds_id = self.seed_data_source(kwargs["knowledgeBaseId"], kwargs["name"])
        return {"dataSource": self.data_sources[ds_id]}

    def list_data_sources(self, **kwargs):
        self.calls.append("list_data_sources")
        owned = [
            d for d in self.data_sources.values()
            if d["knowledgeBaseId"] == kwargs["knowledgeBaseId"]
        ]
        page = _page(owned, kwargs.get("nextToken"))
        out = {"dataSourceSummaries": page["items"]}
        if "next" in page:
            out["nextToken"] = page["next"]
        return out

    def get_data_source(self, knowledgeBaseId, dataSourceId):
        self.calls.append("get_data_source")
        return {"dataSource": self.data_sources[dataSourceId]}

    def list_ingestion_jobs(self, **kwargs):
        self._maybe_fail("list_ingestion_jobs")
        wanted = set()
        for f in kwargs.get("filters") or []:
            wanted.update(f.get("values") or [])
        jobs = [
            j for j in self.jobs.values()
            if not wanted or j["status"] in wanted
        ]
        return {"ingestionJobSummaries": jobs}

    def stop_ingestion_job(self, knowledgeBaseId, dataSourceId, ingestionJobId):
        self._maybe_fail("stop_ingestion_job")
        self.stopped.append(ingestionJobId)
        # The real API accepts the stop before the job is finished.
        self.jobs[ingestionJobId]["status"] = "STOPPING"
        return {}

    def get_ingestion_job(self, knowledgeBaseId, dataSourceId, ingestionJobId):
        self.calls.append("get_ingestion_job")
        job = self.jobs[ingestionJobId]
        # Settles one poll after the stop, as the service does.
        if job["status"] == "STOPPING":
            job["status"] = "STOPPED"
        return {"ingestionJob": job}

    def delete_data_source(self, knowledgeBaseId, dataSourceId):
        self._maybe_fail("delete_data_source")
        # The live service refuses while a sync is running *or* stopping, which is
        # the whole reason teardown stops jobs and then retries. A STOPPING job
        # settles after one rejection, as the real one does after ~25s.
        running = [
            j for j in self.jobs.values()
            if j["status"] in ("STARTING", "IN_PROGRESS", "STOPPING")
        ]
        if running:
            for job in running:
                if job["status"] == "STOPPING":
                    job["status"] = "STOPPED"
            raise client_error(
                "ValidationException",
                "There is at least one running ingestion job for this data source",
                "DeleteDataSource",
            )
        self.data_sources.pop(dataSourceId, None)
        return {}


class FakeControl:
    """The bedrock-agentcore-control calls the provisioner makes."""

    # DeleteGatewayTarget returns before the gateway stops counting the target, and
    # DeleteGateway rejects until it does. Reproduced here — with the live service's
    # observed behaviour of failing the first attempt and passing the next — so a
    # teardown that cannot survive the lag fails in the suite rather than leaving a
    # user's knowledge base in DELETE_FAILED with a paid-for gateway behind it.
    DETACH_LAG_ATTEMPTS = 1

    def __init__(self):
        self.gateways = {}
        self.targets = {}
        self.calls = []
        self.errors = {}
        # gatewayId -> DeleteGateway attempts still to be rejected.
        self.detaching = {}

    def _maybe_fail(self, operation):
        self.calls.append(operation)
        error = self.errors.get(operation)
        if error:
            if isinstance(error, list):
                if error:
                    raise error.pop(0)
            else:
                raise error

    def seed_gateway(self, name, status="READY"):
        gateway_id = f"gw-{len(self.gateways) + 1}"
        self.gateways[gateway_id] = {
            "gatewayId": gateway_id,
            "gatewayArn": f"arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/{name}",
            "name": name,
            "status": status,
        }
        return gateway_id

    def seed_target(self, gateway_id, name, status="READY"):
        target_id = f"tg-{len(self.targets) + 1}"
        self.targets[target_id] = {
            "targetId": target_id, "gatewayId": gateway_id,
            "name": name, "status": status,
        }
        return target_id

    def create_gateway(self, **kwargs):
        self._maybe_fail("create_gateway")
        assert kwargs["authorizerType"] == "AWS_IAM"
        assert kwargs["protocolType"] == "MCP"
        assert kwargs["roleArn"] == GATEWAY_ROLE
        gateway_id = self.seed_gateway(kwargs["name"])
        return dict(self.gateways[gateway_id])

    def list_gateways(self, **kwargs):
        self.calls.append("list_gateways")
        page = _page(list(self.gateways.values()), kwargs.get("nextToken"))
        out = {"items": page["items"]}
        if "next" in page:
            out["nextToken"] = page["next"]
        return out

    def get_gateway(self, gatewayIdentifier):
        self.calls.append("get_gateway")
        return dict(self.gateways[gatewayIdentifier])

    def delete_gateway(self, gatewayIdentifier):
        self._maybe_fail("delete_gateway")
        if self.detaching.get(gatewayIdentifier, 0) > 0:
            self.detaching[gatewayIdentifier] -= 1
            raise client_error(
                "ValidationException",
                f"Gateway with ID: {gatewayIdentifier} has targets associated "
                "with it. Delete all targets before deleting the gateway.",
                "DeleteGateway",
            )
        self.gateways.pop(gatewayIdentifier, None)
        return {}

    # The real connector accepts `knowledgeBaseId` on Retrieve and on no other
    # tool: AgenticRetrieveStream rejects it with "is not a recognized
    # parameter". Mirrored here so a tool that cannot be pinned to one knowledge
    # base fails in the suite rather than three minutes into a live provision.
    PINNABLE_TOOLS = {"Retrieve"}

    def create_gateway_target(self, **kwargs):
        self._maybe_fail("create_gateway_target")
        assert kwargs["credentialProviderConfigurations"] == [
            {"credentialProviderType": "GATEWAY_IAM_ROLE"}
        ]
        connector = kwargs["targetConfiguration"]["mcp"]["connector"]
        for configuration in connector["configurations"]:
            unknown = set(configuration.get("parameterValues") or {})
            if configuration["name"] not in self.PINNABLE_TOOLS and unknown:
                raise client_error(
                    "ValidationException",
                    f"Connector target validation failed: Configuration "
                    f"'{configuration['name']}': "
                    f"'{sorted(unknown)[0]}' is not a recognized parameter.",
                    "CreateGatewayTarget",
                )
        target_id = self.seed_target(kwargs["gatewayIdentifier"], kwargs["name"])
        self.targets[target_id]["targetConfiguration"] = kwargs["targetConfiguration"]
        return dict(self.targets[target_id])

    def list_gateway_targets(self, **kwargs):
        self.calls.append("list_gateway_targets")
        owned = [
            t for t in self.targets.values()
            if t["gatewayId"] == kwargs["gatewayIdentifier"]
        ]
        page = _page(owned, kwargs.get("nextToken"))
        out = {"items": page["items"]}
        if "next" in page:
            out["nextToken"] = page["next"]
        return out

    def get_gateway_target(self, gatewayIdentifier, targetId):
        self.calls.append("get_gateway_target")
        return dict(self.targets[targetId])

    def delete_gateway_target(self, gatewayIdentifier, targetId):
        self._maybe_fail("delete_gateway_target")
        self.targets.pop(targetId, None)
        self.detaching[gatewayIdentifier] = self.DETACH_LAG_ATTEMPTS
        return {}


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch):
    """Polling and IAM-propagation waits are real seconds; skip them."""
    monkeypatch.setattr(provisioner_module.time, "sleep", lambda _seconds: None)


def build(record=None, agent=None, control=None):
    record = record or KnowledgeBaseRecord(
        kb_key=KB_KEY, owner_id="user-1", name="Product Docs",
        status=STATUS_CREATING, created_at=now_iso(), updated_at=now_iso(),
    )
    repository = FakeRepository(record)
    return (
        KnowledgeProvisioner(
            repository=repository,
            region="us-east-1",
            service_role_arn=SERVICE_ROLE,
            gateway_role_arn=GATEWAY_ROLE,
            agent=agent or FakeAgent(),
            control=control or FakeControl(),
        ),
        repository,
    )


def test_a_full_run_creates_all_four_resources_and_records_them():
    prov, repo = build()

    result = prov.advance(KB_KEY)

    assert result.status == STATUS_READY
    assert result.kb_id and result.data_source_id
    assert result.gateway_id and result.target_id
    assert result.gateway_arn.startswith("arn:aws:bedrock-agentcore:")


def test_the_target_is_bound_to_this_knowledge_base_only():
    """The target's knowledgeBaseId is what isolates one user's KB from another's."""
    prov, repo = build()

    result = prov.advance(KB_KEY)

    target = prov.control.targets[result.target_id]
    connector = target["targetConfiguration"]["mcp"]["connector"]
    assert connector["source"] == {"connectorId": "bedrock-knowledge-bases"}
    assert [c["name"] for c in connector["configurations"]] == ["Retrieve"]
    for configuration in connector["configurations"]:
        assert configuration["parameterValues"] == {"knowledgeBaseId": result.kb_id}


def test_no_tool_is_exposed_that_cannot_be_pinned_to_one_knowledge_base():
    """A tool without a knowledgeBaseId takes the id from the agent instead.

    The gateway role can read every knowledge base in the account, so an unpinned
    tool would let one user's agent retrieve from another user's knowledge base.
    """
    assert provisioner_module.CONNECTOR_TOOLS
    unpinnable = set(provisioner_module.CONNECTOR_TOOLS) - FakeControl.PINNABLE_TOOLS
    assert not unpinnable, f"cannot pin knowledgeBaseId on {sorted(unpinnable)}"


def test_the_gateway_is_named_so_the_agent_tool_is_readable():
    prov, _ = build()

    result = prov.advance(KB_KEY)

    assert prov.control.gateways[result.gateway_id]["name"] == gateway_name_for(KB_KEY)


def test_a_second_advance_creates_nothing_new():
    """The revive path in GET /api/knowledge calls advance on records already done."""
    prov, repo = build()
    first = prov.advance(KB_KEY)

    prov.agent.calls.clear()
    prov.control.calls.clear()
    second = prov.advance(KB_KEY)

    assert (second.kb_id, second.target_id) == (first.kb_id, first.target_id)
    assert "create_knowledge_base" not in prov.agent.calls
    assert "create_gateway" not in prov.control.calls


@pytest.mark.parametrize("stop_after", [
    "create_knowledge_base",
    "create_data_source",
    "create_gateway",
    "create_gateway_target",
])
def test_a_restart_after_any_step_resumes_without_duplicating(stop_after):
    """
    The core claim of the design.

    The container is killed immediately after an AWS create returns — so the
    resource exists but its id was never written to DynamoDB, the worst case. A
    fresh provisioner over the same record must find the orphan by name and adopt
    it rather than making a second one.
    """
    agent, control = FakeAgent(), FakeControl()
    prov, repo = build(agent=agent, control=control)

    # Crash at the chosen step, after AWS has already done the work.
    boom = RuntimeError("container killed")
    if stop_after in ("create_knowledge_base", "create_data_source"):
        agent.errors[stop_after] = boom
    else:
        control.errors[stop_after] = boom
    prov.advance(KB_KEY)
    assert repo.get(KB_KEY).status == STATUS_CREATE_FAILED

    # A new driver over the same record, with the same AWS state.
    agent.errors.pop(stop_after, None)
    control.errors.pop(stop_after, None)
    resumed = KnowledgeProvisioner(
        repository=repo, region="us-east-1",
        service_role_arn=SERVICE_ROLE, gateway_role_arn=GATEWAY_ROLE,
        agent=agent, control=control,
    )
    result = resumed.advance(KB_KEY)

    assert result.status == STATUS_READY
    assert len(agent.knowledge_bases) == 1
    assert len(agent.data_sources) == 1
    assert len(control.gateways) == 1
    assert len(control.targets) == 1


def test_an_orphan_from_a_crash_before_the_write_is_adopted_not_recreated():
    agent = FakeAgent()
    agent.seed_knowledge_base(KB_KEY)
    prov, _ = build(agent=agent)

    result = prov.advance(KB_KEY)

    assert "create_knowledge_base" not in agent.calls
    assert result.kb_id in agent.knowledge_bases
    assert len(agent.knowledge_bases) == 1


def test_a_concurrent_driver_winning_the_race_is_not_a_failure():
    """
    Two page loads can revive the same record at once. The loser sees
    ConflictException, which means "the other one got there first" — so it
    re-reads instead of failing the knowledge base.
    """
    agent = FakeAgent()

    def create_then_conflict(**kwargs):
        agent.calls.append("create_knowledge_base")
        # The winner's resource lands between our list and our create.
        agent.seed_knowledge_base(KB_KEY)
        raise client_error("ConflictException", "already exists")

    agent.create_knowledge_base = create_then_conflict
    prov, _ = build(agent=agent)

    result = prov.advance(KB_KEY)

    assert result.status == STATUS_READY
    assert len(agent.knowledge_bases) == 1


def test_a_conflict_with_nothing_to_find_still_fails():
    agent = FakeAgent()
    agent.errors["create_knowledge_base"] = client_error("ConflictException")
    prov, repo = build(agent=agent)

    result = prov.advance(KB_KEY)

    assert result.status == STATUS_CREATE_FAILED


def test_the_gateway_role_permission_is_retried_while_it_propagates():
    """
    CreateGatewayTarget rejects the freshly attached role with "lacks
    permission" for up to a minute or two after the role is first used.
    """
    control = FakeControl()
    control.errors["create_gateway_target"] = [
        client_error("ValidationException", "The provided role lacks permission"),
        client_error("ValidationException", "The provided role lacks permission"),
    ]
    prov, _ = build(control=control)

    result = prov.advance(KB_KEY)

    assert result.status == STATUS_READY
    assert control.calls.count("create_gateway_target") == 3


def test_an_unrelated_validation_error_is_not_retried():
    control = FakeControl()
    control.errors["create_gateway_target"] = client_error(
        "ValidationException", "connector bedrock-knowledge-bases has no tool Retrieve"
    )
    prov, _ = build(control=control)

    result = prov.advance(KB_KEY)

    assert result.status == STATUS_CREATE_FAILED
    assert control.calls.count("create_gateway_target") == 1
    assert "no tool Retrieve" in result.failure_reason


def test_a_failed_knowledge_base_stops_the_chain_and_keeps_its_id():
    """The id has to survive: it is the only handle DELETE has for cleanup."""
    agent = FakeAgent()
    prov, repo = build(agent=agent)

    original_get = agent.get_knowledge_base
    agent.get_knowledge_base = lambda knowledgeBaseId: {
        "knowledgeBase": {**original_get(knowledgeBaseId)["knowledgeBase"],
                          "status": "FAILED",
                          "failureReasons": ["embedding model unavailable"]}
    }
    result = prov.advance(KB_KEY)

    assert result.status == STATUS_CREATE_FAILED
    assert "FAILED" in result.failure_reason
    assert result.kb_id in agent.knowledge_bases
    assert "create_data_source" not in agent.calls


def test_status_advances_step_by_step_so_the_ui_can_report_progress():
    seen = []
    prov, repo = build()
    original_update = repo.update

    def spy(kb_key, **fields):
        if "status" in fields:
            seen.append(fields["status"])
        return original_update(kb_key, **fields)

    repo.update = spy
    prov.advance(KB_KEY)

    assert seen == ["CREATING", "DATA_SOURCE", "GATEWAY", "TARGET", "READY"]


def test_teardown_deletes_in_reverse_order():
    prov, repo = build()
    record = prov.advance(KB_KEY)
    prov.agent.calls.clear()
    prov.control.calls.clear()

    prov.teardown(record)

    # Consecutive duplicates collapsed: delete_gateway is retried while the target
    # detaches, which is its own test below.
    deletes = []
    for call in prov.control.calls + prov.agent.calls:
        if call.startswith("delete_") and call != (deletes[-1] if deletes else None):
            deletes.append(call)
    assert deletes == [
        "delete_gateway_target", "delete_gateway",
        "delete_data_source", "delete_knowledge_base",
    ]


def test_teardown_stops_a_running_sync_first():
    """DeleteDataSource refuses while a sync runs, so teardown stops it.

    Without this a knowledge base could not be deleted until whatever sync it was
    running finished on its own, which takes minutes.
    """
    prov, _repo = build()
    record = prov.advance(KB_KEY)
    prov.agent.seed_job("job-running")

    prov.teardown(record)

    assert prov.agent.stopped == ["job-running"]
    assert prov.agent.data_sources == {}


def test_teardown_survives_a_sync_listing_failure():
    """A knowledge base must stay deletable even when the job list is unreadable."""
    prov, _repo = build()
    record = prov.advance(KB_KEY)
    prov.agent.errors["list_ingestion_jobs"] = client_error("ThrottlingException")

    prov.teardown(record)

    assert prov.agent.data_sources == {}


def test_teardown_clears_the_ids_so_a_retry_skips_what_is_gone():
    prov, repo = build()
    record = prov.advance(KB_KEY)

    cleared = prov.teardown(record)
    assert (cleared.target_id, cleared.gateway_id, cleared.kb_id) == (None, None, None)

    prov.agent.calls.clear()
    prov.control.calls.clear()
    prov.teardown(cleared)
    assert not [c for c in prov.agent.calls + prov.control.calls if c.startswith("delete_")]


def test_a_resource_already_gone_counts_as_deleted():
    """Retrying a partial delete must be able to finish."""
    prov, repo = build()
    record = prov.advance(KB_KEY)
    prov.control.errors["delete_gateway"] = client_error("ResourceNotFoundException")

    result = prov.teardown(record)

    assert result.kb_id is None


def test_teardown_waits_out_a_target_that_is_still_detaching():
    """The live service rejected DeleteGateway right after DeleteGatewayTarget.

    Left unhandled this surfaced as a 502 and a DELETE_FAILED record, and the only
    way through was pressing delete a second time.
    """
    prov, repo = build()
    record = prov.advance(KB_KEY)
    prov.control.calls.clear()

    result = prov.teardown(record)

    assert prov.control.calls.count("delete_gateway") == 2
    assert (result.target_id, result.gateway_id, result.kb_id) == (None, None, None)


def test_a_gateway_that_never_detaches_still_fails_rather_than_looping_forever():
    prov, repo = build()
    record = prov.advance(KB_KEY)
    prov.control.DETACH_LAG_ATTEMPTS = provisioner_module._TARGET_DETACH_ATTEMPTS + 1

    with pytest.raises(RuntimeError, match="still reported targets"):
        prov.teardown(record)


def test_a_real_delete_error_propagates_so_the_record_survives():
    prov, repo = build()
    record = prov.advance(KB_KEY)
    prov.agent.errors["delete_knowledge_base"] = client_error("ConflictException")

    with pytest.raises(ClientError):
        prov.teardown(record)


def test_a_missing_record_is_not_an_error_because_delete_can_race_the_revive():
    prov, _ = build()

    assert prov.advance("kb_gone_00000000") is None


def test_an_upload_knowledge_base_gets_the_custom_connector():
    prov, _repo = build()

    prov.advance(KB_KEY)

    agent = prov.agent
    assert agent.connector_parameters == [
        {"type": "CUSTOM", "version": "1", "aclEnabled": False}
    ]


def test_an_s3_knowledge_base_gets_the_s3_connector_scoped_to_its_prefix():
    """The one call a source type changes; everything downstream is identical."""
    prov, _repo = build(record=KnowledgeBaseRecord(
        kb_key=KB_KEY, owner_id="user-1", name="Corp Docs",
        source_type=SOURCE_S3,
        source_config={"bucket_name": "corp-docs", "prefix": "exports/"},
        status=STATUS_CREATING, created_at=now_iso(), updated_at=now_iso(),
    ))

    prov.advance(KB_KEY)

    assert prov.agent.connector_parameters == [{
        "type": "S3",
        "version": "1",
        "connectionConfiguration": {
            "bucketName": "corp-docs",
            # Taken from the service role ARN rather than an STS call.
            "bucketOwnerAccountId": "111122223333",
        },
        "filterConfiguration": {"inclusionPrefixes": ["exports/"]},
    }]


def test_the_retrieval_target_is_the_same_whatever_the_source():
    """Why this feature is cheap: an agent cannot tell the two types apart.

    Same name, so the MCP tool the model sees is identical, and the connector is
    pinned to a knowledgeBaseId either way.
    """
    def target_of(record):
        prov, _repo = build(record=record)
        prov.advance(KB_KEY)
        return [
            (t["name"], t["targetConfiguration"]) for t in prov.control.targets.values()
        ]

    upload = target_of(None)
    s3 = target_of(KnowledgeBaseRecord(
        kb_key=KB_KEY, owner_id="user-1", name="Product Docs",
        source_type=SOURCE_S3, source_config={"bucket_name": "corp-docs"},
        status=STATUS_CREATING, created_at=now_iso(), updated_at=now_iso(),
    ))

    assert upload == s3
    assert upload  # not vacuously equal because both are empty


def test_an_unconfigured_provisioner_refuses_to_run():
    prov = KnowledgeProvisioner(
        repository=None, service_role_arn="", gateway_role_arn=""
    )

    assert prov.enabled is False
    with pytest.raises(KnowledgeNotConfigured):
        prov.advance(KB_KEY)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
