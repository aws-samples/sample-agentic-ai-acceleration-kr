"""
Models and naming rules for user-created Bedrock Managed Knowledge Bases.

The names here are load-bearing rather than decorative:

- `kb_key` is this platform's identifier and is written to DynamoDB *before*
  CreateKnowledgeBase runs, so a crash mid-provisioning still leaves a record
  pointing at whatever was made. It doubles as the knowledge base and data
  source name, which is what makes find-then-create possible on resume.
- The gateway name becomes the harness tool name, and the MCP tools the agent
  sees are `<target>___Retrieve` / `<target>___AgenticRetrieveStream`. A gateway
  called `kb-7f3a` tells the model nothing about when to use it.
- `doc_id` carries the filename because ListKnowledgeBaseDocuments returns only
  the identifier and a status — there is nowhere else for a display name to live.
"""
import hashlib
import os
import re
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from services.registry_service import _slug

# Provisioning progress. Each value names the step currently *in progress*, so
# the UI can say what is happening and the provisioner can tell where it is from
# the record alone.
STATUS_CREATING = "CREATING"
STATUS_DATA_SOURCE = "DATA_SOURCE"
STATUS_GATEWAY = "GATEWAY"
STATUS_TARGET = "TARGET"
STATUS_READY = "READY"
STATUS_CREATE_FAILED = "CREATE_FAILED"
STATUS_DELETING = "DELETING"
STATUS_DELETE_FAILED = "DELETE_FAILED"

PROVISIONING_STATUSES = {
    STATUS_CREATING,
    STATUS_DATA_SOURCE,
    STATUS_GATEWAY,
    STATUS_TARGET,
}

# Where a knowledge base's documents come from. Fixed at creation: AWS does not
# allow a data source to change connector type.
#
# UPLOAD is the CUSTOM connector — documents are *pushed* with
# IngestKnowledgeBaseDocuments as they are uploaded, so there is nothing to sync.
# S3 is a *pull* connector over a bucket the caller already owns, so it syncs and
# cannot be uploaded to. The two are mutually exclusive at every layer, which is
# why the routes reject the wrong verb rather than only hiding the button.
SOURCE_UPLOAD = "UPLOAD"
SOURCE_S3 = "S3"
SOURCE_TYPES = {SOURCE_UPLOAD, SOURCE_S3}

# `connectorParameters.type` for each of ours. Worth stating explicitly because
# the AWS docs get these names wrong: they list `WEB_CRAWLER` and `GOOGLE_DRIVE`,
# but CreateDataSource rejects both — the accepted set is S3, ONEDRIVE, ZENDESK,
# SALESFORCE, BOX, DROPBOX, SHAREPOINT, GOOGLEDRIVE, WEB, CUSTOM,
# CONFLUENCEONPREM, CONFLUENCE, SERVICENOW.
_CONNECTOR_TYPES = {SOURCE_UPLOAD: "CUSTOM", SOURCE_S3: "S3"}

# Statuses a sync job never moves off of. As with document statuses, this is a
# list of endings rather than of middles: AWS adds values ahead of the SDK enum,
# and a whitelist of in-progress values freezes the UI the first time it does.
SYNC_TERMINAL_STATUSES = {"COMPLETE", "FAILED", "STOPPED"}

# What Bedrock's managed parser accepts. Rejecting at upload time is clearer than
# letting ingestion fail asynchronously with an opaque status.
SUPPORTED_MIME_TYPES: Dict[str, str] = {
    "text/plain": "Text",
    "text/markdown": "Markdown",
    "text/html": "HTML",
    "text/csv": "CSV",
    "application/pdf": "PDF",
    "application/msword": "Word (.doc)",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "Word (.docx)",
}

GATEWAY_NAME_MAX = 48
GATEWAY_NAME_RE = re.compile(r"^([0-9a-zA-Z][-]?){1,48}$")

# Characters a document id may keep. The extension is preserved deliberately: it
# is part of the name a user recognises, and CustomDocumentIdentifier imposes no
# character restrictions of its own (1-2048 characters).
_DOC_CHAR_RE = re.compile(r"[^A-Za-z0-9._-]")
_DOC_SLUG_MAX = 96
_RANDOM_SUFFIX_BYTES = 4  # 8 hex characters

# AWS resource names are ASCII; `_slug` alone would keep Hangul and CJK.
_NON_ASCII_RE = re.compile(r"[^\x00-\x7F]")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _random_suffix() -> str:
    return secrets.token_hex(_RANDOM_SUFFIX_BYTES)


def _name_slug(name: str) -> str:
    """`_slug`, restricted to what AWS resource names actually accept.

    `_slug` keeps every character `str.isalnum()` accepts, which includes Hangul
    and CJK — fine for a skill name, fatal here: both `GatewayName` and
    bedrock-agent's `Name` are ASCII alphanumerics with separators, so
    `제품 문서` would produce `kb_제품-문서_...` and AWS would reject the create.
    Korean titles are the norm on this platform, so the ASCII part is dropped
    to `kb` and the readable title survives in the record's `name` field and in
    the gateway description the model reads.
    """
    result = _slug(_NON_ASCII_RE.sub(" ", name or "")).strip("-")
    return result if result and result != "skill" else "kb"


