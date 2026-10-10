"""
AWS Agent Registry service.

Wraps three boto3 clients and maps their responses to typed models:
`agent-registry-control` (list/get/create/status of records), `agent-registry`
(semantic search), and `bedrock-agentcore-control` (gateways and runtimes,
which stay on the AgentCore namespace — only the Agent Registry APIs moved).

The registry left the `bedrock-agentcore` preview namespace on 2026-08-06 and
the old namespace shuts down on 2026-10-30 (AWS registry-faq; GA 2026-08). The new service
has a different record schema (`recordType` + a flat descriptor union keyed
`a2aAgentCard`/`mcpServer`/`agentSkillsDefinition`/`custom`, each carrying
`data`/`dataSchemaVersion`/`additionalData`), which the translation helpers
below own so the rest of the platform keeps its internal `descriptor_type`.
"""
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional, List, Dict, Any, Tuple, TypeVar
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from core.config import (
    AGENT_REGISTRY_ID,
    AGENT_RUNTIME_DISCOVERY_REGIONS,
    AP_USE_REGISTRY,
    AWS_REGION,
    REGISTRY_SYNC_ROLE_ARN,
)
from models.registry import (
    AgentRuntimeSummary,
    CreateRecordRequest,
    GatewaySummary,
    RegistryInfo,
    RegistryRecordDetail,
    RegistryRecordSummary,
    UpdateRecordRequest,
    DESCRIPTOR_A2A,
    DESCRIPTOR_AGENT_SKILLS,
    DESCRIPTOR_CUSTOM,
    DESCRIPTOR_MCP,
)

logger = logging.getLogger(__name__)

# Gateways and runtimes: AgentCore control plane (unchanged namespace).
_CONTROL_SERVICE = "bedrock-agentcore-control"
# Registry records: the `agent-registry` namespace, control and data planes.
_REGISTRY_CONTROL_SERVICE = "agent-registry-control"
_REGISTRY_DATA_SERVICE = "agent-registry"

# The A2A agent-card schema version accepted by CreateRegistryRecord.
_A2A_SCHEMA_VERSION = "0.3.0"
_MCP_SCHEMA_VERSION = "2025-12-11"
# server.json (the schema above) allows 1–100 characters for `description`, and
# AWS validates the descriptor against it on every Create/Update. The record's
# own description allows 4096, so the copy inside the descriptor is clipped
# rather than letting one long sentence reject the whole write.
_MCP_DESCRIPTION_MAX_CHARS = 100
_SKILL_SCHEMA_VERSION = "0.1.0"

# Internal descriptor_type -> agent-registry recordType, and the primary
# descriptor key each maps to. recordType is a required top-level field of the
# new schema; the descriptor union key is what tells the type apart on read.
_RECORD_TYPE = {
    DESCRIPTOR_A2A: "AGENT",
    DESCRIPTOR_MCP: "MCP",
    DESCRIPTOR_AGENT_SKILLS: "SKILL",
    DESCRIPTOR_CUSTOM: "CUSTOM",
}
_PRIMARY_KEY = {
    DESCRIPTOR_A2A: "a2aAgentCard",
    DESCRIPTOR_MCP: "mcpServer",
    DESCRIPTOR_AGENT_SKILLS: "agentSkillsDefinition",
    DESCRIPTOR_CUSTOM: "custom",
}
_KEY_TO_DESCRIPTOR = {v: k for k, v in _PRIMARY_KEY.items()}
# Descriptors this platform never writes but does read: organisation-wide
# auto-detection records an HTTP- or AG-UI-protocol runtime with only its
# invocation endpoint (`source.fromUrl`, no data). Both describe an agent, so
# they classify as A2A for the UI and bind chat through the endpoint URL.
_ENDPOINT_ONLY_KEYS = ("http", "agui")
for _key in _ENDPOINT_ONLY_KEYS:
    _KEY_TO_DESCRIPTOR[_key] = DESCRIPTOR_A2A
# Reverse of _RECORD_TYPE, for list summaries that carry recordType but no
# descriptors (ListRegistryRecords omits descriptors). GATEWAY is what
# auto-detection assigns a detected gateway; it is a tool surface like MCP.
_DESCRIPTOR_FROM_RECORD_TYPE = {v: k for k, v in _RECORD_TYPE.items()}
_DESCRIPTOR_FROM_RECORD_TYPE["GATEWAY"] = DESCRIPTOR_MCP
_GATEWAY_RECORD_TYPE = "GATEWAY"


def _record_type_of(descriptor_type: str) -> str:
    return _RECORD_TYPE[descriptor_type]


def _descriptor_type_of(descriptors: Optional[Dict[str, Any]]) -> Optional[str]:
    for key, descriptor_type in _KEY_TO_DESCRIPTOR.items():
        if descriptors and key in descriptors:
            return descriptor_type
    return None


def _endpoint(service: str, region: str) -> str:
    # botocore resolves agent-registry* to .amazonaws.com, a hostname that does
    # not exist in DNS; the service is served at .api.aws (registry-faq). Still
    # true as of botocore 1.43.106 (checked 2026-10-01), so set it explicitly.
    return f"https://{service}.{region}.api.aws"


def registry_mcp_endpoint(registry_id: str, region: str) -> str:
    """The registry's own MCP server (spec 2025-11-25) on the data plane.

    Exposes search/list/batch-get as MCP tools; authorised by the registry's
    discovery configuration (SigV4 here) plus `agent-registry:InvokeRegistryMcp`.
    """
    return f"{_endpoint(_REGISTRY_DATA_SERVICE, region)}/registry/{registry_id}/mcp"


# SigV4 service name AWS documents for synchronising from servers hosted on
# AgentCore Runtime or Gateway (registry-sync-records). Not `bedrock-agentcore`.
_SYNC_SIGNING_SERVICE = "agent-registry"

_INVOCATION_PATH = re.compile(r"/runtimes/([^/]+)/invocations")


def runtime_arn_from_invocation_url(url: str) -> tuple[Optional[str], Optional[str]]:
    """(runtime ARN, qualifier) from an AgentCore invocation URL, else (None, None).

    `https://bedrock-agentcore.<r>.amazonaws.com/runtimes/<url-encoded ARN>/invocations?qualifier=DEFAULT`
    is what auto-detection writes into an `http`/`agui` descriptor's source, and
    what the gateway module builds for runtime MCP targets.
    """
    if not isinstance(url, str):
        return None, None
    parts = urlsplit(url)
    match = _INVOCATION_PATH.search(parts.path)
    if not match:
        return None, None
    arn = unquote(match.group(1))
    if not arn.startswith("arn:") or ":runtime/" not in arn:
        return None, None
    qualifier = (parse_qs(parts.query).get("qualifier") or [None])[0]
    return arn, qualifier


def mcp_endpoint_of(url: str) -> str:
    """A gateway's MCP endpoint: GetGateway's `gatewayUrl` with `/mcp` ensured.

    Older gateways report the bare host (the live `bap-gateway` does) while newer
    ones include `/mcp`; MCP clients need the path, and a record carrying the
    bare host hung the harness's app discovery.
    """
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    if not path.endswith("/mcp"):
        path = f"{path}/mcp"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))

# SearchRegistryRecords caps maxResults at 20 and returns no nextToken.
_SEARCH_MAX_RESULTS = 20

# MCP server records are identified as "namespace/name" by the descriptor schema.
_MCP_NAMESPACE = "bap"

# Where a skill record records the S3 prefix holding its bundle.
#
# The AGENT_SKILLS descriptor has no field for a source: it takes SKILL.md and a
# skill definition, both inline, and AWS documents the markdown as "metadata for
# discovery purpose" with "Registry does not support storing other agent skill
# files". The 0.1.0 skill-definition schema does reserve `_meta` for
# "vendor-specific data" under reverse-DNS namespacing, and allows unknown fields
# for forward compatibility — so that is where the pointer goes, rather than in a
# key the schema might one day claim.
SKILL_SOURCE_META_KEY = "com.amazonaws.bap/skillSource"

