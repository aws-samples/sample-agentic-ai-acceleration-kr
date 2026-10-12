"""Models for AWS Bedrock AgentCore Agent Registry."""
from typing import Optional, Dict, Any, List

from pydantic import BaseModel

# AWS descriptorType enum values.
DESCRIPTOR_MCP = "MCP"
DESCRIPTOR_A2A = "A2A"
DESCRIPTOR_CUSTOM = "CUSTOM"
DESCRIPTOR_AGENT_SKILLS = "AGENT_SKILLS"

DESCRIPTOR_TYPES = [
    DESCRIPTOR_A2A,
    DESCRIPTOR_AGENT_SKILLS,
    DESCRIPTOR_MCP,
    DESCRIPTOR_CUSTOM,
]


class RegistryRecordSummary(BaseModel):
    record_id: str
    name: str
    description: Optional[str] = None
    descriptor_type: Optional[str] = None
    version: Optional[str] = None
    status: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    record_arn: Optional[str] = None
    # Runtime ARN parsed out of the descriptor; present when the record points at
    # a deployed AgentCore runtime, which is what makes it chattable.
    agent_runtime_arn: Optional[str] = None
    # Set instead of agent_runtime_arn for managed-harness agents, which must be
    # invoked through InvokeHarness rather than InvokeAgentRuntime.
    harness_arn: Optional[str] = None
    qualifier: Optional[str] = None
    # True for the record that points at the server's default runtime
    # (AGENT_RUNTIME_ARN): the agent a chat starts with when none is picked, and
    # the one the picker pins to the top. Derived at read time
    # (services/default_agent.py), never stored on the record.
    is_default: bool = False
    # "deployed" when this summary was synthesised from a deployed AgentCore
    # resource rather than read from the registry (registry-off fallback). None
    # for real registry records. The UI uses it to hide registry-only actions.
    source: Optional[str] = None
    # Whether the discovery data plane (search, browse, the registry MCP
    # endpoint) returns this record — i.e. it has an APPROVED revision. Differs
    # from `status` after an edit: AWS opens a DRAFT revision but keeps serving
    # the approved one, so a DRAFT record can still be discoverable and
    # chattable. None when not looked up (deployed fallback, search results).
    discoverable: Optional[bool] = None
    # Typed key/values validated against the registry's custom metadata schema
    # (owner, team, tier, …). Searchable as `customMetadata.<field>`.
    custom_metadata: Optional[Dict[str, Any]] = None
    # False when custom_metadata could not be determined (a failed BatchGet or
    # Get while listing, a deployed harness whose tags could not be read). The
    # team filter hides such records from non-admins instead of treating the
    # missing team as "shared" (services/team_access.py).
    visibility_known: bool = True
    # COMPLIANT / NON_COMPLIANT against the current schema; a later schema change
    # (a field made required) can turn an approved record NON_COMPLIANT without
    # touching its approval.
    compliance_status: Optional[str] = None
    # Provenance: set by organisation-wide auto-detection, which catalogs every
    # AgentCore Runtime and Gateway in the org and links each record back to its
    # source resource. Such records are owned by the detector (no delete while
    # it is on; source-derived fields are refreshed) and must not be registered
    # a second time by this platform's sync.
    auto_detected: bool = False
    source_arn: Optional[str] = None
    source_type: Optional[str] = None

    @property
    def team(self) -> Optional[str]:
        """`custom_metadata["team"]` normalised; None = shared. Not serialised —
        the web reads custom_metadata directly."""
        from services.team_access import team_of  # local: models must not import services at load

        return team_of(self)


class RegistryRecordDetail(RegistryRecordSummary):
    descriptors: Optional[Dict[str, Any]] = None
    # Descriptor inlineContent is a JSON/markdown string; parsed for display.
    descriptor_content: Optional[Any] = None
    status_reason: Optional[str] = None
    # Kept for API compatibility; the new schema moved synchronization into the
    # descriptor, which `sync_source` summarises.
    sync_config: Optional[Dict[str, Any]] = None
    # `{"url": …, "credential": "none" | "iam" | "oauth", "role_arn"?: …}` when
    # the primary descriptor carries a `source.fromUrl`, i.e. AWS can re-fetch
    # the server/agent-card definition from that endpoint on demand.
    sync_source: Optional[Dict[str, Any]] = None
    # Raw provenance entries as AWS returns them (relation, sourceId, sourceType,
    # sourceDetails), for the detail panel.
    provenance: Optional[List[Dict[str, Any]]] = None
    # "approved" when this detail is the discoverable approved revision served
    # in place of a newer non-approved latest revision (see
    # RegistryService.chattable_record). None for the latest revision.
    revision: Optional[str] = None