def kb_key_for(name: str) -> str:
    """This platform's knowledge base id: `kb_<slug>_<8 hex>`.

    Not the AWS knowledgeBaseId, because the item has to exist before the AWS
    resource does — otherwise a container that dies right after
    CreateKnowledgeBase leaves a knowledge base nothing can find or clean up.
    """
    return f"kb_{_name_slug(name)}_{_random_suffix()}"


def gateway_name_for(kb_key: str) -> str:
    """The gateway (and target) name for a knowledge base key.

    GatewayName allows only alphanumerics and single hyphens, up to 48
    characters, so the key's underscores are replaced and the *slug* is what gets
    shortened — truncating the tail would drop the random suffix and let two
    knowledge bases with long, similar titles collide on a gateway name.
    """
    parts = kb_key.split("_")
    slug = parts[1] if len(parts) > 2 else _name_slug(kb_key)
    suffix = parts[-1] if len(parts) > 2 else _random_suffix()
    head = slug[: GATEWAY_NAME_MAX - len(suffix) - 1].strip("-") or "kb"
    return f"{head}-{suffix}"


def doc_id_for(filename: str) -> str:
    """`<filename>_<8 hex>`, keeping the extension.

    KnowledgeBaseDocumentDetail returns no metadata, so the identifier is the
    only place a display name can survive a round trip through AWS. The random
    suffix keeps a re-upload of the same filename from replacing the first copy
    silently.
    """
    cleaned = _DOC_CHAR_RE.sub("-", (filename or "").strip())[:_DOC_SLUG_MAX].strip("-.")
    return f"{cleaned or 'document'}_{_random_suffix()}"


def display_name_for(doc_id: str) -> str:
    """The filename inside a document id, or the id itself if it has no suffix."""
    head, separator, tail = doc_id.rpartition("_")
    if separator and len(tail) == _RANDOM_SUFFIX_BYTES * 2:
        return head or doc_id
    return doc_id


def document_name_for(source_type: str, doc_id: str) -> str:
    """The name to show for a document, which depends on where it came from.

    An uploaded document's id is `<filename>_<8 hex>` because the CUSTOM connector
    returns no metadata and the identifier is the only place a filename can live.
    An S3 connector's identifier is the object's URI, which needs no decoding —
    running it through display_name_for would strip a trailing `_`-and-8-hex-like
    fragment out of a real key.
    """
    if source_type == SOURCE_S3:
        return doc_id.rstrip("/").rpartition("/")[2] or doc_id
    return display_name_for(doc_id)


def connector_parameters_for(
    source_type: str, source_config: Dict[str, Any], account_id: str = ""
) -> Dict[str, Any]:
    """`connectorParameters` for CreateDataSource.

    A pure function, and deliberately not a method on the provisioner: the field
    names here are the easiest thing in this feature to get wrong, and getting
    them wrong is otherwise only visible as a CREATE_FAILED three minutes later.
    The shape is unvalidated by botocore too — the API models `connectorParameters`
    as a free-form Document.
    """
    connector = _CONNECTOR_TYPES.get(source_type)
    if connector is None:
        raise ValueError(f"Unknown source type {source_type}")
    if source_type == SOURCE_UPLOAD:
        return {"type": connector, "version": "1", "aclEnabled": False}

    config = S3SourceConfig(**source_config)
    bucket = config.bucket_name.strip()
    if not bucket:
        raise ValueError("An S3 source requires a bucket name.")
    connection: Dict[str, Any] = {"bucketName": bucket}
    if account_id:
        # Same-account only, so the server fills this in; the allowlist is what
        # decides which buckets are reachable.
        connection["bucketOwnerAccountId"] = account_id
    parameters: Dict[str, Any] = {
        "type": connector,
        "version": "1",
        "connectionConfiguration": connection,
    }
    prefix = (config.prefix or "").strip()
    if prefix:
        parameters["filterConfiguration"] = {"inclusionPrefixes": [prefix]}
    return parameters


def s3_key_for(kb_key: str, doc_id: str) -> str:
    """Where the uploaded original lives. Derived, so it is never stored."""
    return f"knowledge/{kb_key}/{doc_id}"


def managed_prefix_for(owner_sub: str, kb_key: str) -> str:
    """The per-user folder a managed S3 knowledge base reads and writes.

    Neither component is user input — the sub comes from the token and the key
    from kb_key_for — which is what makes the prefix an isolation boundary
    rather than a guardrail.
    """
    return f"users/{owner_sub}/{kb_key}/"


