"""Models for the AgentCore Managed Agent Harness."""
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from models.registry import RegistryRecordSummary
from models.skill import DiscoveredSkill

# HarnessTool.type values this platform composes from the UI.
TOOL_REMOTE_MCP = "remote_mcp"
TOOL_GATEWAY = "agentcore_gateway"
TOOL_BROWSER = "agentcore_browser"
TOOL_CODE_INTERPRETER = "agentcore_code_interpreter"

BUILTIN_TOOL_TYPES = [TOOL_BROWSER, TOOL_CODE_INTERPRETER]

# Output ceiling for a single model call inside the agent loop.
#
# Lives with the models rather than the service because both the compose path
# (CreateHarness/UpdateHarness) and the chat path's per-turn model override
# (InvokeHarness) have to name it: the `model` block is replaced wholesale, so
# an override without the cap would fall back to Bedrock's default.
#
# Left unset, Bedrock Converse supplies its own default — measured at **4096**
# output tokens for `global.anthropic.claude-sonnet-5`, against a model whose own
# ceiling is 128K. Any turn that writes something substantial (a Node script that
# builds a .docx, a long analysis) hits it, the call returns
# `stopReason=max_tokens`, and the harness turns that into a fatal
# `runtimeClientError: Model stopped generating due to maximum token limit`. The
# turn dies rather than the answer merely being cut short.
#
# Not AWS's *top-level* `maxTokens`, which is the total across every model call in
# one invocation — a loop-wide budget that would not lift this ceiling.
#
# Billing is on tokens produced, not on the ceiling, so headroom is free until it
# is used. Kept below the 128K maximum so one runaway reply cannot spend a whole
# turn's budget by itself.
HARNESS_MAX_TOKENS = 64000

# Categories of the AWS Agent Toolkit skill catalog, offered as glob patterns.
AWS_SKILL_CATEGORIES = [
    {"path": "core-skills/*", "label": "Core (EC2, S3, Lambda, DynamoDB, IAM)"},
    {"path": "specialized-skills/analytics-skills/*", "label": "Analytics (Athena, Glue, QuickSight)"},
    {"path": "specialized-skills/operations-skills/*", "label": "Operations (troubleshooting, logs)"},
    {"path": "specialized-skills/storage-skills/*", "label": "Storage (S3, EFS, FSx, Backup)"},
]


class TruncationSettings(BaseModel):
    """Flattened `truncation` block: one strategy, one optional window size."""
    strategy: str = "sliding_window"
    # sliding_window only. AWS's default is 150.
    messages_count: Optional[int] = None

    def to_api(self) -> Dict[str, Any]:
        block: Dict[str, Any] = {"strategy": self.strategy}
        if self.strategy == "sliding_window" and self.messages_count:
            block["config"] = {"slidingWindow": {"messagesCount": self.messages_count}}
        return block

    @classmethod
    def from_api(cls, block: Optional[Dict[str, Any]]) -> Optional["TruncationSettings"]:
        if not block:
            return None
        window = ((block.get("config") or {}).get("slidingWindow") or {})
        return cls(
            strategy=block.get("strategy", "sliding_window"),
            messages_count=window.get("messagesCount"),
        )


class MemorySettings(BaseModel):
    """Read-only view of the memory block, for display on the edit form."""
    arn: Optional[str] = None
    strategies: List[str] = []
    event_expiry_days: Optional[int] = None
    disabled: bool = False

    @classmethod
    def from_api(cls, block: Optional[Dict[str, Any]]) -> Optional["MemorySettings"]:
        if not block:
            return None
        if "disabled" in block:
            return cls(disabled=True)
        managed = block.get("managedMemoryConfiguration") or block.get(
            "agentCoreMemoryConfiguration"
        ) or {}
        return cls(
            arn=managed.get("arn"),
            strategies=list(managed.get("strategies") or []),
            event_expiry_days=managed.get("eventExpiryDuration"),
        )


class ComposeHarnessRequest(BaseModel):
    name: str
    description: Optional[str] = None
    system_prompt: Optional[str] = None
    model_id: Optional[str] = None
    # Registry records to compose in, by record id.
    mcp_record_ids: List[str] = []
    skill_record_ids: List[str] = []
    # Uploaded skill bundles selected by their S3 prefix (`s3://SKILLS_BUCKET/
    # skills/<name>/`). The registry-free skill source: it needs no AGENT_SKILLS
    # record, so it is how the compose UI offers skills when the registry is off.
    skill_bucket_uris: List[str] = []
    # Glob patterns into the AWS Agent Toolkit skill catalog.
    aws_skill_paths: List[str] = []
    builtin_tools: List[str] = []
    gateway_arns: List[str] = []
    allowed_tools: Optional[List[str]] = None
    max_iterations: Optional[int] = None
    timeout_seconds: Optional[int] = None
    # Output ceiling for one model call. Omitted, the service applies
    # HARNESS_MAX_TOKENS — Bedrock's own default is 4096, which ends a long reply
    # as a fatal error rather than a short one.
    max_tokens: Optional[int] = None
    # Context assembly. Omitted, the service pins sliding_window (AWS's own
    # default, but named so it is not inherited silently).
    truncation: Optional[TruncationSettings] = None
    # How long AgentCore Memory keeps this harness's conversation events. Create
    # only: re-sending the memory block on update could recreate the Memory.
    memory_event_expiry_days: Optional[int] = None
    # Team the harness belongs to: picks the execution role and the default
    # allowed-tools list, and tags the harness and its registry record. None =
    # shared (the deployment's single default role).
    team: Optional[str] = None


