"""
Knowledge bases as the API sees them: ownership, documents and deletion.

Provisioning lives next door in knowledge_provisioner; this module is what
decides *who* may do what, and it does so in one place because the rules are
easy to get subtly wrong:

- Reading needs ownership or `shared`. A knowledge base someone else owns
  privately raises KnowledgeNotFound, never a permission error — a 403 confirms
  it exists, which is a leak of exactly the thing being isolated.
- Writing (upload, document delete, knowledge-base delete) needs ownership or
  admin. Being able to *read* a shared knowledge base does not make it yours.
- `shared` is admin-only, on create as well as later.

Document state is read from AWS every time rather than mirrored into DynamoDB.
Ingestion is asynchronous and per document, so a mirror would be a second source
of truth that drifts the first time an ingest fails.
"""
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

from core.auth import AuthUser
from core.config import AWS_REGION, KNOWLEDGE_BUCKET, KNOWLEDGE_SOURCE_BUCKETS, KNOWLEDGE_PLATFORM_SOURCE_BUCKET
from models.knowledge import (
    PROVISIONING_STATUSES,
    SOURCE_S3,
    SOURCE_TYPES,
    SOURCE_UPLOAD,
    STATUS_CREATING,
    STATUS_DELETE_FAILED,
    STATUS_DELETING,
    STATUS_READY,
    SUPPORTED_MIME_TYPES,
    CreateKnowledgeBaseRequest,
    KnowledgeBaseDetail,
    KnowledgeBaseRecord,
    KnowledgeDocument,
    S3SourceConfig,
    SyncJob,
    client_token_for,
    doc_id_for,
    document_name_for,
    is_managed_source,
    kb_key_for,
    managed_prefix_for,
    now_iso,
    s3_key_for,
    source_object_name_for,
)
from repositories.knowledge_repository import KnowledgeRepository
from services.knowledge_provisioner import (
    KnowledgeNotConfigured,
    KnowledgeProvisioner,
)
from services.registry_service import fan_out

logger = logging.getLogger(__name__)

__all__ = [
    "KnowledgeForbidden",
    "KnowledgeNotConfigured",
    "KnowledgeNotFound",
    "KnowledgeService",
    "REVIVE_AFTER_SECONDS",
]

# How long a provisioning record may go without progress before the next listing
# restarts its driver. Long enough that a step in flight is not restarted for no
# reason (the slowest single step, CreateKnowledgeBase, updates the record at
# both ends), short enough that a user watching the page sees it resume.
REVIVE_AFTER_SECONDS = 120

# Uploads are held in memory to hash and forward, so the cap is a memory bound as
# much as a policy one.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

_S3_DELETE_BATCH = 1000
_FAILURE_REASON_MAX = 900

# What a deleted document settles on. ListKnowledgeBaseDocuments keeps returning
# the identifier after the vectors and the S3 object are gone — a tombstone that
# never expires (one observed unchanged a day later) — so the listing filters it
# out. DELETE_IN_PROGRESS is deliberately kept: a delete still in flight is
# something the user asked for and should watch finish.
_STATUS_NOT_FOUND = "NOT_FOUND"


class KnowledgeNotFound(Exception):
    """Raised when a knowledge base does not exist — or must appear not to."""


class KnowledgeForbidden(Exception):
    """Raised when the caller may see a knowledge base but not change it."""


def _document_id(detail: Dict[str, Any]) -> Optional[str]:
    """The identifier AWS returns, whose location depends on the connector.

    A CUSTOM data source reports `custom.id`; an S3 one reports `s3.uri`. Both are
    the value the delete API expects back, so they are interchangeable as an id
    even though only one of them is a name.
    """
    identifier = detail.get("identifier") or {}
    for holder, field in (("custom", "id"), ("s3", "uri")):
        value = (identifier.get(holder) or {}).get(field)
        if isinstance(value, str) and value:
            return value
    return None