class CreateRecordRequest(BaseModel):
    name: str
    description: Optional[str] = None
    descriptor_type: str
    version: Optional[str] = None
    # For A2A agent records: the deployed runtime to bind chat to.
    agent_runtime_arn: Optional[str] = None
    # For A2A records backed by a managed harness; takes precedence over
    # agent_runtime_arn when binding chat.
    harness_arn: Optional[str] = None
    qualifier: Optional[str] = None
    # For MCP records: the endpoint a harness connects to as a remote_mcp tool.
    remote_url: Optional[str] = None
    # For MCP records backed by an AgentCore gateway. The composer attaches these
    # as a native agentcore_gateway tool, which handles the gateway's SigV4 auth.
    gateway_arn: Optional[str] = None
    # Free-form descriptor payload for MCP / CUSTOM / AGENT_SKILLS records.
    content: Optional[Any] = None
    skill_markdown: Optional[str] = None
    # For AGENT_SKILLS records: where the published bundle lives, as a
    # `models.skill.SkillSource` dump. The registry itself can only hold the
    # markdown, so this pointer is what lets the harness fetch the whole
    # directory. Set by the skill upload route, never by hand.
    skill_source: Optional[Dict[str, Any]] = None
    # Records are created as DRAFT; submitting is what makes them searchable and
    # (on an auto-approval registry) immediately APPROVED.
    submit_for_approval: bool = True
    # Values for the registry's custom metadata schema (owner, team, tier…).
    # Omitted or empty = not sent: a registry without a schema rejects even {}.
    custom_metadata: Optional[Dict[str, Any]] = None
    # Synchronise from an endpoint: an MCP server URL (MCP records) or an agent
    # card URL (A2A records). AWS fetches the definition and populates the
    # descriptor — tools included — and *overwrites the record's name,
    # description and version with what the source advertises* (measured live
    # 2026-10-10), which is why the bulk sync never sets this on its own.
    sync_url: Optional[str] = None
    # IAM role AWS assumes to sign the fetch (SigV4, service `agent-registry`)
    # for servers on AgentCore Runtime or Gateway. None = unauthenticated fetch.
    sync_role_arn: Optional[str] = None
    # Resource tags on the record (e.g. the Platform cost-allocation tag).
    tags: Optional[Dict[str, str]] = None


class UpdateRecordRequest(BaseModel):
    """
    A partial edit to a record. Every field is optional: an unset field is left
    untouched rather than cleared, which is what the API's optionalValue wrapper
    is there to express.
    """
    name: Optional[str] = None
    description: Optional[str] = None
    # Free-form descriptor payload, merged over the record's current content.
    content: Optional[Any] = None
    skill_markdown: Optional[str] = None
    # Replaces the record's bundle pointer; see CreateRecordRequest.skill_source.
    skill_source: Optional[Dict[str, Any]] = None
    # Full replacement of the record's custom metadata map (AWS semantics: the
    # map is replaced, not merged; {} clears it). None = untouched.
    custom_metadata: Optional[Dict[str, Any]] = None


class UpdateStatusRequest(BaseModel):
    action: str  # submit | approve | reject | deprecate
    # AWS requires a statusReason on every transition; omitted, the server
    # sends a per-action default.
    reason: Optional[str] = None


