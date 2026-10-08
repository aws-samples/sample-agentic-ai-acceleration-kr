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
    # "deployed" when this summary was synthesised from a deployed AgentCore
    # resource rather than read from the registry (registry-off fallback). None
    # for real registry records. The UI uses it to hide registry-only actions.
    source: Optional[str] = None


class RegistryRecordDetail(RegistryRecordSummary):
    descriptors: Optional[Dict[str, Any]] = None
    # Descriptor inlineContent is a JSON/markdown string; parsed for display.
    descriptor_content: Optional[Any] = None
    status_reason: Optional[str] = None
    sync_config: Optional[Dict[str, Any]] = None


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


class RecordListResponse(BaseModel):
    records: List[RegistryRecordSummary]
    count: int