# CreateRegistryRecord is asynchronous; records pass through CREATING first.
_CREATE_POLL_ATTEMPTS = 10
_CREATE_POLL_INTERVAL_SECONDS = 1.0

_ACTION_TO_STATUS = {
    "approve": "APPROVED",
    "reject": "REJECTED",
    "deprecate": "DEPRECATED",
}

# UpdateRegistryRecordStatus requires statusReason (botocore rejects the call
# client-side without it), so every action carries a default.
_ACTION_TO_REASON = {
    "approve": "Approved by a curator.",
    "reject": "Rejected by a curator.",
    "deprecate": "Retired by a curator.",
}

# ListRegistryRecords omits descriptors; approved records are recovered in bulk
# (BatchGetDiscoverableRegistryRecord, this many ids per call) and only the rest
# fan out one GetRegistryRecord each.
_BATCH_GET_MAX = 100
# How long a record's team is trusted when a pinned thread re-checks visibility.
TEAM_CACHE_SECONDS = 60
_FANOUT_WORKERS = 8

# botocore's default pool (10) is smaller than the fan-out of a few concurrent
# requests, and an exhausted pool serialises them again — which is the whole
# problem being fixed here. Sized for several in-flight listings at once.
BOTO_CONFIG = BotoConfig(max_pool_connections=_FANOUT_WORKERS * 6)

_T = TypeVar("_T")
_R = TypeVar("_R")


def fan_out(fn: Callable[[_T], _R], items: List[_T]) -> List[_R]:
    """Map `fn` over `items` concurrently, preserving order."""
    if len(items) < 2:
        return [fn(item) for item in items]
    with ThreadPoolExecutor(max_workers=min(_FANOUT_WORKERS, len(items))) as pool:
        return list(pool.map(fn, items))


def is_deprecated(record: "RegistryRecordSummary") -> bool:
    return (record.status or "").upper() == "DEPRECATED"


class RegistryNotConfigured(Exception):
    """Raised when AGENT_REGISTRY_ID is not set on the server."""


def registry_enabled() -> bool:
    """Whether registry calls should be attempted at all.

    Off when explicitly disabled (AP_USE_REGISTRY false) or when no registry id
    is configured. `auto` (the default) is on whenever an id exists. Callers that
    can serve from deployed resources instead check this first and skip the
    registry entirely, so an SCP-blocked or absent registry costs nothing.
    """
    if AP_USE_REGISTRY in ("0", "false", "no", "off"):
        return False
    return bool(AGENT_REGISTRY_ID)


def is_registry_unavailable(exc: Exception) -> bool:
    """A registry call failed in a way that means 'fall back', not 'error out'.

    Covers both the never-configured case and the AWS-side case (access denied
    by an SCP, a retired namespace, a transient endpoint failure), so callers
    degrade to deployed-resource fallbacks uniformly instead of each guessing.
    """
    return isinstance(exc, (RegistryNotConfigured, ClientError, BotoCoreError))


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else str(value)


def _slug(name: str) -> str:
    """
    Skill names must be 1-64 lowercase alphanumerics/hyphens, with no leading,
    trailing, or repeated hyphens.
    """
    lowered = "".join(c if c.isalnum() else "-" for c in name.lower())
    parts = [p for p in lowered.split("-") if p]
    return "-".join(parts)[:64].strip("-") or "skill"


def _semver(version: Optional[str]) -> str:
    """Descriptor schemas require a full MAJOR.MINOR.PATCH version."""
    parts = (version or "1.0.0").split(".")
    while len(parts) < 3:
        parts.append("0")
    return ".".join(parts[:3])


def _clip_mcp_description(text: Any, fallback: str) -> str:
    """
    Fit a description into server.json's 1–100 character window.

    The record keeps the full text (and search indexes the record, not just the
    descriptor), so the descriptor copy only needs to stay valid and readable:
    cut on the limit with a one-character ellipsis. Empty falls back to the
    name because the schema also sets minLength 1.
    """
    value = str(text).strip() if text is not None else ""
    if not value:
        value = fallback
    if len(value) <= _MCP_DESCRIPTION_MAX_CHARS:
        return value
    return value[: _MCP_DESCRIPTION_MAX_CHARS - 1].rstrip() + "…"


def _parse_inline(content: Optional[str]) -> Optional[Any]:
    """Descriptor payloads are transported as strings; decode JSON when possible."""
    if not content:
        return None
    try:
        return json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return content


def _extract_descriptor_content(descriptors: Optional[Dict[str, Any]]) -> Optional[Any]:
    if not descriptors:
        return None
    if "a2aAgentCard" in descriptors:
        return _parse_inline(descriptors["a2aAgentCard"].get("data"))
    if "mcpServer" in descriptors:
        mcp = descriptors["mcpServer"]
        tools = (mcp.get("additionalData") or {}).get("tools") or {}
        return {
            "server": _parse_inline(mcp.get("data")),
            "tools": _parse_inline(tools.get("data")),
        }
    if "custom" in descriptors:
        return _parse_inline(descriptors["custom"].get("data"))
    if "agentSkillsDefinition" in descriptors:
        skills = descriptors["agentSkillsDefinition"]
        skill_md = (skills.get("additionalData") or {}).get("skillMd") or {}
        return {
            "skillMd": skill_md.get("data"),
            "skillDefinition": _parse_inline(skills.get("data")),
        }
    for key in _ENDPOINT_ONLY_KEYS:
        if key in descriptors:
            # Endpoint-only descriptor: the URL is the whole definition.
            url = ((descriptors[key].get("source") or {}).get("fromUrl") or {}).get("url")
            return {"protocol": key, "url": url}
    return None


def gateway_arn_of(descriptor_content: Any) -> Optional[str]:
    """
    Read the AgentCore gateway ARN out of an MCP record's descriptor payload.

    Set only for records registered from a deployed gateway; a plain MCP server
    (say a hosted third-party endpoint) has none.
    """
    if not isinstance(descriptor_content, dict):
        return None
    server = descriptor_content.get("server")
    if not isinstance(server, dict):
        return None
    arn = server.get("gatewayArn")
    return arn if isinstance(arn, str) and arn.startswith("arn:") else None


def skill_source_of(descriptor_content: Any) -> Optional[Dict[str, Any]]:
    """
    Read the S3 bundle pointer out of an AGENT_SKILLS record's descriptor payload.

    None for a record whose SKILL.md was entered inline — every record created
    before bundle uploads existed — which is why the harness composer keeps its
    publish-the-markdown fallback.
    """
    if not isinstance(descriptor_content, dict):
        return None
    definition = descriptor_content.get("skillDefinition")
    if not isinstance(definition, dict):
        return None
    meta = definition.get("_meta")
    if not isinstance(meta, dict):
        return None
    source = meta.get(SKILL_SOURCE_META_KEY)
    if not isinstance(source, dict):
        return None
    uri = source.get("uri")
    return source if isinstance(uri, str) and uri.startswith("s3://") else None