class AgentRuntimeSummary(BaseModel):
    """A runtime deployed to AgentCore, offered as a choice when registering an agent."""
    name: str
    agent_runtime_arn: str
    status: Optional[str] = None
    description: Optional[str] = None
    # "HTTP" for an agent, "MCP" for a tool server. ListAgentRuntimes omits this, so
    # it is filled per-runtime by GetAgentRuntime; None means "not looked up".
    server_protocol: Optional[str] = None
    # The runtime's container image, from the same GetAgentRuntime call. It is the
    # only harness marker a runtime carries: AWS runs a harness's companion runtime
    # from a published image, and nothing on the runtime itself names its harness.
    container_uri: Optional[str] = None
    # The model the runtime runs, read from its `MODEL_ID` environment variable in
    # the same GetAgentRuntime call. There is no per-request model override —
    # `StreamConfig` says so — so the runtime's environment *is* the model, and this
    # is the only place a runtime-backed agent's model can be read from. It is what
    # lets the insights page price a runtime-backed agent's tokens; without it,
    # `model_map` could only answer for harness-backed records, and on this account
    # the busiest agent is runtime-backed.
    model_id: Optional[str] = None


class GatewaySummary(BaseModel):
    """An MCP gateway deployed to AgentCore, offered as a composable tool surface."""
    name: str
    gateway_id: str
    gateway_arn: str
    # The MCP endpoint. ListGateways omits it, so it comes from GetGateway.
    gateway_url: Optional[str] = None
    status: Optional[str] = None
    description: Optional[str] = None
    # AWS_IAM or CUSTOM_JWT. Decides whether a harness can call it with SigV4.
    authorizer_type: Optional[str] = None


# Kinds of deployed AgentCore target the registry sync knows how to register.
TARGET_RUNTIME = "runtime"
TARGET_HARNESS = "harness"
# A gateway is a tool surface rather than an agent, so it registers as an MCP
# record instead of an A2A one — that is also what puts it in the harness catalog.
TARGET_GATEWAY = "gateway"


class DeployedTarget(BaseModel):
    """A deployed agent or tool surface, paired with its registry record (if any)."""
    kind: str  # runtime | harness | gateway
    name: str
    # Invocation target: the harness ARN for harnesses, the runtime ARN otherwise.
    arn: str
    status: Optional[str] = None
    description: Optional[str] = None
    registered: bool = False
    # True when the only record for this deployment was deprecated. Deprecation is
    # terminal in AWS, so re-registering creates a new record — which is why this
    # is distinct from `registered=False` and needs an explicit opt-in.
    retired: bool = False
    record_id: Optional[str] = None
    record_name: Optional[str] = None
    record_status: Optional[str] = None
    # Harness targets also carry their companion runtime, recorded for traceability.
    runtime_arn: Optional[str] = None
    # Gateway targets carry their MCP endpoint, which is what the record binds to.
    gateway_url: Optional[str] = None
    # Set when the target cannot be registered as-is (not READY, harness-managed).
    reason: Optional[str] = None


class SyncFailure(BaseModel):
    name: str
    arn: Optional[str] = None
    error: str


class SyncAgentsRequest(BaseModel):
    # Register only these targets, matched by ARN or deployed name. Empty = all.
    targets: List[str] = []
    submit_for_approval: bool = True


class SyncAgentsResponse(BaseModel):
    registered: List[RegistryRecordSummary] = []
    # Targets left alone: already registered, or blocked by `reason`.
    skipped: List[DeployedTarget] = []
    failed: List[SyncFailure] = []


class RegistryInfo(BaseModel):
    registry_id: str
    name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    auto_approval: Optional[bool] = None
    registry_arn: Optional[str] = None
    # The registry's own MCP endpoint: search/list/batch-get exposed as MCP
    # tools, callable from any MCP client (SigV4 via mcp-proxy-for-aws) and
    # attached to the platform gateway as a target.
    mcp_endpoint: Optional[str] = None
    # Parsed JSON Schema per record type, with the default schema under
    # "DEFAULT". None when the registry has no custom metadata schema.
    custom_metadata_schema: Optional[Dict[str, Any]] = None
    # {"enabled": bool, "status": "ACTIVE" | "INACTIVE"} — organisation-wide
    # auto-detection of Runtimes and Gateways. None when never configured.
    auto_detection: Optional[Dict[str, Any]] = None
    # Customer managed key encrypting the registry, when one was set at creation.
    kms_key_arn: Optional[str] = None
    # Role the server may hand to AWS for record synchronisation
    # (REGISTRY_SYNC_ROLE_ARN); None = only unauthenticated sync is offered.
    sync_role_arn: Optional[str] = None


class RecordListResponse(BaseModel):
    records: List[RegistryRecordSummary]
    count: int
