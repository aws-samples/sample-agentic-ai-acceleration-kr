"""
The four AWS resources behind one knowledge base, provisioned idempotently.

    CreateKnowledgeBase -> CreateDataSource -> CreateGateway -> CreateGatewayTarget

Three to six minutes end to end, driven from a FastAPI background task — which
means an ECS restart can land between any two steps. Two properties make that
survivable, and both are load-bearing:

1. Every step is idempotent. It skips if the record already holds the id;
   otherwise it looks the resource up *by name* before creating it, so a crash
   between "AWS created it" and "we wrote the id down" is recoverable rather than
   an orphan. Each id is written the moment it is known.
2. Progress lives in DynamoDB, never in memory. `advance` can be called on any
   record at any time by anything — which is how `GET /api/knowledge` revives a
   driver that died (see knowledge_service.list_visible).

Because of (2), two drivers can work the same record at once. That is safe:
find-then-create means neither duplicates a resource, and the loser of a race
gets ConflictException, which is treated as "the other one got there first".
"""
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

from core.config import AWS_REGION, KB_GATEWAY_ROLE_ARN, KB_SERVICE_ROLE_ARN
from models.knowledge import (
    PROVISIONING_STATUSES,
    STATUS_CREATE_FAILED,
    STATUS_CREATING,
    STATUS_DATA_SOURCE,
    STATUS_GATEWAY,
    STATUS_READY,
    STATUS_TARGET,
    KnowledgeBaseRecord,
    client_token_for,
    connector_parameters_for,
    gateway_name_for,
)
from repositories.knowledge_repository import KnowledgeRepository
from services.registry_service import BOTO_CONFIG

logger = logging.getLogger(__name__)

_AGENT_SERVICE = "bedrock-agent"
_CONTROL_SERVICE = "bedrock-agentcore-control"

# The AgentCore built-in connector that exposes a knowledge base as MCP tools.
# There is no API to enumerate connectors, so the id and tool names come from the
# service documentation; a wrong name fails CreateGatewayTarget loudly.
CONNECTOR_ID = "bedrock-knowledge-bases"
# Retrieve only. The connector also offers AgenticRetrieveStream, but that tool
# accepts no parameterValues at all — CreateGatewayTarget rejects
# `knowledgeBaseId` on it with "is not a recognized parameter". An unpinned tool
# would take the knowledge base id from the agent, and the gateway role can read
# every knowledge base in the account, so exposing it would hand one user's agent
# a way to read another user's knowledge base.
CONNECTOR_TOOLS = ("Retrieve",)

_KB_READY = "ACTIVE"
_KB_FAILED = {"FAILED", "DELETE_UNSUCCESSFUL", "UPDATE_UNSUCCESSFUL"}
_DS_READY = "AVAILABLE"
_DS_FAILED = {"FAILED", "DELETE_UNSUCCESSFUL"}
_GATEWAY_READY = "READY"
_GATEWAY_FAILED = {"FAILED", "UPDATE_UNSUCCESSFUL"}
_TARGET_READY = "READY"
_TARGET_FAILED = {"FAILED", "CREATE_FAILED", "UPDATE_FAILED"}

# A managed knowledge base takes two to five minutes to become ACTIVE.
_POLL_ATTEMPTS = 120
_POLL_INTERVAL_SECONDS = 5.0

# A role's grants are eventually consistent, and this gateway role is used for the
# first time here: CreateGatewayTarget can reject it as missing a permission it
# has. Same treatment as infra/modules/mcp_gateway/scripts/gateway_target.py.
_IAM_PROPAGATION_ATTEMPTS = 12
_IAM_PROPAGATION_INTERVAL_SECONDS = 10.0
_IAM_PROPAGATION_MARKER = "lacks permission"

# DeleteGatewayTarget returns as soon as the deletion is accepted, but the gateway
# goes on counting the target for a moment afterwards and DeleteGateway rejects
# with a ValidationException naming it. Observed window on the live service: under
# two seconds, and the very next attempt succeeds.
_TARGET_DETACH_ATTEMPTS = 30
_TARGET_DETACH_INTERVAL_SECONDS = 2.0
_TARGET_ATTACHED_MARKER = "has targets associated with it"

_LIST_PAGE_SIZE = 100