def _iso(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.isoformat()
    return value if isinstance(value, str) else None


def _error_code(exc: ClientError) -> str:
    return (exc.response or {}).get("Error", {}).get("Code", "")


def _sync_job(job: Dict[str, Any]) -> SyncJob:
    stats = job.get("statistics") or {}
    return SyncJob(
        job_id=job.get("ingestionJobId") or "",
        status=job.get("status") or "",
        started_at=_iso(job.get("startedAt")),
        updated_at=_iso(job.get("updatedAt")),
        documents_scanned=stats.get("numberOfDocumentsScanned") or 0,
        documents_indexed=stats.get("numberOfNewDocumentsIndexed") or 0,
        documents_modified=stats.get("numberOfModifiedDocumentsIndexed") or 0,
        documents_deleted=stats.get("numberOfDocumentsDeleted") or 0,
        documents_failed=stats.get("numberOfDocumentsFailed") or 0,
        documents_skipped=stats.get("numberOfDocumentsSkipped") or 0,
        failure_reasons=list(job.get("failureReasons") or []),
    )


def _gateway_arns_of(harness: Any) -> List[str]:
    """The gateway ARNs a harness has attached as tools."""
    arns = []
    for tool in getattr(harness, "tools", None) or []:
        if not isinstance(tool, dict):
            continue
        config = tool.get("agentCoreGateway") or tool.get("config", {}).get(
            "agentCoreGateway"
        )
        if isinstance(config, dict) and config.get("gatewayArn"):
            arns.append(config["gatewayArn"])
    return arns


class KnowledgeService:
    def __init__(
        self,
        repository: Optional[KnowledgeRepository] = None,
        provisioner: Optional[KnowledgeProvisioner] = None,
        bucket: Optional[str] = None,
        region: Optional[str] = None,
        s3: Optional[Any] = None,
        harness: Optional[Any] = None,
        source_buckets: Optional[List[str]] = None,
        platform_source_bucket: Optional[str] = None,
    ):
        self.repository = repository
        self.provisioner = provisioner
        self.bucket = KNOWLEDGE_BUCKET if bucket is None else bucket
        self.region = region or AWS_REGION
        # Which buckets an S3-backed knowledge base may read. Not part of
        # `enabled`: an empty list disables one source type, not the feature.
        self.source_buckets = list(
            KNOWLEDGE_SOURCE_BUCKETS if source_buckets is None else source_buckets
        )
        # The bucket managed S3 sources live in. Not part of `enabled` for the
        # same reason source_buckets is not: its absence disables one source
        # type, not the feature.
        self.platform_source_bucket = (
            KNOWLEDGE_PLATFORM_SOURCE_BUCKET
            if platform_source_bucket is None
            else platform_source_bucket
        )
        self._s3 = s3
        # Only used to warn before deleting a knowledge base a harness attached.
        # Injected rather than constructed so a harness failure cannot break this
        # service's import, and so tests need no AgentCore client.
        self._harness = harness

    @property
    def s3(self) -> Any:
        if self._s3 is None:
            self._s3 = boto3.client("s3", region_name=self.region)
        return self._s3

    @property
    def harness(self) -> Any:
        if self._harness is None:
            from services.harness_service import HarnessService

            self._harness = HarnessService()
        return self._harness

    @property
    def enabled(self) -> bool:
        return bool(
            self.repository
            and self.bucket
            and self.provisioner
            and self.provisioner.enabled
        )

    def _require_configured(self) -> None:
        if not self.enabled:
            raise KnowledgeNotConfigured(
                "Knowledge bases are not configured on the server. Set "
                "KNOWLEDGE_BUCKET, KNOWLEDGE_TABLE, KB_SERVICE_ROLE_ARN and "
                "KB_GATEWAY_ROLE_ARN from `terraform output`."
            )

    def source_bucket_info(self, user: AuthUser) -> Dict[str, Any]:
        """What the create dialog needs to offer S3 sources.

        The external-bucket list is admin-only twice over: only an admin can
        use it, and organisation bucket names are not something every user
        needs shown. platform_available is for everyone — it is the only way
        the dialog can tell "S3 works here" apart from a 400 after the click.
        """
        return {
            "buckets": list(self.source_buckets) if user.is_admin else [],
            "platform_available": bool(self.platform_source_bucket),
        }

    # --- access rules -----------------------------------------------------

    def _readable(self, user: AuthUser, kb_key: str) -> KnowledgeBaseRecord:
        record = self.repository.get(kb_key)
        if record is None:
            raise KnowledgeNotFound(f"Knowledge base {kb_key} not found")
        if record.owner_id == user.sub or record.shared or user.is_admin:
            return record
        # Deliberately the same error as "does not exist": a distinguishable
        # response would let anyone enumerate other people's knowledge bases.
        raise KnowledgeNotFound(f"Knowledge base {kb_key} not found")

    def _writable(self, user: AuthUser, kb_key: str) -> KnowledgeBaseRecord:
        record = self._readable(user, kb_key)
        if record.owner_id == user.sub or user.is_admin:
            return record
        raise KnowledgeForbidden(
            f"Knowledge base {kb_key} belongs to {record.owner_name or 'another user'} "
            "and is shared read-only."
        )

    # --- listing ----------------------------------------------------------

    def list_visible(
        self, user: AuthUser, status: Optional[str] = None
    ) -> List[KnowledgeBaseRecord]:
        self._require_configured()
        records = self.repository.visible_to(user.sub)
        if status:
            records = [r for r in records if r.status == status]
        return records

    def stale_keys(self, records: List[KnowledgeBaseRecord]) -> List[str]:
        """Provisioning records whose driver appears to have died.

        The caller (the GET route) restarts these as background tasks. Polling the
        listing is therefore the recovery mechanism — no worker, no scheduler.
        """
        cutoff = datetime.now(timezone.utc).timestamp() - REVIVE_AFTER_SECONDS
        stale = []
        for record in records:
            if record.status not in PROVISIONING_STATUSES:
                continue
            try:
                touched = datetime.fromisoformat(record.updated_at).timestamp()
            except (TypeError, ValueError):
                # An unreadable timestamp must not strand a knowledge base
                # forever; treat it as overdue.
                touched = 0.0
            if touched < cutoff:
                stale.append(record.kb_key)
        return stale

    # --- creation ---------------------------------------------------------

    def create(
        self, user: AuthUser, req: CreateKnowledgeBaseRequest
    ) -> KnowledgeBaseRecord:
        """Record the knowledge base and return; the caller starts provisioning.

        The item is written before any AWS call so that a container which dies
        mid-provisioning leaves something that can find and clean up whatever it
        managed to create.
        """
        self._require_configured()
        name = (req.name or "").strip()
        if not name:
            raise ValueError("Knowledge base name is required.")
        if req.shared and not user.is_admin:
            raise KnowledgeForbidden(
                "Only an administrator can publish a shared knowledge base."
            )
        source_type = (req.source_type or SOURCE_UPLOAD).strip().upper()
        kb_key = kb_key_for(name)
        source_config = self._validated_source_config(
            user, source_type, req.source_config, kb_key
        )
        stamp = now_iso()
        return self.repository.put(
            KnowledgeBaseRecord(
                kb_key=kb_key,
                owner_id=user.sub,
                owner_name=user.username,
                shared=bool(req.shared),
                name=name,
                description=(req.description or "").strip() or None,
                source_type=source_type,
                source_config=source_config,
                status=STATUS_CREATING,
                created_at=stamp,
                updated_at=stamp,
            )
        )

    def _validated_source_config(
        self,
        user: AuthUser,
        source_type: str,
        source_config: Dict[str, Any],
        kb_key: str,
    ) -> Dict[str, Any]:
        """Check the connector settings now, while a 400 is still possible.

        Provisioning runs detached and takes minutes, so anything wrong here
        would otherwise surface as a CREATE_FAILED record long after the click.

        Two S3 shapes come out of this. A request that names a bucket or prefix
        is the external path — admin only, allowlist-checked, read-only from
        then on. An empty config is the managed path: the platform's own source
        bucket and a per-user prefix, neither of which is user input, which is
        what makes the prefix an isolation boundary.
        """
        if source_type not in SOURCE_TYPES:
            raise ValueError(
                f"Unknown source type {source_type}. Supported: "
                + ", ".join(sorted(SOURCE_TYPES))
            )
        if source_type == SOURCE_UPLOAD:
            # Nothing to configure, and quietly keeping a stray config would
            # reappear later as connector parameters nobody asked for.
            return {}

        config = S3SourceConfig(**(source_config or {}))
        bucket = config.bucket_name.strip()
        prefix = (config.prefix or "").strip().lstrip("/")
        if bucket or prefix:
            if not user.is_admin:
                raise ValueError(
                    "버킷과 접두사는 서버가 정합니다. 비워두고 생성하세요."
                )
            if bucket not in self.source_buckets:
                raise ValueError(
                    f"Bucket {bucket} is not available as a knowledge base source. "
                    + (
                        "Available: " + ", ".join(sorted(self.source_buckets))
                        if self.source_buckets
                        else "An administrator has not registered any source buckets."
                    )
                )
            return {"bucket_name": bucket, "prefix": prefix or None}

        if not self.platform_source_bucket:
            raise ValueError(
                "이 환경에는 플랫폼 소스 버킷이 없어 S3 타입을 만들 수 없습니다."
            )
        return {
            "bucket_name": self.platform_source_bucket,
            "prefix": managed_prefix_for(user.sub, kb_key),
            "managed": True,
        }

    def advance(self, kb_key: str) -> None:
        """Background-task entry point for provisioning.

        Swallows everything: it runs detached from any request, and the
        provisioner has already recorded the failure on the record.
        """
        try:
            self.provisioner.advance(kb_key)
        except Exception:
            logger.exception("Background provisioning of %s failed", kb_key)

    # --- source type rules ------------------------------------------------

    def _require_upload_source(
        self, record: KnowledgeBaseRecord, action: str
    ) -> None:
        if record.source_type != SOURCE_UPLOAD:
            raise ValueError(
                f"{record.name} reads from {self._source_label(record)}, so you "
                f"cannot {action} it. Change the source and synchronise instead."
            )

    def _require_sync_source(self, record: KnowledgeBaseRecord) -> None:
        if record.source_type == SOURCE_UPLOAD:
            raise ValueError(
                f"{record.name} is an upload knowledge base. Uploaded files are "
                "indexed on arrival, so there is nothing to synchronise."
            )

    @staticmethod
    def _source_label(record: KnowledgeBaseRecord) -> str:
        if record.source_type == SOURCE_S3:
            bucket = record.source_config.get("bucket_name") or "an S3 bucket"
            prefix = record.source_config.get("prefix")
            return f"s3://{bucket}/{prefix}" if prefix else f"s3://{bucket}"
        return "uploaded files"

    # --- detail and documents ---------------------------------------------

    def detail(self, user: AuthUser, kb_key: str) -> KnowledgeBaseDetail:
        self._require_configured()
        record = self._readable(user, kb_key)
        return KnowledgeBaseDetail(
            knowledge_base=record,
            documents=self._documents(record),
            # Folded into the detail rather than left to a second round trip: the
            # panel needs both to render once.
            sync=self._latest_sync(record),
        )

    def _documents(self, record: KnowledgeBaseRecord) -> List[KnowledgeDocument]:
        if not (record.kb_id and record.data_source_id):
            return []
        documents: List[KnowledgeDocument] = []
        params: Dict[str, Any] = {
            "knowledgeBaseId": record.kb_id,
            "dataSourceId": record.data_source_id,
            "maxResults": 100,
        }
        try:
            while True:
                response = self.provisioner.agent.list_knowledge_base_documents(
                    **params
                )
                for detail in response.get("documentDetails", []):
                    doc_id = _document_id(detail)
                    if not doc_id:
                        continue
                    if (detail.get("status") or "").upper() == _STATUS_NOT_FOUND:
                        continue
                    documents.append(
                        KnowledgeDocument(
                            doc_id=doc_id,
                            filename=document_name_for(record.source_type, doc_id),
                            status=detail.get("status"),
                            status_reason=detail.get("statusReason"),
                            updated_at=_iso(detail.get("updatedAt")),
                        )
                    )
                token = response.get("nextToken")
                if not token:
                    break
                params["nextToken"] = token
        except ClientError as exc:
            # The knowledge base itself is still worth showing; an empty document
            # list with the panel visible beats a failed page.
            logger.warning(
                "Could not list documents for %s: %s", record.kb_key, exc
            )
            return []
        return documents

    def add_document(
        self,
        user: AuthUser,
        kb_key: str,
        filename: str,
        content_type: str,
        body: bytes,
    ) -> KnowledgeDocument:
        self._require_configured()
        record = self._writable(user, kb_key)
        if not is_managed_source(record):
            self._require_upload_source(record, "upload files to")
        if record.status != STATUS_READY:
            raise ValueError(
                f"Knowledge base {record.name} is {record.status}; wait until it "
                "is READY before uploading."
            )
        normalized = (content_type or "").split(";")[0].strip().lower()
        if normalized not in SUPPORTED_MIME_TYPES:
            raise ValueError(
                f"Unsupported file type {normalized or 'unknown'}. Supported: "
                + ", ".join(sorted(SUPPORTED_MIME_TYPES.values()))
            )
        if not body:
            raise ValueError("The uploaded file is empty.")
        if len(body) > MAX_UPLOAD_BYTES:
            raise ValueError(
                f"File is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
            )

        if is_managed_source(record):
            return self._add_source_object(record, filename, normalized, body)

        doc_id = doc_id_for(filename)
        key = s3_key_for(kb_key, doc_id)
        self.s3.put_object(
            Bucket=self.bucket, Key=key, Body=body, ContentType=normalized
        )
        response = self.provisioner.agent.ingest_knowledge_base_documents(
            knowledgeBaseId=record.kb_id,
            dataSourceId=record.data_source_id,
            clientToken=client_token_for(kb_key, f"ingest-{doc_id}"),
            documents=[
                {
                    # The same filename as the identifier carries, but here it
                    # reaches retrieval results as a citation attribute. That is a
                    # search-quality concern, not a display one — the display name
                    # comes from doc_id, since document listings return no metadata.
                    "metadata": {
                        "type": "IN_LINE_ATTRIBUTE",
                        "inlineAttributes": [
                            {
                                "key": "filename",
                                "value": {
                                    "type": "STRING",
                                    "stringValue": filename,
                                },
                            }
                        ],
                    },
                    "content": {
                        "dataSourceType": "CUSTOM",
                        "custom": {
                            "customDocumentIdentifier": {"id": doc_id},
                            # Via S3, not inline: a PDF or docx exceeds the inline
                            # limit, and keeping the original allows re-ingestion
                            # and download later.
                            "sourceType": "S3_LOCATION",
                            "s3Location": {"uri": f"s3://{self.bucket}/{key}"},
                        },
                    },
                }
            ],
        )
        details = response.get("documentDetails") or [{}]
        return KnowledgeDocument(
            doc_id=doc_id,
            filename=document_name_for(record.source_type, doc_id),
            status=details[0].get("status"),
            status_reason=details[0].get("statusReason"),
            updated_at=_iso(details[0].get("updatedAt")),
        )

    def _add_source_object(
        self, record: KnowledgeBaseRecord, filename: str, content_type: str,
        body: bytes,
    ) -> KnowledgeDocument:
        """Put the file into the knowledge base's own folder and sync.

        No IngestKnowledgeBaseDocuments here — the S3 connector is pull-only,
        so the sync is what indexes the object. status=None is honest: until a
        sync scans it the document does not exist as far as Bedrock knows.
        """
        if not (record.kb_id and record.data_source_id):
            raise KnowledgeNotFound(
                f"Knowledge base {record.kb_key} has no data source"
            )
        bucket = record.source_config["bucket_name"]
        prefix = record.source_config.get("prefix") or ""
        key = f"{prefix}{source_object_name_for(filename)}"
        self.s3.put_object(
            Bucket=bucket, Key=key, Body=body, ContentType=content_type
        )
        self._start_ingestion(record)
        return KnowledgeDocument(
            doc_id=f"s3://{bucket}/{key}",
            filename=document_name_for(record.source_type, f"s3://{bucket}/{key}"),
            status=None,
        )

    def _delete_source_object(self, record: KnowledgeBaseRecord, doc_id: str) -> None:
        """Delete an uploaded original and let a sync drop it from the index.

        The namespace check is load-bearing: an S3 document id is a URI the
        client sends back, so without it a crafted id could delete objects
        outside this knowledge base's folder — in the feature whose whole point
        is that folders are isolation boundaries.
        """
        if not (record.kb_id and record.data_source_id):
            raise KnowledgeNotFound(f"Knowledge base {record.kb_key} has no data source")
        bucket = record.source_config["bucket_name"]
        prefix = record.source_config.get("prefix") or ""
        namespace = f"s3://{bucket}/{prefix}"
        if not doc_id.startswith(namespace) or doc_id == namespace:
            raise ValueError(f"{doc_id}는 이 Knowledge Base의 폴더에 없습니다.")
        self.s3.delete_object(Bucket=bucket, Key=doc_id[len(f"s3://{bucket}/"):])
        self._start_ingestion(record)

    def delete_document(self, user: AuthUser, kb_key: str, doc_id: str) -> None:
        self._require_configured()
        record = self._writable(user, kb_key)
        if is_managed_source(record):
            self._delete_source_object(record, doc_id)
            return
        # Deleting one document from a pull connector removes it from the index
        # only; the next sync finds the untouched original and indexes it again.
        # An API that reports success and then undoes itself is worse than one
        # that refuses, so the caller is told to delete the object instead.
        self._require_upload_source(record, "delete individual documents from")
        if not (record.kb_id and record.data_source_id):
            raise KnowledgeNotFound(f"Knowledge base {kb_key} has no data source")
        self.provisioner.agent.delete_knowledge_base_documents(
            knowledgeBaseId=record.kb_id,
            dataSourceId=record.data_source_id,
            clientToken=client_token_for(kb_key, f"delete-{doc_id}"),
            documentIdentifiers=[
                {"dataSourceType": "CUSTOM", "custom": {"id": doc_id}}
            ],
        )
        self.s3.delete_object(Bucket=self.bucket, Key=s3_key_for(kb_key, doc_id))

    # --- sync -------------------------------------------------------------

    def _start_ingestion(self, record: KnowledgeBaseRecord) -> SyncJob:
        """Start an ingestion job, or return the one already running.

        Returning the in-flight job is not an error case dressed up: the caller
        wants the index current, and a running sync is that request already
        being served.
        """
        running = self._latest_sync(record)
        if running and running.in_progress:
            return running
        try:
            response = self.provisioner.agent.start_ingestion_job(
                knowledgeBaseId=record.kb_id,
                dataSourceId=record.data_source_id,
                clientToken=client_token_for(record.kb_key, f"sync-{now_iso()}"),
            )
        except ClientError as exc:
            # A job started between the check above and here. The caller's
            # intent is satisfied either way, so report that job, not the race.
            if _error_code(exc) == "ConflictException":
                existing = self._latest_sync(record)
                if existing:
                    return existing
            raise
        return _sync_job(response.get("ingestionJob") or {})

    def _latest_sync(self, record: KnowledgeBaseRecord) -> Optional[SyncJob]:
        if record.source_type == SOURCE_UPLOAD:
            return None
        if not (record.kb_id and record.data_source_id):
            return None
        try:
            response = self.provisioner.agent.list_ingestion_jobs(
                knowledgeBaseId=record.kb_id,
                dataSourceId=record.data_source_id,
                sortBy={"attribute": "STARTED_AT", "order": "DESCENDING"},
                maxResults=1,
            )
        except ClientError as exc:
            # Same reasoning as the document listing: the knowledge base is still
            # worth showing without its sync history.
            logger.warning(
                "Could not list sync jobs for %s: %s", record.kb_key, exc
            )
            return None
        jobs = response.get("ingestionJobSummaries") or []
        return _sync_job(jobs[0]) if jobs else None

    def start_sync(self, user: AuthUser, kb_key: str) -> SyncJob:
        """Start an ingestion job, or return the one already running.

        Returning the in-flight job is not an error case dressed up: the caller
        asked for the index to be current, and a running sync is that request
        already being served. A 409 would only invite them to retry until it
        happened to be idle.
        """
        self._require_configured()
        record = self._writable(user, kb_key)
        self._require_sync_source(record)
        if record.status != STATUS_READY:
            raise ValueError(
                f"Knowledge base {record.name} is {record.status}; wait until it "
                "is READY before synchronising."
            )
        if not (record.kb_id and record.data_source_id):
            raise KnowledgeNotFound(f"Knowledge base {kb_key} has no data source")
        return self._start_ingestion(record)

    def sync_status(self, user: AuthUser, kb_key: str) -> Optional[SyncJob]:
        self._require_configured()
        record = self._readable(user, kb_key)
        self._require_sync_source(record)
        return self._latest_sync(record)

    # --- deletion ---------------------------------------------------------

    def delete(self, user: AuthUser, kb_key: str, force: bool = False) -> None:
        self._require_configured()
        record = self._writable(user, kb_key)
        if not force and record.gateway_arn:
            attached = self._harnesses_using(record.gateway_arn)
            if attached:
                raise ValueError(
                    f"{', '.join(attached)} still use this knowledge base. Their "
                    "retrieval tools will break. Re-run with force=true to delete "
                    "it anyway."
                )
        self.repository.update(kb_key, status=STATUS_DELETING, failure_reason="")
        try:
            record = self.provisioner.teardown(record)
        except Exception as exc:
            # The record stays behind on purpose: it holds the ids of whatever is
            # left in AWS, and deleting it would leave those resources orphaned
            # with nothing pointing at them.
            self.repository.update(
                kb_key,
                status=STATUS_DELETE_FAILED,
                failure_reason=str(exc)[:_FAILURE_REASON_MAX],
            )
            raise
        if record.source_type == SOURCE_UPLOAD:
            # Only uploads put originals in this bucket, and only originals this
            # platform created are ours to remove. A knowledge base that merely
            # read someone's bucket must leave it exactly as it found it — the
            # prefix here would be empty anyway, but the guard is what states that
            # deleting a knowledge base never deletes attached source data.
            self._delete_prefix(f"knowledge/{kb_key}/")
        elif is_managed_source(record) and record.source_config.get("prefix"):
            # A managed source's originals were put there by this platform, so
            # they leave with it. External sources stay exactly as found — the
            # platform only ever borrowed them. The prefix guard keeps an
            # (impossible today) empty prefix from becoming a bucket wipe.
            self._delete_prefix(
                record.source_config["prefix"],
                bucket=record.source_config["bucket_name"],
            )
        self.repository.delete(kb_key)

    def _harnesses_using(self, gateway_arn: str) -> List[str]:
        """Names of harnesses that attached this knowledge base's gateway.

        ListHarnesses returns identity and status only — no `tools` — so the
        attachment has to come from GetHarness per harness. Reading it off the
        summaries instead made the guard silently unanimous: every delete looked
        unused, and the confirmation prompt never appeared.
        """
        try:
            harnesses = self.harness.list_harnesses()
        except Exception as exc:
            # A warning is a courtesy, not a gate. Failing the delete because the
            # check failed would leave no way to remove a knowledge base.
            logger.warning("Could not check harness attachments: %s", exc)
            return []

        def attaches(summary: Any) -> Optional[str]:
            try:
                detail = self.harness.get_harness(summary.harness_id)
            except Exception as exc:
                logger.warning(
                    "Could not read harness %s: %s", summary.harness_id, exc
                )
                return None
            return (
                detail.harness_name or summary.harness_name
                if gateway_arn in _gateway_arns_of(detail)
                else None
            )

        return [name for name in fan_out(attaches, harnesses) if name]

    def _delete_prefix(self, prefix: str, bucket: Optional[str] = None) -> None:
        params: Dict[str, Any] = {"Bucket": bucket or self.bucket, "Prefix": prefix}
        while True:
            response = self.s3.list_objects_v2(**params)
            keys = [{"Key": o["Key"]} for o in response.get("Contents", [])]
            for start in range(0, len(keys), _S3_DELETE_BATCH):
                self.s3.delete_objects(
                    Bucket=params["Bucket"],
                    Delete={"Objects": keys[start:start + _S3_DELETE_BATCH]},
                )
            token = response.get("NextContinuationToken")
            if not token:
                return
            params["ContinuationToken"] = token