class UpdateHarnessRequest(BaseModel):
    """What the edit form may change.

    UpdateHarness is a partial update (measured 2026-09-23), so every field is
    optional and an omitted one is left as it is. The two list fields are the
    exception in spirit: `tools`/`skills` are sent whenever any selection field
    is present, because the API replaces the list — the form always sends its
    whole selection, and an empty one must clear the harness's tools.

    Deliberately absent: `name` (immutable in the API), `description` (lives on
    the registry record, whose PATCH resets it to DRAFT), and memory settings.
    """
    system_prompt: Optional[str] = None
    model_id: Optional[str] = None
    mcp_record_ids: Optional[List[str]] = None
    skill_record_ids: Optional[List[str]] = None
    skill_bucket_uris: Optional[List[str]] = None
    aws_skill_paths: Optional[List[str]] = None
    builtin_tools: Optional[List[str]] = None
    gateway_arns: Optional[List[str]] = None
    allowed_tools: Optional[List[str]] = None
    max_iterations: Optional[int] = None
    timeout_seconds: Optional[int] = None
    max_tokens: Optional[int] = None
    truncation: Optional[TruncationSettings] = None

    @property
    def selects_tools(self) -> bool:
        return any(
            v is not None
            for v in (self.mcp_record_ids, self.builtin_tools, self.gateway_arns)
        )

    @property
    def selects_skills(self) -> bool:
        return any(
            v is not None
            for v in (self.skill_record_ids, self.skill_bucket_uris, self.aws_skill_paths)
        )


class HarnessSummary(BaseModel):
    harness_id: str
    harness_arn: str
    harness_name: str
    status: Optional[str] = None
    # The companion runtime AgentCore creates alongside the harness. Recorded for
    # traceability only — it rejects direct InvokeAgentRuntime calls.
    runtime_arn: Optional[str] = None
    failure_reason: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    model_id: Optional[str] = None
    tools: Optional[List[Dict[str, Any]]] = None
    skills: Optional[List[Dict[str, Any]]] = None
    # --- GetHarness-only detail, for the edit form. None on a ListHarnesses row
    # (which omits all of it), so callers can tell "not fetched" from "empty".
    version: Optional[str] = None
    system_prompt: Optional[str] = None
    max_tokens: Optional[int] = None
    max_iterations: Optional[int] = None
    timeout_seconds: Optional[int] = None
    allowed_tools: Optional[List[str]] = None
    truncation: Optional[TruncationSettings] = None
    memory: Optional[MemorySettings] = None
    # Each tool/skill by the source the form re-selects it with. Tool *names* are
    # derived and lossy (sanitised record names), so they are not what to match.
    gateway_arns: Optional[List[str]] = None
    mcp_urls: Optional[List[str]] = None
    builtin_tools: Optional[List[str]] = None
    skill_uris: Optional[List[str]] = None
    aws_skill_paths: Optional[List[str]] = None


class ComposeHarnessResponse(BaseModel):
    harness: HarnessSummary
    # Absent when the harness was created but registering it failed.
    record: Optional[RegistryRecordSummary] = None
    warning: Optional[str] = None


class ComposableRecord(BaseModel):
    """A registry record offered as a harness building block."""
    record_id: str
    name: str
    description: Optional[str] = None
    descriptor_type: str
    status: Optional[str] = None
    # MCP records need an endpoint URL to be composable; skills need SKILL.md.
    composable: bool = True
    reason: Optional[str] = None
    # The source the record resolves to on the harness, so the edit form can map
    # an existing harness's tools/skills back onto catalogue selections.
    url: Optional[str] = None
    gateway_arn: Optional[str] = None
    s3_uri: Optional[str] = None


class HarnessCatalog(BaseModel):
    mcp_servers: List[ComposableRecord]
    skills: List[ComposableRecord]
    # Skill bundles discovered directly in SKILLS_BUCKET, offered so the compose
    # UI has a skill picker even when the registry is off and `skills` is empty.
    bucket_skills: List[DiscoveredSkill] = []
    aws_skill_categories: List[Dict[str, str]]
    builtin_tools: List[str]
    default_model_id: str
    configured: bool