def source_object_name_for(filename: str) -> str:
    """The object name for a file uploaded into a managed S3 source.

    Unlike doc_id_for there is no random suffix: the S3 connector's document
    identifier is the object URI itself, so re-uploading the same name is an
    overwrite the sync reports as a modification — which is what a user
    replacing a file means. _DOC_CHAR_RE also strips non-ASCII, which this
    platform's S3 boundary requires.
    """
    base, ext = os.path.splitext((filename or "").strip())
    cleaned_base = _DOC_CHAR_RE.sub("-", base)[:_DOC_SLUG_MAX].strip("-.")
    return (cleaned_base or "document") + ext


def is_managed_source(record: "KnowledgeBaseRecord") -> bool:
    """An S3 source whose bucket and prefix this platform assigned and owns.

    Stored on the record rather than derived from the bucket name so a config
    change cannot retroactively reclassify existing knowledge bases.
    """
    return record.source_type == SOURCE_S3 and bool(record.source_config.get("managed"))


def client_token_for(kb_key: str, step: str) -> str:
    """A per-step idempotency token for the AWS create calls.

    Stable for a given (key, step) so a retried step is deduplicated by AWS too,
    not just by this platform's find-then-create. A hash rather than the key
    itself because ClientToken must be 33-256 characters of alphanumerics and
    hyphens — `kb_key` is both too short and full of underscores.
    """
    return hashlib.sha256(f"{kb_key}:{step}".encode("utf-8")).hexdigest()


class KnowledgeBaseRecord(BaseModel):
    kb_key: str
    owner_id: str
    # The `username` claim, which is only as readable as the pool makes it: a pool
    # whose usernames are UUIDs (this one) stores the same value as owner_id, so the
    # UI shows a UUID for a shared knowledge base's owner. The access token carries
    # no `email`, so the server has nothing better to record — a readable owner needs
    # an alias or a pool attribute mapped into the token, not a change here.
    owner_name: str = ""
    # Admin-only. A shared knowledge base is readable and attachable by everyone.
    shared: bool = False
    name: str
    description: Optional[str] = None
    # Defaults to UPLOAD so records written before source types existed keep
    # working: DynamoDB has no attribute, and Pydantic fills in what they are.
    source_type: str = SOURCE_UPLOAD
    # Connector-specific settings, validated on the way in and opaque afterwards.
    # A dict rather than a typed field because each connector carries a different
    # shape and DynamoDB stores one map either way.
    source_config: Dict[str, Any] = {}
    status: str = STATUS_CREATING
    failure_reason: Optional[str] = None
    # Populated step by step; a missing id means that step has not finished.
    kb_id: Optional[str] = None
    data_source_id: Optional[str] = None
    gateway_id: Optional[str] = None
    gateway_arn: Optional[str] = None
    target_id: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""


class S3SourceConfig(BaseModel):
    """Settings for a knowledge base that pulls from a bucket.

    `bucket_name` is empty for a managed source — the server assigns the
    platform bucket. When named (admin, external source) it is checked against
    the server's allowlist separately: this validates shape, the service
    validates permission.
    """

    bucket_name: str = ""
    # Narrows the crawl to one part of the bucket. Assigned by the server for a
    # managed source; free-form for an admin's external source.
    prefix: Optional[str] = None
    # True when this platform assigned the bucket and prefix and owns the
    # objects under it. Server-set; never accepted from the request.
    managed: bool = False


class CreateKnowledgeBaseRequest(BaseModel):
    name: str
    description: Optional[str] = None
    shared: bool = False
    # Omitted by every caller written before source types existed, which is
    # exactly what they meant.
    source_type: str = SOURCE_UPLOAD
    source_config: Dict[str, Any] = {}


class KnowledgeDocument(BaseModel):
    doc_id: str
    filename: str
    status: Optional[str] = None
    status_reason: Optional[str] = None
    updated_at: Optional[str] = None


class SyncJob(BaseModel):
    """The latest ingestion job for a pull connector.

    Read from AWS on every request rather than mirrored: ListIngestionJobs already
    sorts by start time and carries the statistics, so a copy here would be a
    second source of truth that diverges the first time a sync fails.
    """

    job_id: str
    status: str
    started_at: Optional[str] = None
    updated_at: Optional[str] = None
    # Raw counters from IngestionJobStatistics, passed through for display.
    documents_scanned: int = 0
    documents_indexed: int = 0
    documents_modified: int = 0
    documents_deleted: int = 0
    documents_failed: int = 0
    documents_skipped: int = 0
    failure_reasons: List[str] = []

    @property
    def in_progress(self) -> bool:
        return self.status.upper() not in SYNC_TERMINAL_STATUSES


class KnowledgeBaseDetail(BaseModel):
    knowledge_base: KnowledgeBaseRecord
    documents: List[KnowledgeDocument] = []
    # None for UPLOAD, which has no sync, and for a pull connector that has never
    # been synced.
    sync: Optional[SyncJob] = None