def _extract_runtime_binding(
    content: Any,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Pull the AgentCore invocation target out of a descriptor payload.

    Agent records written by this platform put the ARN in the A2A agent card's
    `url` (and mirror it under `agentRuntimeArn`/`harnessArn`), which is what lets
    the chat UI invoke the right target without guessing from the record name.

    Returns (agent_runtime_arn, harness_arn, qualifier). The two ARNs are kept
    apart because a harness-managed runtime rejects InvokeAgentRuntime — a harness
    ARN leaking into agent_runtime_arn would route chat to the wrong API.
    """
    if not isinstance(content, dict):
        return None, None, None

    qualifier = content.get("qualifier")
    candidates = [
        content.get("harnessArn"),
        content.get("agentRuntimeArn"),
        content.get("agent_runtime_arn"),
        content.get("url"),
    ]

    provider = content.get("provider")
    if isinstance(provider, dict):
        candidates.extend([provider.get("agentRuntimeArn"), provider.get("url")])

    runtime_arn: Optional[str] = None
    harness_arn: Optional[str] = None
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        if candidate.startswith("https://"):
            # An invocation URL (auto-detected `http`/`agui` descriptors) names
            # the runtime inside its path and the qualifier in its query.
            from_url, url_qualifier = runtime_arn_from_invocation_url(candidate)
            if from_url:
                runtime_arn = runtime_arn or from_url
                qualifier = qualifier or url_qualifier
            continue
        if not candidate.startswith("arn:"):
            continue
        if ":harness/" in candidate:
            harness_arn = harness_arn or candidate
        elif ":runtime/" in candidate:
            runtime_arn = runtime_arn or candidate

    return runtime_arn, harness_arn, qualifier


def _provenance_source(record: Dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """(sourceId, sourceType) of the DETECTED_FROM provenance entry, if any."""
    for entry in record.get("provenance") or []:
        if isinstance(entry, dict) and entry.get("relation") == "DETECTED_FROM":
            return entry.get("sourceId"), entry.get("sourceType")
    return None, None


def _to_summary(
    record: Dict[str, Any], discoverable: Optional[bool] = None
) -> RegistryRecordSummary:
    content = _extract_descriptor_content(record.get("descriptors"))
    arn, harness_arn, qualifier = _extract_runtime_binding(content)
    source_arn, source_type = _provenance_source(record)
    # ListRegistryRecords carries recordType but no descriptors; without the
    # fallback those summaries get descriptor_type=None, the hydration filter
    # in list_records never matches, and agent records lose their invoke ARN.
    descriptor_type = _descriptor_type_of(record.get("descriptors")) or (
        _DESCRIPTOR_FROM_RECORD_TYPE.get(record.get("recordType"))
    )
    return RegistryRecordSummary(
        record_id=record.get("recordId", ""),
        name=record.get("displayName") or record.get("name", ""),
        description=record.get("description"),
        descriptor_type=descriptor_type,
        version=record.get("recordVersion") or record.get("version"),
        status=record.get("status"),
        created_at=_iso(record.get("createdAt")),
        updated_at=_iso(record.get("updatedAt")),
        record_arn=record.get("recordArn"),
        agent_runtime_arn=arn,
        harness_arn=harness_arn,
        qualifier=qualifier,
        discoverable=discoverable,
        custom_metadata=record.get("customMetadata") or None,
        compliance_status=record.get("customMetadataSchemaComplianceStatus"),
        auto_detected=bool(record.get("createdByAutoDetection")) or source_arn is not None,
        source_arn=source_arn,
        source_type=source_type,
    )


def _sync_source_of(descriptors: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The `source.fromUrl` of the primary descriptor, flattened for the UI."""
    if not descriptors:
        return None
    for key in _KEY_TO_DESCRIPTOR:
        descriptor = descriptors.get(key)
        if not isinstance(descriptor, dict):
            continue
        from_url = (descriptor.get("source") or {}).get("fromUrl")
        if not isinstance(from_url, dict) or not from_url.get("url"):
            return None
        summary: Dict[str, Any] = {"url": from_url["url"], "credential": "none"}
        for config in from_url.get("credentialProviderConfigurations") or []:
            kind = (config.get("credentialProviderType") or "").lower()
            provider = config.get("credentialProvider") or {}
            if kind == "iam":
                summary["credential"] = "iam"
                summary["role_arn"] = (provider.get("iamCredentialProvider") or {}).get("roleArn")
            elif kind == "oauth":
                summary["credential"] = "oauth"
                summary["provider_arn"] = (provider.get("oauthCredentialProvider") or {}).get(
                    "providerArn"
                )
        return summary
    return None


def _to_detail(
    record: Dict[str, Any],
    discoverable: Optional[bool] = None,
    revision: Optional[str] = None,
) -> RegistryRecordDetail:
    summary = _to_summary(record, discoverable=discoverable).model_dump()
    descriptors = record.get("descriptors")
    return RegistryRecordDetail(
        **summary,
        descriptors=descriptors,
        descriptor_content=_extract_descriptor_content(descriptors),
        status_reason=record.get("statusReason"),
        sync_config=None,
        sync_source=_sync_source_of(descriptors),
        provenance=record.get("provenance") or None,
        revision=revision,
    )


class _DescriptorSource:
    """
    The descriptor fields _build_descriptors reads, assembled from a record's
    current content plus an edit's overrides.
    """

    def __init__(self, **fields: Any):
        for key, value in fields.items():
            setattr(self, key, value)


def _existing_payload(current: RegistryRecordDetail) -> Any:
    """
    The record's current descriptor content, in the flat shape _build_descriptors
    merges into `content`.

    _extract_descriptor_content nests per type — MCP as {"server":…, "tools":…}
    and AGENT_SKILLS as {"skillMd":…, "skillDefinition":…} — so passing it
    straight back through would bury an MCP server's `remotes` one level down
    and lose the endpoint. A2A and CUSTOM are already flat.
    """
    content = current.descriptor_content
    if not isinstance(content, dict):
        return content
    if current.descriptor_type == DESCRIPTOR_MCP:
        server = content.get("server")
        return dict(server) if isinstance(server, dict) else {}
    if current.descriptor_type == DESCRIPTOR_AGENT_SKILLS:
        definition = content.get("skillDefinition")
        return dict(definition) if isinstance(definition, dict) else {}
    return dict(content)


def _merge_descriptor_content(
    current: RegistryRecordDetail, req: UpdateRecordRequest
) -> _DescriptorSource:
    """Overlay an edit onto the record's existing descriptor content."""
    base = _existing_payload(current)
    if isinstance(req.content, dict) and isinstance(base, dict):
        base.update(req.content)
    elif req.content is not None:
        # A non-dict payload (e.g. a CUSTOM record's raw string) replaces it.
        base = req.content

    name = req.name if req.name is not None else current.name
    description = (
        req.description if req.description is not None else current.description
    )
    if isinstance(base, dict):
        if description is not None:
            # Description drives semantic search relevance, so keep the copy
            # inside the descriptor in step with the record's own description.
            base["description"] = description
        if req.name is not None:
            # The descriptor is indexed too; a stale name would keep matching
            # the old one.
            base["name"] = name

    existing_content = current.descriptor_content
    markdown = req.skill_markdown
    if markdown is None and isinstance(existing_content, dict):
        # Without this, an unrelated edit falls through to the generated stub
        # in _build_descriptors and destroys the real SKILL.md.
        existing_markdown = existing_content.get("skillMd")
        if isinstance(existing_markdown, str) and existing_markdown.strip():
            markdown = existing_markdown

    # The descriptor's own version — current.version is the record revision, a
    # different thing that would overwrite an agent card's or MCP server's semver.
    version: Optional[str] = None
    if isinstance(base, dict) and isinstance(base.get("version"), str):
        version = base["version"]

    return _DescriptorSource(
        descriptor_type=current.descriptor_type,
        name=name,
        description=description,
        version=version,
        content=base,
        skill_markdown=markdown,
        # Only set when the bundle is being replaced. An unset one leaves the
        # existing `_meta` alone: `base` came from the record's own
        # skillDefinition, so the pointer is already in there.
        skill_source=req.skill_source,
        # A2A rebuilds require a binding, and _build_descriptors reads these
        # attributes rather than the merged payload.
        agent_runtime_arn=current.agent_runtime_arn,
        harness_arn=current.harness_arn,
        qualifier=current.qualifier,
    )


def _attach_sync_source(descriptors: Dict[str, Any], req: Any) -> Dict[str, Any]:
    """
    Add `source.fromUrl` to the primary descriptor when the request asks for
    synchronisation. Only MCP and A2A descriptors can carry a source (AWS); the
    data built alongside it is kept — AWS overlays what it fetches and preserves
    keys it does not know, such as `gatewayArn` (verified live 2026-10-10).
    """
    sync_url = getattr(req, "sync_url", None)
    if not sync_url:
        return descriptors
    key = next(iter(descriptors))
    if key not in ("mcpServer", "a2aAgentCard"):
        raise ValueError(
            "Synchronisation from an endpoint is only supported for MCP and A2A records."
        )
    from_url: Dict[str, Any] = {"url": sync_url}
    role_arn = getattr(req, "sync_role_arn", None)
    if role_arn:
        from_url["credentialProviderConfigurations"] = [
            {
                "credentialProviderType": "IAM",
                "credentialProvider": {
                    "iamCredentialProvider": {
                        "roleArn": role_arn,
                        "service": _SYNC_SIGNING_SERVICE,
                    }
                },
            }
        ]
    descriptors[key]["source"] = {"fromUrl": from_url}
    return descriptors


def _build_descriptors(req: Any) -> Dict[str, Any]:
    """Descriptors for a create/edit request: the per-type payload plus, when
    asked, the synchronisation source."""
    return _attach_sync_source(_build_primary_descriptors(req), req)


def _build_primary_descriptors(req: Any) -> Dict[str, Any]:
    """
    Map a create or edit request onto the descriptor union AWS expects per type.

    Takes any object carrying the descriptor fields (`descriptor_type`, `name`,
    `description`, `version`, `content`, `skill_markdown`, and the A2A/MCP
    binding attributes) so create and update can share it. Missing optional
    attributes are read as None.
    """
    def opt(attr: str) -> Any:
        """Optional attribute, so an edit request need not carry create-only fields."""
        return getattr(req, attr, None)

    if req.descriptor_type == DESCRIPTOR_A2A:
        if not opt("agent_runtime_arn") and not opt("harness_arn"):
            raise ValueError(
                "agent_runtime_arn or harness_arn is required for A2A agent records"
            )
        # Minimal A2A agent card. `url` carries the invocation target so the chat UI
        # can invoke this agent directly from the record. For harness-backed agents
        # that is the harness ARN, since the companion runtime rejects direct calls.
        card: Dict[str, Any] = {
            "protocolVersion": _A2A_SCHEMA_VERSION,
            "name": req.name,
            "description": req.description or req.name,
            "version": opt("version") or "1.0",
            "url": opt("harness_arn") or opt("agent_runtime_arn"),
            "capabilities": {"streaming": True},
            "defaultInputModes": ["text/plain"],
            "defaultOutputModes": ["text/plain"],
            "skills": [],
        }
        if opt("agent_runtime_arn"):
            card["agentRuntimeArn"] = opt("agent_runtime_arn")
        if opt("harness_arn"):
            card["harnessArn"] = opt("harness_arn")
        if opt("qualifier"):
            card["qualifier"] = opt("qualifier")
        if isinstance(req.content, dict):
            card.update(req.content)
        return {
            "a2aAgentCard": {
                "data": json.dumps(card),
                "dataSchemaVersion": _A2A_SCHEMA_VERSION,
            }
        }

    if req.descriptor_type == DESCRIPTOR_MCP:
        overrides = req.content if isinstance(req.content, dict) else {}
        server = {
            # The MCP server schema requires a "namespace/name" identifier, a
            # description, and a semver version — all three are mandatory.
            "name": req.name if "/" in req.name else f"{_MCP_NAMESPACE}/{req.name}",
            "description": req.description or req.name,
            "version": _semver(opt("version")),
        }
        if opt("remote_url"):
            # Endpoint the harness composer wires up as a remote_mcp tool.
            server["remotes"] = [
                {"type": "streamable-http", "url": opt("remote_url")}
            ]
        if opt("gateway_arn"):
            # An AgentCore gateway is reachable over plain streamable-HTTP like any
            # MCP server, but it needs SigV4 or a JWT — which a bare `remote_mcp`
            # tool cannot supply. Recording the ARN lets the composer attach it as a
            # native `agentcore_gateway` tool instead, and lets the sync recognise
            # the gateway as already registered. Unknown keys are preserved verbatim
            # by CreateRegistryRecord, so this survives a round trip.
            server["gatewayArn"] = opt("gateway_arn")
        server.update(overrides)
        # Clipped after the merge: an edit arrives as an override carrying the
        # record's full description, and that is the copy the schema rejects.
        server["description"] = _clip_mcp_description(
            server.get("description"), fallback=req.name
        )
        return {
            "mcpServer": {
                "data": json.dumps(server),
                "dataSchemaVersion": _MCP_SCHEMA_VERSION,
            }
        }

    if req.descriptor_type == DESCRIPTOR_AGENT_SKILLS:
        markdown = req.skill_markdown or (
            f"---\nname: {_slug(req.name)}\n"
            f"description: {req.description or req.name}\n---\n"
        )
        definition = req.content if isinstance(req.content, dict) else {}
        source = opt("skill_source")
        if isinstance(source, dict):
            # The bundle itself cannot live in the record, so the record carries
            # the prefix it was published to. `_meta` is merged rather than
            # replaced so an edit that only sets the source keeps whatever else a
            # caller put there.
            meta = definition.get("_meta")
            definition["_meta"] = {
                **(meta if isinstance(meta, dict) else {}),
                SKILL_SOURCE_META_KEY: source,
            }
        return {
            "agentSkillsDefinition": {
                "data": json.dumps(definition),
                "dataSchemaVersion": _SKILL_SCHEMA_VERSION,
                "additionalData": {"skillMd": {"data": markdown}},
            }
        }

    if req.descriptor_type == DESCRIPTOR_CUSTOM:
        payload = req.content if req.content is not None else {
            "name": req.name,
            "description": req.description or req.name,
        }
        if isinstance(payload, str):
            inline = payload
        else:
            inline = json.dumps(payload)
        return {"custom": {"data": inline}}

    raise ValueError(f"Unsupported descriptor type: {req.descriptor_type}")


def _wrap_optional(value: Any) -> Dict[str, Any]:
    return {"optionalValue": value}


def _wrap_updated_descriptor(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Wrap one primary descriptor's fields (data/dataSchemaVersion/source/
    additionalData), each independently `optionalValue`d, per
    UpdateRegistryRecord's *DescriptorFields shapes.

    `additionalData` nests a second descriptor-shaped union one level down
    (MCP's `tools`, AGENT_SKILLS' `skillMd`), so it recurses through this same
    wrapping rather than being wrapped as a single opaque value.
    """
    fields: Dict[str, Any] = {}
    for key, value in payload.items():
        if key == "additionalData" and isinstance(value, dict):
            fields[key] = _wrap_optional(
                {
                    inner_key: _wrap_optional(_wrap_updated_descriptor(inner_value))
                    for inner_key, inner_value in value.items()
                }
            )
        else:
            fields[key] = _wrap_optional(value)
    return fields


def _as_updated_descriptors(descriptors: Dict[str, Any]) -> Dict[str, Any]:
    """
    Wrap create-shaped descriptors for UpdateRegistryRecord's PATCH semantics.

    Every independently-unsettable level gets its own `optionalValue`: the
    union, each primary descriptor key, and every field inside it — including,
    one level deeper, each field nested under `additionalData`. Passing the
    create shape through is rejected client-side by botocore ("Unknown
    parameter … must be one of: optionalValue"), and a shallower two-level wrap
    validates locally but the real API rejects it: see
    test_update_descriptor_shape.py, which runs the real service model.
    """
    return _wrap_optional(
        {
            key: _wrap_optional(_wrap_updated_descriptor(payload))
            for key, payload in descriptors.items()
        }
    )


def _parse_metadata_schema(config: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    GetRegistry's `customMetadataSchemaConfiguration` as parsed JSON Schema per
    record type, the default under "DEFAULT". AWS stores each schema as a
    string; a string that fails to parse is dropped rather than failing /info.
    """
    if not config:
        return None
    schemas: Dict[str, Any] = {}
    default = config.get("defaultSchema")
    if isinstance(default, str) and default.strip():
        parsed = _parse_inline(default)
        if isinstance(parsed, dict):
            schemas["DEFAULT"] = parsed
    for override in config.get("recordTypeSchemaOverrides") or []:
        parsed = _parse_inline(override.get("schema"))
        if override.get("recordType") and isinstance(parsed, dict):
            schemas[override["recordType"]] = parsed
    return schemas or None


# The metadata field the server fills with the registering user. Named after the
# field in the AWS documentation's example schema; the module's default schema
# defines it as optional text.
OWNER_FIELD = "owner"


# sub -> e-mail, installed by core.dependencies from the directory service.
# Module state rather than a constructor argument because owner_identity is
# called from routes, the harness composer and the sync service alike.
_owner_resolver: Optional[Callable[[str], Optional[str]]] = None


def set_owner_resolver(resolver: Optional[Callable[[str], Optional[str]]]) -> None:
    global _owner_resolver
    _owner_resolver = resolver


def owner_identity(user: Any) -> Optional[str]:
    """The value written into a record's `owner` metadata for `user` (an AuthUser).

    The e-mail: the Cognito pool here uses the e-mail as the sign-in name, so the
    token's `username` is the opaque `sub` UUID, which tells a curator nothing on
    a card. A Cognito *access* token carries no e-mail claim at all (measured
    live 2026-10-10: the first deploy stamped the UUID), so when the token has
    none the sub is looked up in the user directory. `username` and `sub` are
    the last resorts for a sub the directory does not know.
    """
    if user is None:
        return None
    email = getattr(user, "email", None)
    if isinstance(email, str) and email.strip():
        return email.strip()
    sub = getattr(user, "sub", None)
    if _owner_resolver is not None and isinstance(sub, str) and sub.strip():
        try:
            resolved = _owner_resolver(sub.strip())
        except Exception:
            logger.info("Owner e-mail lookup failed for %s; stamping the sub", sub, exc_info=True)
            resolved = None
        if isinstance(resolved, str) and resolved.strip():
            return resolved.strip()
    for attr in ("username", "sub"):
        value = getattr(user, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _search_filters(
    descriptor_types: Optional[List[str]], custom_metadata: Optional[Dict[str, str]]
) -> Optional[Dict[str, Any]]:
    """
    The `filters` expression SearchDiscoverableRegistryRecords evaluates before
    ranking. One clause comes back bare; several are joined with `$and`. Blank
    metadata values are dropped rather than sent as `$eq ""`, which the UI emits
    for an un-chosen filter chip.
    """
    clauses: List[Dict[str, Any]] = []
    if descriptor_types:
        clauses.append(
            {"recordType": {"$in": [_record_type_of(t) for t in descriptor_types]}}
        )
    for key, value in (custom_metadata or {}).items():
        if value is None or not str(value).strip() or not key.strip():
            continue
        clauses.append({f"customMetadata.{key.strip()}": {"$eq": str(value).strip()}})
    if not clauses:
        return None
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}


class RegistryService:
    def __init__(self, registry_id: Optional[str] = None, region: Optional[str] = None):
        self.registry_id = registry_id if registry_id is not None else AGENT_REGISTRY_ID
        self.region = region or AWS_REGION
        self._registry_control = None
        self._registry_data = None
        self._agentcore_control = None
        # Per-region AgentCore control-plane clients for runtimes deployed outside
        # this platform's own region (see AGENT_RUNTIME_DISCOVERY_REGIONS). Keyed
        # by region name; this platform's region uses `agentcore_control` itself.
        self._region_controls: Dict[str, Any] = {}
        # boto3 clients are safe to call from several threads but not safe to
        # *create* concurrently, and listings now fan out across a threadpool.
        self._lock = threading.Lock()
        self._team_cache: Dict[str, Tuple[float, Optional[str]]] = {}

    def _require_configured(self) -> None:
        if not self.registry_id:
            raise RegistryNotConfigured(
                "AGENT_REGISTRY_ID is not configured on the server. "
                "Deploy infra/modules/agent_registry and set the value from "
                "`terraform output agent_registry_id`."
            )

    @property
    def registry_control(self):
        if self._registry_control is None:
            with self._lock:
                if self._registry_control is None:
                    self._registry_control = boto3.client(
                        _REGISTRY_CONTROL_SERVICE,
                        region_name=self.region,
                        endpoint_url=_endpoint(_REGISTRY_CONTROL_SERVICE, self.region),
                        config=BOTO_CONFIG,
                    )
        return self._registry_control

    @property
    def registry_data(self):
        if self._registry_data is None:
            with self._lock:
                if self._registry_data is None:
                    self._registry_data = boto3.client(
                        _REGISTRY_DATA_SERVICE,
                        region_name=self.region,
                        endpoint_url=_endpoint(_REGISTRY_DATA_SERVICE, self.region),
                        config=BOTO_CONFIG,
                    )
        return self._registry_data

    @property
    def agentcore_control(self):
        if self._agentcore_control is None:
            with self._lock:
                if self._agentcore_control is None:
                    self._agentcore_control = boto3.client(
                        _CONTROL_SERVICE, region_name=self.region, config=BOTO_CONFIG
                    )
        return self._agentcore_control

    def get_registry_info(self) -> RegistryInfo:
        self._require_configured()
        resp = self.registry_control.get_registry(registryId=self.registry_id)
        rules = (resp.get("approvalConfiguration") or {}).get("autoApprovalRules") or []
        auto_detection = resp.get("autoDetection") or {}
        registry_id = resp.get("registryId", self.registry_id)
        return RegistryInfo(
            registry_id=registry_id,
            name=resp.get("name"),
            description=resp.get("description"),
            status=resp.get("status"),
            auto_approval="APPROVE_ALL" in rules,
            registry_arn=resp.get("registryArn"),
            mcp_endpoint=registry_mcp_endpoint(registry_id, self.region),
            custom_metadata_schema=_parse_metadata_schema(
                resp.get("customMetadataSchemaConfiguration")
            ),
            auto_detection=(
                {
                    "enabled": bool((auto_detection.get("configuration") or {}).get("enabled")),
                    "status": auto_detection.get("status"),
                }
                if auto_detection
                else None
            ),
            kms_key_arn=(resp.get("encryptionConfiguration") or {}).get("kmsKeyArn"),
            sync_role_arn=REGISTRY_SYNC_ROLE_ARN or None,
        )

    def _schema_has_field(self, record_type: str, field: str) -> bool:
        """Whether the registry's metadata schema for `record_type` (its own
        override, else DEFAULT) defines `field`. False when there is no schema."""
        schemas = self.get_registry_info().custom_metadata_schema or {}
        schema = schemas.get(record_type) or schemas.get("DEFAULT") or {}
        return field in (schema.get("properties") or {})

    def list_records(
        self,
        descriptor_type: Optional[str] = None,
        status: Optional[str] = None,
        name: Optional[str] = None,
        record_type: Optional[str] = None,
    ) -> List[RegistryRecordSummary]:
        """
        Records by (one) type, status or exact name. `record_type` is the raw AWS
        value for types this platform has no descriptor_type of its own for
        (GATEWAY); each List filter takes exactly one value, so a multi-type
        listing is several calls.
        """
        self._require_configured()
        params: Dict[str, Any] = {"registryId": self.registry_id, "maxResults": 100}
        # The new List API takes structured filters instead of per-field params.
        filters: List[Dict[str, Any]] = []
        if record_type:
            filters.append({"name": "recordType", "values": [record_type]})
        elif descriptor_type:
            filters.append({"name": "recordType", "values": [_record_type_of(descriptor_type)]})
        if status:
            filters.append({"name": "status", "values": [status]})
        if name:
            filters.append({"name": "name", "values": [name]})
        if filters:
            params["filters"] = filters

        summaries: List[RegistryRecordSummary] = []
        token: Optional[str] = None
        while True:
            if token:
                params["nextToken"] = token
            resp = self.registry_control.list_registry_records(**params)
            summaries.extend(_to_summary(r) for r in resp.get("registryRecords", []))
            token = resp.get("nextToken")
            if not token:
                break
        return self._hydrate_all(summaries)

    def _hydrate_all(
        self, summaries: List[RegistryRecordSummary]
    ) -> List[RegistryRecordSummary]:
        """
        Fill in what ListRegistryRecords omits: descriptors (hence the invoke ARN
        agent records are chattable through) and discoverability.

        One BatchGetDiscoverableRegistryRecord covers every record with an
        approved revision — 100 per call, descriptors included — so only records
        the data plane does not serve (never approved) still cost a
        GetRegistryRecord each. Before this the listing fanned out one Get per
        agent record against a 10 TPS quota.

        Metadata is attached to every record from its approved revision because
        the team filter (services/team_access.py) reads it; never-approved
        records cost one Get each, which is the only path that still does.
        """
        needs_descriptor = {
            summary.record_id
            for summary in summaries
            if summary.descriptor_type in (DESCRIPTOR_A2A, DESCRIPTOR_CUSTOM)
            and not (summary.agent_runtime_arn or summary.harness_arn)
        }
        try:
            approved = self.batch_get_discoverable([s.record_id for s in summaries])
        except Exception as exc:  # noqa: BLE001 — a listing beats a 500
            # Without metadata the team of every record is unknown; flag it so
            # the team filter fails closed instead of listing team records as shared.
            logger.warning("BatchGetDiscoverable failed; listing without metadata: %s", exc)
            return [s.model_copy(update={"visibility_known": False}) for s in summaries]

        def hydrate(summary: RegistryRecordSummary) -> RegistryRecordSummary:
            discoverable = summary.record_id in approved
            if discoverable:
                record = approved[summary.record_id]
                if summary.record_id in needs_descriptor and summary.status == "APPROVED":
                    # The approved revision *is* the latest one here.
                    return _to_summary(record, discoverable=True)
                # Metadata (team, tier, …) rides on the approved revision; the
                # List summary has none. Keep the latest revision's fields, add it.
                return summary.model_copy(update={
                    "discoverable": True,
                    "custom_metadata": record.get("customMetadata") or None,
                    "compliance_status": summary.compliance_status
                    or record.get("customMetadataSchemaComplianceStatus"),
                })
            # Never approved: only a Get knows its descriptors and metadata.
            try:
                detail = self.get_record(summary.record_id)
            except Exception as exc:
                logger.warning("Could not hydrate record %s: %s", summary.record_id, exc)
                return summary.model_copy(update={"discoverable": False, "visibility_known": False})
            return RegistryRecordSummary(
                **detail.model_dump(include=set(RegistryRecordSummary.model_fields))
            ).model_copy(update={"discoverable": False})

        # Only the never-approved leftovers actually reach the network here.
        return fan_out(hydrate, summaries)

    def batch_get_discoverable(self, record_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        Approved revisions of the given records, keyed by record id.

        The data plane answers only for records with an APPROVED revision, and
        reports the rest in `errors` instead of failing the call — so a missing
        key means "not discoverable", not "unknown". 100 ids per call is the API
        cap; the single `entries` element is the registry.
        """
        found: Dict[str, Dict[str, Any]] = {}
        ids = [rid for rid in record_ids if rid]
        for start in range(0, len(ids), _BATCH_GET_MAX):
            chunk = ids[start : start + _BATCH_GET_MAX]
            resp = self.registry_data.batch_get_discoverable_registry_record(
                entries=[{"registryId": self.registry_id, "recordIds": chunk}]
            )
            for record in resp.get("registryRecords", []):
                found[record.get("recordId", "")] = record
        return found

    def approved_revision(self, record_id: str) -> Optional[RegistryRecordDetail]:
        """The discoverable (approved) revision of a record, or None."""
        self._require_configured()
        record = self.batch_get_discoverable([record_id]).get(record_id)
        if record is None:
            return None
        return _to_detail(record, discoverable=True, revision="approved")

    def chattable_record(self, record_id: str) -> RegistryRecordDetail:
        """
        The revision chat should bind to.

        The latest revision when it is APPROVED. Otherwise the approved revision
        AWS still serves to consumers (an edit of an approved record opens a
        DRAFT alongside it, and the approved one stays discoverable until the
        new one is approved) — so an edit never cuts off chat with an agent that
        curation already signed off on, and the binding is that revision's, not
        the unreviewed draft's. When nothing is discoverable the latest is
        returned as-is, status and all, for the caller to refuse.
        """
        latest = self.get_record(record_id)
        if (latest.status or "").upper() == "APPROVED":
            return latest.model_copy(update={"discoverable": True})
        try:
            approved = self.approved_revision(record_id)
        except Exception as exc:
            logger.warning("Could not read approved revision of %s: %s", record_id, exc)
            approved = None
        if approved is not None:
            return approved
        return latest.model_copy(update={"discoverable": False})

    def get_record(self, record_id: str) -> RegistryRecordDetail:
        self._require_configured()
        resp = self.registry_control.get_registry_record(
            registryId=self.registry_id, recordId=record_id
        )
        return _to_detail(resp)

    def team_of_record(self, record_id: str) -> Optional[str]:
        """The team of the revision chat binds to, cached briefly: a pinned
        thread re-checks visibility every turn (services/agent_access.py)."""
        hit = self._team_cache.get(record_id)
        now = time.monotonic()
        if hit and now - hit[0] < TEAM_CACHE_SECONDS:
            return hit[1]
        team = self.chattable_record(record_id).team
        self._team_cache[record_id] = (now, team)
        return team

    def search_records(
        self,
        query: str,
        descriptor_types: Optional[List[str]] = None,
        custom_metadata: Optional[Dict[str, str]] = None,
    ) -> List[RegistryRecordSummary]:
        """
        Hybrid (semantic + keyword) search over approved records.

        Narrowing is done by AWS, before ranking: `filters` takes `$eq`/`$ne`/`$in`
        over `recordType`, `name`, `recordVersion` and `customMetadata.<field>`,
        joined with `$and`/`$or` (verified live 2026-10-10 — an earlier version of
        this code filtered client-side on the belief that the API took no type
        filter, which capped a typed search at "whatever of the 20 happened to
        match"). The returned order is AWS's relevance ranking and is passed
        through untouched.
        """
        self._require_configured()
        params: Dict[str, Any] = {
            "searchQuery": query,
            "registryIds": [self.registry_id],
            # The API rejects anything above 20, and offers no nextToken.
            "maxResults": _SEARCH_MAX_RESULTS,
        }
        filters = _search_filters(descriptor_types, custom_metadata)
        if filters:
            params["filters"] = filters
        resp = self.registry_data.search_discoverable_registry_records(**params)
        return [_to_summary(r) for r in resp.get("registryRecords", [])]

    def create_record(
        self, req: CreateRecordRequest, owner: Optional[str] = None
    ) -> RegistryRecordSummary:
        """
        `owner` is the caller's identity (see `owner_identity`). It is written
        into the `owner` metadata field over anything the client sent, so the
        field is a fact about who registered the record rather than a form
        value. Only when the registry's schema for the record type has that
        field — a registry without it rejects the whole create.
        """
        self._require_configured()
        descriptors = _build_descriptors(req)
        if owner and self._schema_has_field(_record_type_of(req.descriptor_type), OWNER_FIELD):
            req = req.model_copy(
                update={"custom_metadata": {**(req.custom_metadata or {}), OWNER_FIELD: owner}}
            )
        params: Dict[str, Any] = {
            "registryId": self.registry_id,
            "name": req.name,
            "displayName": req.name,
            "recordType": _record_type_of(req.descriptor_type),
            "descriptors": descriptors,
        }
        if req.description:
            params["description"] = req.description
        if req.version:
            params["recordVersion"] = req.version
        # Both only when non-empty: a registry without a metadata schema rejects
        # `customMetadata: {}`, and empty tags are noise.
        if req.custom_metadata:
            params["customMetadata"] = req.custom_metadata
        if req.tags:
            params["tags"] = req.tags

        resp = self.registry_control.create_registry_record(**params)
        record_id = resp.get("recordArn", "").rsplit("/", 1)[-1]
        if not record_id:
            return RegistryRecordSummary(
                record_id="",
                name=req.name,
                descriptor_type=req.descriptor_type,
                status=resp.get("status"),
            )

        if req.submit_for_approval:
            try:
                self._wait_out_of_creating(record_id)
                return self.update_status(record_id, "submit")
            except Exception as exc:
                # The record exists either way; surface it as DRAFT rather than
                # failing the whole create.
                logger.warning("Submit for approval failed for %s: %s", record_id, exc)

        return RegistryRecordSummary(
            **self.get_record(record_id).model_dump(
                include=set(RegistryRecordSummary.model_fields)
            )
        )

    def _wait_out_of_creating(self, record_id: str) -> None:
        """
        Block until a freshly created record leaves CREATING.

        CreateRegistryRecord is asynchronous, and submitting for approval while
        the record is still CREATING is rejected — which would silently leave it
        as a DRAFT that never appears in search.
        """
        for _ in range(_CREATE_POLL_ATTEMPTS):
            status = self.get_record(record_id).status
            if status != "CREATING":
                return
            time.sleep(_CREATE_POLL_INTERVAL_SECONDS)

    def update_record(
        self, record_id: str, req: UpdateRecordRequest
    ) -> RegistryRecordDetail:
        """
        Edit a record's name, description or descriptor content.

        Editing an APPROVED record creates a new DRAFT revision; the approved
        revision stays in search until the new one is approved. The caller is
        not re-submitted automatically — that is the curator's decision.
        """
        self._require_configured()
        current = self.get_record(record_id)
        if current.status == "DEPRECATED":
            raise ValueError(
                "DEPRECATED is a terminal status in AWS, so this record cannot "
                "be edited. Register a new record instead."
            )

        params: Dict[str, Any] = {
            "registryId": self.registry_id,
            "recordId": record_id,
        }
        if req.name is not None:
            # req.name is the human-facing display name; the dedup `name` key
            # is immutable once created.
            params["displayName"] = {"optionalValue": req.name}
        if req.description is not None:
            params["description"] = {"optionalValue": req.description}
        if req.custom_metadata is not None:
            # PATCH-style like every other field: the whole map replaces the
            # stored one, and {} clears it. The owner is not the client's to
            # set: whatever it sent for that key is dropped and the stored
            # value, when there is one, is carried over.
            metadata = {k: v for k, v in req.custom_metadata.items() if k != OWNER_FIELD}
            stored_owner = (current.custom_metadata or {}).get(OWNER_FIELD)
            if stored_owner:
                metadata[OWNER_FIELD] = stored_owner
            params["customMetadata"] = {"optionalValue": metadata}

        # Every field this endpoint exposes also appears inside the descriptor,
        # which is indexed for search — a rename that left the descriptor alone
        # would keep matching the old name. The merge is over the record's
        # current content, so it cannot drop the runtime binding that makes an
        # agent record chattable.
        #
        # Endpoint-only descriptors (`http`/`agui`, written by auto-detection)
        # have no payload to merge into and are owned by the detector, which
        # refreshes them from the source resource — so they are left alone.
        descriptor_key = next(iter(current.descriptors or {}), None)
        if descriptor_key not in _ENDPOINT_ONLY_KEYS and (
            req.name is not None
            or req.description is not None
            or req.content is not None
            or req.skill_markdown is not None
            or req.skill_source is not None
        ):
            merged = _merge_descriptor_content(current, req)
            params["descriptors"] = _as_updated_descriptors(_build_descriptors(merged))

        self.registry_control.update_registry_record(**params)
        return self.get_record(record_id)

    def trigger_sync(self, record_id: str) -> RegistryRecordDetail:
        """
        Re-fetch a record's definition from its `source.fromUrl`.

        The record goes UPDATING, then back to DRAFT with descriptors rebuilt
        from the endpoint (UPDATE_FAILED with a statusReason when the fetch
        fails). Like any edit this opens a new revision: the approved one stays
        discoverable until the refreshed one is submitted and approved. Only
        meaningful for records that have a source; AWS rejects the rest.
        """
        self._require_configured()
        self.registry_control.update_registry_record(
            registryId=self.registry_id, recordId=record_id, triggerSynchronization=True
        )
        return self.get_record(record_id)

    def update_status(
        self, record_id: str, action: str, reason: Optional[str] = None
    ) -> RegistryRecordSummary:
        self._require_configured()
        if action == "submit":
            self.registry_control.submit_registry_record_for_approval(
                registryId=self.registry_id, recordId=record_id
            )
        else:
            status = _ACTION_TO_STATUS.get(action)
            if status is None:
                raise ValueError(f"Unknown status action: {action}")
            self.registry_control.update_registry_record_status(
                registryId=self.registry_id,
                recordId=record_id,
                status=status,
                statusReason=reason or _ACTION_TO_REASON[action],
            )
        return RegistryRecordSummary(
            **self.get_record(record_id).model_dump(
                include=set(RegistryRecordSummary.model_fields)
            )
        )

    def delete_record(self, record_id: str) -> None:
        self._require_configured()
        self.registry_control.delete_registry_record(
            registryId=self.registry_id, recordId=record_id
        )

    def agent_records(self) -> List[RegistryRecordSummary]:
        """Every record of a type that can bind to a deployed agent."""
        listings = fan_out(
            lambda descriptor_type: self.list_records(descriptor_type=descriptor_type),
            [DESCRIPTOR_A2A, DESCRIPTOR_CUSTOM],
        )
        return [record for listing in listings for record in listing]

    def agent_records_by_arn(
        self, include_deprecated: bool = False
    ) -> Dict[str, RegistryRecordSummary]:
        """
        Index agent records by every AgentCore ARN they bind to.

        A harness-backed record carries both its harness ARN and its companion
        runtime ARN, and either one identifies the same deployed agent — so both
        are indexed.

        DEPRECATED records are excluded by default: deprecation is terminal in
        AWS (no edit, no transition back), so such a record can never bind its
        deployment again and must not make it look registered. Pass
        `include_deprecated` to see them, which is how a caller distinguishes
        "never registered" from "registration was retired".
        """
        index: Dict[str, RegistryRecordSummary] = {}
        for record in self.agent_records():
            if not include_deprecated and is_deprecated(record):
                continue
            # `source_arn` is the resource an auto-detected record was cataloged
            # from; indexing it keeps the bulk sync from registering that runtime
            # a second time under this platform's own record shape.
            for arn in (record.harness_arn, record.agent_runtime_arn, record.source_arn):
                if arn:
                    index.setdefault(arn, record)
        return index

    def deprecate_records_for_arns(self, *arns: Optional[str]) -> List[str]:
        """
        Deprecate agent records bound to ARNs that no longer exist.

        Called after deleting a deployment so the registry stops advertising an
        agent that can no longer be invoked. Records are deprecated rather than
        deleted to keep the audit trail, and every failure is logged rather than
        raised — the delete this follows has already succeeded.
        """
        targets = {arn for arn in arns if arn}
        if not targets:
            return []

        try:
            # Already-deprecated records must be visible here so they are skipped
            # rather than re-deprecated, which AWS rejects as a terminal state.
            index = self.agent_records_by_arn(include_deprecated=True)
        except Exception as exc:
            logger.warning("Could not scan registry for orphaned records: %s", exc)
            return []

        deprecated: List[str] = []
        for arn in targets:
            record = index.get(arn)
            # The same record is indexed under both of a harness's ARNs.
            if record is None or record.record_id in deprecated:
                continue
            if (record.status or "").upper() == "DEPRECATED":
                continue
            try:
                self.update_status(
                    record.record_id,
                    "deprecate",
                    reason="The deployment backing this record was deleted.",
                )
                deprecated.append(record.record_id)
            except Exception as exc:
                logger.warning(
                    "Could not deprecate record %s for %s: %s",
                    record.record_id,
                    arn,
                    exc,
                )
        return deprecated

    def gateway_arns(self) -> Dict[str, RegistryRecordSummary]:
        """
        Index MCP records by the gateway ARN they were registered from.

        MCP records are excluded from `agent_records`: a gateway is a tool surface,
        not something chat can be bound to. Deprecated records are dropped for the
        same reason as there — deprecation is terminal, so such a record can never
        advertise its gateway again.
        """
        index: Dict[str, RegistryRecordSummary] = {}
        # Our own gateway records are MCP; auto-detection files a detected
        # gateway as GATEWAY with the ARN in its provenance. Both count as
        # "this gateway is already in the catalog".
        listings = fan_out(
            lambda call: call(),
            [
                lambda: self.list_records(descriptor_type=DESCRIPTOR_MCP),
                lambda: self.list_records(record_type=_GATEWAY_RECORD_TYPE),
            ],
        )
        summaries = [summary for listing in listings for summary in listing]

        def arn_of(summary: RegistryRecordSummary) -> Optional[str]:
            try:
                detail = self.get_record(summary.record_id)
            except Exception as exc:
                logger.warning("Could not read MCP record %s: %s", summary.record_id, exc)
                return None
            return gateway_arn_of(detail.descriptor_content) or (
                detail.source_arn if ":gateway/" in (detail.source_arn or "") else None
            )

        live = [s for s in summaries if not is_deprecated(s)]
        for summary, arn in zip(live, fan_out(arn_of, live)):
            if arn:
                index.setdefault(arn, summary)
        return index

    def list_gateways(self) -> List[GatewaySummary]:
        """
        MCP gateways deployed to AgentCore.

        ListGateways omits both the ARN and the MCP endpoint, so each entry needs a
        GetGateway call; those are fanned out because the listing is user-facing.
        """
        items: List[Dict[str, Any]] = []
        params: Dict[str, Any] = {"maxResults": 100}
        token: Optional[str] = None
        while True:
            if token:
                params["nextToken"] = token
            resp = self.agentcore_control.list_gateways(**params)
            items.extend(resp.get("items", []))
            token = resp.get("nextToken")
            if not token:
                break

        def describe(item: Dict[str, Any]) -> GatewaySummary:
            gateway_id = item.get("gatewayId", "")
            detail: Dict[str, Any] = {}
            try:
                detail = self.agentcore_control.get_gateway(gatewayIdentifier=gateway_id)
            except Exception as exc:
                logger.warning("Could not describe gateway %s: %s", gateway_id, exc)
            return GatewaySummary(
                name=item.get("name") or gateway_id,
                gateway_id=gateway_id,
                gateway_arn=detail.get("gatewayArn", ""),
                # GetGateway reports some gateways without the `/mcp` path.
                gateway_url=(
                    mcp_endpoint_of(detail["gatewayUrl"]) if detail.get("gatewayUrl") else None
                ),
                status=item.get("status") or detail.get("status"),
                description=item.get("description") or detail.get("description"),
                authorizer_type=item.get("authorizerType")
                or detail.get("authorizerType"),
            )

        return [g for g in fan_out(describe, items) if g.gateway_arn]

    def _control_for_region(self, region: str):
        """A control-plane client for `region`, created lazily and cached.

        Runtimes are region-scoped: to offer one deployed elsewhere, that region's
        control plane has to be asked directly. The invoke region is later taken
        from the runtime ARN (`StreamingService._get_agent_client`), so a record
        bound to a cross-region runtime stays reachable.
        """
        if region == self.region:
            return self.agentcore_control
        with self._lock:
            client = self._region_controls.get(region)
            if client is None:
                client = boto3.client(
                    _CONTROL_SERVICE, region_name=region, config=BOTO_CONFIG
                )
                self._region_controls[region] = client
            return client

    def _discovery_regions(self) -> List[str]:
        """This platform's region first, then the extra discovery regions, de-duped."""
        regions = [self.region]
        for region in AGENT_RUNTIME_DISCOVERY_REGIONS:
            if region and region not in regions:
                regions.append(region)
        return regions

    def list_agent_runtimes(self) -> List[AgentRuntimeSummary]:
        """Deployed AgentCore runtimes, offered as choices when registering an agent.

        Scans this platform's region plus AGENT_RUNTIME_DISCOVERY_REGIONS, so a
        runtime deployed in another region can be registered here without being
        moved. A region that cannot be listed (no permission, wrong name) is logged
        and skipped rather than failing the whole listing.
        """
        runtimes: List[AgentRuntimeSummary] = []
        keys: List[tuple[str, str]] = []  # (runtime_id, region) — region drives facts
        for region in self._discovery_regions():
            control = self._control_for_region(region)
            params: Dict[str, Any] = {"maxResults": 100}
            token: Optional[str] = None
            while True:
                if token:
                    params["nextToken"] = token
                try:
                    resp = control.list_agent_runtimes(**params)
                except (BotoCoreError, ClientError) as exc:
                    logger.warning("Could not list runtimes in %s: %s", region, exc)
                    break
                for runtime in resp.get("agentRuntimes", []):
                    keys.append((runtime.get("agentRuntimeId", ""), region))
                    runtimes.append(
                        AgentRuntimeSummary(
                            name=runtime.get("agentRuntimeName", ""),
                            agent_runtime_arn=runtime.get("agentRuntimeArn", ""),
                            status=runtime.get("status"),
                            description=runtime.get("description"),
                        )
                    )
                token = resp.get("nextToken")
                if not token:
                    break

        # ListAgentRuntimes omits the protocol, the image and the environment, and
        # each decides something a caller needs — an MCP runtime serves tools, a
        # harness image means the runtime belongs to a harness, and `MODEL_ID` is the
        # only place a runtime-backed agent's model is written down. So it takes one
        # GetAgentRuntime each, fanned out against the runtime's own region. A failed
        # lookup leaves all three None, treated as "a plain agent, unknown model".
        facts = fan_out(lambda key: self._runtime_facts(key[0], key[1]), keys)
        return [
            summary.model_copy(
                update={
                    "server_protocol": protocol,
                    "container_uri": container_uri,
                    "model_id": model_id,
                }
            )
            for summary, (protocol, container_uri, model_id) in zip(runtimes, facts)
        ]

    def _runtime_facts(
        self, runtime_id: str, region: Optional[str] = None
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """(serverProtocol, containerUri, MODEL_ID) for one runtime.

        `MODEL_ID` comes out of `environmentVariables`, which is where it is set and
        the only place it exists: `InvokeAgentRuntime` accepts no per-request model
        override (see `models/common.py`), so the runtime's environment *is* its
        model for every turn it serves.
        """
        if not runtime_id:
            return None, None, None
        control = self._control_for_region(region) if region else self.agentcore_control
        try:
            resp = control.get_agent_runtime(agentRuntimeId=runtime_id)
        except Exception as exc:
            logger.warning("Could not read runtime %s: %s", runtime_id, exc)
            return None, None, None
        artifact = resp.get("agentRuntimeArtifact") or {}
        container = artifact.get("containerConfiguration") or {}
        environment = resp.get("environmentVariables") or {}
        return (
            (resp.get("protocolConfiguration") or {}).get("serverProtocol"),
            container.get("containerUri"),
            environment.get("MODEL_ID"),
        )