# DeleteDataSource refuses while a sync is running or stopping, with a
# ValidationException naming it. Teardown stops the job and then retries the delete
# the same way DeleteGateway waits out a detaching target. StopIngestionJob is
# accepted immediately but the job sits in STOPPING for a while — measured at ~25s
# on the live service, so the budget below is generous. The caller has already said
# they want the knowledge base gone, so abandoning a sync is the intent.
_JOB_RUNNING = {"STARTING", "IN_PROGRESS"}
_JOB_STOP_ATTEMPTS = 45
_JOB_STOP_INTERVAL_SECONDS = 4.0
_JOB_RUNNING_MARKER = "running ingestion job"

# Description is 1-200 characters in both services.
_DESCRIPTION_MAX = 200
_FAILURE_REASON_MAX = 900


class KnowledgeNotConfigured(Exception):
    """Raised when knowledge bases are requested but the server is not set up."""


def _error_code(exc: ClientError) -> str:
    return exc.response.get("Error", {}).get("Code", "")


def _error_message(exc: ClientError) -> str:
    return exc.response.get("Error", {}).get("Message", "")


class KnowledgeProvisioner:
    def __init__(
        self,
        repository: Optional[KnowledgeRepository] = None,
        region: Optional[str] = None,
        service_role_arn: Optional[str] = None,
        gateway_role_arn: Optional[str] = None,
        agent: Optional[Any] = None,
        control: Optional[Any] = None,
    ):
        self.repository = repository
        self.region = region or AWS_REGION
        self.service_role_arn = (
            KB_SERVICE_ROLE_ARN if service_role_arn is None else service_role_arn
        )
        self.gateway_role_arn = (
            KB_GATEWAY_ROLE_ARN if gateway_role_arn is None else gateway_role_arn
        )
        self._agent = agent
        self._control = control
        self._client_lock = threading.Lock()

    @property
    def account_id(self) -> str:
        """This account, read off the service role ARN.

        An S3 connector wants `bucketOwnerAccountId`. Taking it from an ARN the
        server already has avoids both an STS call on the create path and an
        sts:GetCallerIdentity grant.
        """
        parts = (self.service_role_arn or "").split(":")
        return parts[4] if len(parts) > 4 else ""

    @property
    def enabled(self) -> bool:
        return bool(
            self.repository and self.service_role_arn and self.gateway_role_arn
        )

    def _require_configured(self) -> None:
        if not self.enabled:
            raise KnowledgeNotConfigured(
                "Knowledge bases require KNOWLEDGE_TABLE, KB_SERVICE_ROLE_ARN and "
                "KB_GATEWAY_ROLE_ARN to be set on the server."
            )

    @property
    def agent(self) -> Any:
        if self._agent is None:
            with self._client_lock:
                if self._agent is None:
                    self._agent = boto3.client(
                        _AGENT_SERVICE, region_name=self.region, config=BOTO_CONFIG
                    )
        return self._agent

    @property
    def control(self) -> Any:
        if self._control is None:
            with self._client_lock:
                if self._control is None:
                    self._control = boto3.client(
                        _CONTROL_SERVICE, region_name=self.region, config=BOTO_CONFIG
                    )
        return self._control

    # ---- the chain -------------------------------------------------------

    def advance(self, kb_key: str) -> Optional[KnowledgeBaseRecord]:
        """Run every step that is not done yet, then mark the record READY.

        Safe to call on a record in any state, including one already finished or
        being deleted — the revive path in `GET /api/knowledge` does exactly that.
        """
        self._require_configured()
        record = self.repository.get(kb_key)
        if record is None:
            # Lost a race with DELETE. Not an error: there is simply nothing left
            # to provision.
            return None
        if record.status not in PROVISIONING_STATUSES | {STATUS_CREATE_FAILED}:
            return record

        try:
            record = self._ensure_knowledge_base(record)
            record = self._ensure_data_source(record)
            record = self._ensure_gateway(record)
            record = self._ensure_target(record)
            return self.repository.update(
                record.kb_key, status=STATUS_READY, failure_reason=""
            )
        except Exception as exc:
            # The ids created so far stay on the record on purpose: they are the
            # only handles DELETE has for cleaning up a half-built knowledge base.
            logger.exception("Provisioning knowledge base %s failed", kb_key)
            return self.repository.update(
                kb_key,
                status=STATUS_CREATE_FAILED,
                failure_reason=str(exc)[:_FAILURE_REASON_MAX],
            )

    def _ensure_knowledge_base(
        self, record: KnowledgeBaseRecord
    ) -> KnowledgeBaseRecord:
        if record.kb_id:
            self._wait_knowledge_base(record.kb_id)
            return record
        record = self.repository.update(record.kb_key, status=STATUS_CREATING)
        kb_id = self._find_or_create(
            lambda: self._find_knowledge_base(record.kb_key),
            lambda: self._create_knowledge_base(record),
        )
        # Written before the wait, not after: a knowledge base that never reaches
        # ACTIVE still exists in AWS, and this id is the only handle DELETE has
        # for cleaning it up.
        record = self.repository.update(record.kb_key, kb_id=kb_id)
        self._wait_knowledge_base(kb_id)
        return record

    def _create_knowledge_base(self, record: KnowledgeBaseRecord) -> str:
        response = self.agent.create_knowledge_base(
            clientToken=client_token_for(record.kb_key, "knowledge-base"),
            # The platform key, not the display name: it is unique, matches the
            # bedrock-agent Name pattern, and is what find-then-create looks for.
            name=record.kb_key,
            description=self._description(record),
            roleArn=self.service_role_arn,
            knowledgeBaseConfiguration={
                "type": "MANAGED",
                # No embeddingModelArn and no embeddingModelConfiguration: a
                # MANAGED knowledge base rejects both, and embeddingModelType
                # cannot be changed afterwards.
                "managedKnowledgeBaseConfiguration": {
                    "embeddingModelType": "MANAGED"
                },
            },
        )
        return response["knowledgeBase"]["knowledgeBaseId"]

    def _find_knowledge_base(self, name: str) -> Optional[str]:
        for summary in self._paginate(
            self.agent.list_knowledge_bases, "knowledgeBaseSummaries"
        ):
            if summary.get("name") == name:
                return summary.get("knowledgeBaseId")
        return None

    def _wait_knowledge_base(self, kb_id: str) -> Dict[str, Any]:
        return self._wait_for(
            f"knowledge base {kb_id}",
            lambda: self.agent.get_knowledge_base(knowledgeBaseId=kb_id)[
                "knowledgeBase"
            ],
            _KB_READY,
            _KB_FAILED,
        )

    def _ensure_data_source(self, record: KnowledgeBaseRecord) -> KnowledgeBaseRecord:
        if record.data_source_id:
            self._wait_data_source(record.kb_id, record.data_source_id)
            return record
        self.repository.update(record.kb_key, status=STATUS_DATA_SOURCE)
        data_source_id = self._find_or_create(
            lambda: self._find_data_source(record.kb_id, record.kb_key),
            lambda: self._create_data_source(record),
        )
        record = self.repository.update(
            record.kb_key, data_source_id=data_source_id
        )
        self._wait_data_source(record.kb_id, data_source_id)
        return record

    def _create_data_source(self, record: KnowledgeBaseRecord) -> str:
        response = self.agent.create_data_source(
            knowledgeBaseId=record.kb_id,
            clientToken=client_token_for(record.kb_key, "data-source"),
            name=record.kb_key,
            description=self._description(record),
            dataSourceConfiguration={
                "type": "MANAGED_KNOWLEDGE_BASE_CONNECTOR",
                # The connector is the only thing a source type changes here. The
                # gateway and target built on top are identical either way, which
                # is why an agent cannot tell the two apart.
                "managedKnowledgeBaseConnectorConfiguration": {
                    "connectorParameters": connector_parameters_for(
                        record.source_type, record.source_config, self.account_id
                    )
                },
            },
        )
        return response["dataSource"]["dataSourceId"]

    def _find_data_source(self, kb_id: str, name: str) -> Optional[str]:
        for summary in self._paginate(
            self.agent.list_data_sources,
            "dataSourceSummaries",
            knowledgeBaseId=kb_id,
        ):
            if summary.get("name") == name:
                return summary.get("dataSourceId")
        return None

    def _wait_data_source(self, kb_id: str, data_source_id: str) -> Dict[str, Any]:
        return self._wait_for(
            f"data source {data_source_id}",
            lambda: self.agent.get_data_source(
                knowledgeBaseId=kb_id, dataSourceId=data_source_id
            )["dataSource"],
            _DS_READY,
            _DS_FAILED,
        )

    def _ensure_gateway(self, record: KnowledgeBaseRecord) -> KnowledgeBaseRecord:
        name = gateway_name_for(record.kb_key)
        if record.gateway_id and record.gateway_arn:
            self._wait_gateway(record.gateway_id)
            return record
        self.repository.update(record.kb_key, status=STATUS_GATEWAY)
        gateway_id = self._find_or_create(
            lambda: self._find_gateway(name),
            lambda: self._create_gateway(record, name),
        )
        gateway = self.control.get_gateway(gatewayIdentifier=gateway_id)
        record = self.repository.update(
            record.kb_key,
            gateway_id=gateway_id,
            gateway_arn=gateway.get("gatewayArn", ""),
        )
        self._wait_gateway(gateway_id)
        return record

    def _create_gateway(self, record: KnowledgeBaseRecord, name: str) -> str:
        response = self.control.create_gateway(
            clientToken=client_token_for(record.kb_key, "gateway"),
            name=name,
            description=self._description(record),
            roleArn=self.gateway_role_arn,
            protocolType="MCP",
            # SigV4. A harness signs outbound gateway calls with its execution
            # role, so CUSTOM_JWT would 401 at the first tool call.
            authorizerType="AWS_IAM",
        )
        return response["gatewayId"]

    def _find_gateway(self, name: str) -> Optional[str]:
        for item in self._paginate(self.control.list_gateways, "items"):
            if item.get("name") == name:
                return item.get("gatewayId")
        return None

    def _wait_gateway(self, gateway_id: str) -> Dict[str, Any]:
        return self._wait_for(
            f"gateway {gateway_id}",
            lambda: self.control.get_gateway(gatewayIdentifier=gateway_id),
            _GATEWAY_READY,
            _GATEWAY_FAILED,
        )

    def _ensure_target(self, record: KnowledgeBaseRecord) -> KnowledgeBaseRecord:
        name = gateway_name_for(record.kb_key)
        if record.target_id:
            self._wait_target(record.gateway_id, record.target_id)
            return record
        self.repository.update(record.kb_key, status=STATUS_TARGET)
        target_id = self._find_or_create(
            lambda: self._find_target(record.gateway_id, name),
            lambda: self._create_target(record, name),
        )
        record = self.repository.update(record.kb_key, target_id=target_id)
        self._wait_target(record.gateway_id, target_id)
        return record

    def _create_target(self, record: KnowledgeBaseRecord, name: str) -> str:
        configuration = {
            "mcp": {
                "connector": {
                    "source": {"connectorId": CONNECTOR_ID},
                    # parameterValues pins the tool to this knowledge base. That
                    # pinning — not IAM — is what keeps one user's gateway from
                    # reading another's knowledge base, since the shared gateway
                    # role can read every knowledge base in the account.
                    #
                    # No parameterOverrides: the query parameter is agent-visible
                    # by default, and ConnectorParameterOverride.path is a JSON
                    # Pointer, so the JSONPath forms would silently not match.
                    "configurations": [
                        {
                            "name": tool,
                            "parameterValues": {"knowledgeBaseId": record.kb_id},
                        }
                        for tool in CONNECTOR_TOOLS
                    ],
                }
            }
        }
        response = self._create_with_iam_retry(
            lambda: self.control.create_gateway_target(
                gatewayIdentifier=record.gateway_id,
                clientToken=client_token_for(record.kb_key, "target"),
                name=name,
                description=self._description(record),
                targetConfiguration=configuration,
                credentialProviderConfigurations=[
                    {"credentialProviderType": "GATEWAY_IAM_ROLE"}
                ],
            )
        )
        return response["targetId"]

    def _find_target(self, gateway_id: str, name: str) -> Optional[str]:
        for item in self._paginate(
            self.control.list_gateway_targets,
            "items",
            gatewayIdentifier=gateway_id,
        ):
            if item.get("name") == name:
                return item.get("targetId")
        return None

    def _wait_target(self, gateway_id: str, target_id: str) -> Dict[str, Any]:
        return self._wait_for(
            f"gateway target {target_id}",
            lambda: self.control.get_gateway_target(
                gatewayIdentifier=gateway_id, targetId=target_id
            ),
            _TARGET_READY,
            _TARGET_FAILED,
        )

    # ---- teardown --------------------------------------------------------

    def teardown(self, record: KnowledgeBaseRecord) -> KnowledgeBaseRecord:
        """Delete the AWS resources in reverse order of creation.

        Each id is cleared as its resource goes, so a retry after a partial
        failure resumes rather than starting over. S3 objects and the DynamoDB
        item are the caller's job (see knowledge_service.delete).
        """
        self._require_configured()
        if record.target_id and record.gateway_id:
            self._delete(
                lambda: self.control.delete_gateway_target(
                    gatewayIdentifier=record.gateway_id, targetId=record.target_id
                )
            )
            record = self.repository.update(record.kb_key, target_id="")
        if record.gateway_id:
            self._delete(
                lambda: self._delete_gateway_once_detached(record.gateway_id)
            )
            record = self.repository.update(
                record.kb_key, gateway_id="", gateway_arn=""
            )
        if record.data_source_id and record.kb_id:
            self._stop_ingestion_jobs(record.kb_id, record.data_source_id)
            self._delete(
                lambda: self._delete_data_source_once_idle(
                    record.kb_id, record.data_source_id
                )
            )
            record = self.repository.update(record.kb_key, data_source_id="")
        if record.kb_id:
            # This can return ConflictException while the data source is still
            # deleting. It is left to propagate: the record survives as
            # DELETE_FAILED and the user can retry, which is cheaper than holding
            # a request open for a poll loop.
            self._delete(
                lambda: self.agent.delete_knowledge_base(
                    knowledgeBaseId=record.kb_id
                )
            )
            record = self.repository.update(record.kb_key, kb_id="")
        return record

    def _stop_ingestion_jobs(self, kb_id: str, data_source_id: str) -> None:
        """Stop any running sync so the data source can be deleted.

        Best effort throughout: a knowledge base must stay deletable even if the
        job list cannot be read, and a job that finishes on its own between the
        list and the stop is the outcome this wanted anyway.
        """
        try:
            response = self.agent.list_ingestion_jobs(
                knowledgeBaseId=kb_id,
                dataSourceId=data_source_id,
                filters=[
                    {
                        "attribute": "STATUS",
                        "operator": "EQ",
                        "values": sorted(_JOB_RUNNING),
                    }
                ],
            )
        except ClientError as exc:
            logger.warning("Could not list sync jobs for %s: %s", kb_id, exc)
            return

        for summary in response.get("ingestionJobSummaries") or []:
            job_id = summary.get("ingestionJobId")
            if not job_id:
                continue
            try:
                self.agent.stop_ingestion_job(
                    knowledgeBaseId=kb_id,
                    dataSourceId=data_source_id,
                    ingestionJobId=job_id,
                )
            except ClientError as exc:
                # Already finished, most likely. The delete retry below settles it
                # either way, so this is not worth failing over.
                logger.info("Could not stop sync job %s: %s", job_id, exc)

    def _delete_data_source_once_idle(
        self, kb_id: str, data_source_id: str
    ) -> None:
        """DeleteDataSource, waiting out a sync that is still stopping.

        The same shape as _delete_gateway_once_detached: the stop has already been
        requested, so the only thing left is the service catching up. Retrying here
        is what the user would otherwise do by hand — the first attempt fails, the
        record lands in DELETE_FAILED, and pressing delete again works.
        """
        last_error: Optional[ClientError] = None
        for attempt in range(_JOB_STOP_ATTEMPTS):
            try:
                self.agent.delete_data_source(
                    knowledgeBaseId=kb_id, dataSourceId=data_source_id
                )
                return
            except ClientError as exc:
                if _JOB_RUNNING_MARKER not in _error_message(exc):
                    raise
                last_error = exc
                logger.info(
                    "Waiting for sync on %s to stop (%d/%d)",
                    data_source_id,
                    attempt + 1,
                    _JOB_STOP_ATTEMPTS,
                )
                time.sleep(_JOB_STOP_INTERVAL_SECONDS)
        raise RuntimeError(
            f"Data source {data_source_id} still reported a running sync after "
            f"{int(_JOB_STOP_ATTEMPTS * _JOB_STOP_INTERVAL_SECONDS)}s: {last_error}"
        )

    def _delete_gateway_once_detached(self, gateway_id: str) -> None:
        """DeleteGateway, waiting out the target that is still detaching.

        The target delete above has already returned, so the only thing standing
        between here and success is the gateway catching up. Retrying is what the
        user would otherwise have to do by hand: the first attempt fails, the
        record lands in DELETE_FAILED, and pressing delete again works.
        """
        last_error: Optional[ClientError] = None
        for attempt in range(_TARGET_DETACH_ATTEMPTS):
            try:
                self.control.delete_gateway(gatewayIdentifier=gateway_id)
                return
            except ClientError as exc:
                if _TARGET_ATTACHED_MARKER not in _error_message(exc):
                    raise
                last_error = exc
                logger.info(
                    "Waiting for gateway %s targets to detach (%d/%d)",
                    gateway_id,
                    attempt + 1,
                    _TARGET_DETACH_ATTEMPTS,
                )
                time.sleep(_TARGET_DETACH_INTERVAL_SECONDS)
        raise RuntimeError(
            f"Gateway {gateway_id} still reported targets after "
            f"{int(_TARGET_DETACH_ATTEMPTS * _TARGET_DETACH_INTERVAL_SECONDS)}s: "
            f"{last_error}"
        )

    @staticmethod
    def _delete(call: Callable[[], Any]) -> None:
        """Delete, treating "already gone" as success so retries can finish."""
        try:
            call()
        except ClientError as exc:
            if _error_code(exc) != "ResourceNotFoundException":
                raise

    # ---- shared helpers --------------------------------------------------

    @staticmethod
    def _description(record: KnowledgeBaseRecord) -> str:
        text = (record.description or record.name or record.kb_key).strip()
        return (text or record.kb_key)[:_DESCRIPTION_MAX]

    @staticmethod
    def _find_or_create(
        find: Callable[[], Optional[str]], create: Callable[[], str]
    ) -> str:
        """Look the resource up by name before creating it.

        Covers two cases at once: a resource created by a driver that died before
        writing the id down, and a concurrent driver that created it a moment ago
        (which surfaces as ConflictException).
        """
        existing = find()
        if existing:
            return existing
        try:
            return create()
        except ClientError as exc:
            if _error_code(exc) != "ConflictException":
                raise
            found = find()
            if not found:
                raise
            return found

    @staticmethod
    def _create_with_iam_retry(call: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
        last_error: Optional[ClientError] = None
        for attempt in range(_IAM_PROPAGATION_ATTEMPTS):
            try:
                return call()
            except ClientError as exc:
                if _IAM_PROPAGATION_MARKER not in _error_message(exc):
                    raise
                last_error = exc
                logger.info(
                    "Waiting for gateway role propagation (%d/%d)",
                    attempt + 1,
                    _IAM_PROPAGATION_ATTEMPTS,
                )
                time.sleep(_IAM_PROPAGATION_INTERVAL_SECONDS)
        raise RuntimeError(
            f"Gateway role permission never propagated: {last_error}"
        )

    def _wait_for(
        self,
        label: str,
        describe: Callable[[], Dict[str, Any]],
        ready: str,
        failed: set,
    ) -> Dict[str, Any]:
        for _ in range(_POLL_ATTEMPTS):
            payload = describe()
            status = (payload.get("status") or "").upper()
            if status == ready:
                return payload
            if status in failed:
                reasons = payload.get("failureReasons") or payload.get(
                    "statusReasons"
                ) or []
                raise RuntimeError(f"{label} entered {status}: {'; '.join(reasons)}")
            time.sleep(_POLL_INTERVAL_SECONDS)
        raise RuntimeError(
            f"{label} did not become {ready} within "
            f"{int(_POLL_ATTEMPTS * _POLL_INTERVAL_SECONDS)}s"
        )

    @staticmethod
    def _paginate(
        operation: Callable[..., Dict[str, Any]], key: str, **params: Any
    ) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        request = {"maxResults": _LIST_PAGE_SIZE, **params}
        token: Optional[str] = None
        while True:
            if token:
                request["nextToken"] = token
            response = operation(**request)
            items.extend(response.get(key, []))
            token = response.get("nextToken")
            if not token:
                return items
