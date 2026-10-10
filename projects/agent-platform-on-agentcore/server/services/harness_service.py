"""
AWS Bedrock AgentCore Managed Agent Harness service.

Composes registry records (MCP servers, agent skills) plus built-in AgentCore
tools into a harness via CreateHarness, then registers the result back into the
Agent Registry as an A2A record so it shows up alongside hand-deployed agents.
"""
import logging
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import boto3
from botocore.exceptions import ClientError

from core.auth import AuthUser
from core.config import (
    AWS_REGION,
    BEDROCK_MODEL_ID,
    HARNESS_EXECUTION_ROLE_ARN,
    MODEL_COST_AIP_ENABLED,
    PLATFORM,
    SKILLS_BUCKET,
)
from models.harness import (
    AWS_SKILL_CATEGORIES,
    BUILTIN_TOOL_TYPES,
    HARNESS_MAX_TOKENS,
    ComposableRecord,
    ComposeHarnessRequest,
    ComposeHarnessResponse,
    HarnessCatalog,
    HarnessSummary,
    MemorySettings,
    TOOL_GATEWAY,
    TOOL_REMOTE_MCP,
    TruncationSettings,
    UpdateHarnessRequest,
)
from models.registry import (
    CreateRecordRequest,
    RegistryRecordSummary,
    DESCRIPTOR_AGENT_SKILLS,
    DESCRIPTOR_A2A,
    DESCRIPTOR_MCP,
)
from services.registry_service import (
    BOTO_CONFIG,
    RegistryService,
    fan_out,
    gateway_arn_of,
    is_registry_unavailable,
    registry_enabled,
    skill_source_of,
    _iso,
    _slug,
    owner_identity,
)
from services.skill_bundle_service import DiscoveredSkill, SkillBundleService
from services.team_access import can_see, normalize_team, team_of, visibility_known

logger = logging.getLogger(__name__)

_CONTROL_SERVICE = "bedrock-agentcore-control"

# CreateHarness is asynchronous; harnesses pass through CREATING first.
_CREATE_POLL_ATTEMPTS = 60
_CREATE_POLL_INTERVAL_SECONDS = 2.0

_TEAM_TAG_CACHE_SECONDS = 600.0

# HarnessName is far stricter than registry record names: no hyphens or dots.
_HARNESS_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,39}$")

# Harness tool names allow only alphanumerics, underscore and hyphen.
_TOOL_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]")


class HarnessNotConfigured(Exception):
    """Raised when the harness execution role is not set on the server."""


def sanitize_harness_name(name: str) -> str:
    """Coerce a user-supplied name into the HarnessName pattern."""
    cleaned = re.sub(r"[^a-zA-Z0-9_]", "_", (name or "").strip())
    cleaned = cleaned.lstrip("_0123456789")
    cleaned = cleaned[:40]
    if not cleaned:
        raise ValueError(
            "Harness name must contain a letter followed by letters, digits or underscores."
        )
    return cleaned


def _tool_name(name: str) -> str:
    """MCP server names are `namespace/name`; flatten to a valid tool name."""
    tail = name.rsplit("/", 1)[-1]
    cleaned = _TOOL_NAME_RE.sub("-", tail).strip("-")
    return cleaned[:64] or "tool"


def _mcp_url(descriptor_content: Any) -> Optional[str]:
    """Find the endpoint URL in an MCP record's descriptor payload."""
    if not isinstance(descriptor_content, dict):
        return None
    server = descriptor_content.get("server")
    if not isinstance(server, dict):
        return None

    remotes = server.get("remotes")
    if isinstance(remotes, list):
        for remote in remotes:
            if isinstance(remote, dict) and isinstance(remote.get("url"), str):
                return remote["url"]

    for key in ("url", "endpoint"):
        value = server.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
    return None


def _skill_markdown(descriptor_content: Any) -> Optional[str]:
    if not isinstance(descriptor_content, dict):
        return None
    markdown = descriptor_content.get("skillMd")
    return markdown if isinstance(markdown, str) and markdown.strip() else None


def _tool_sources(tools: Optional[List[Dict[str, Any]]]) -> Dict[str, Optional[List[str]]]:
    """Each tool by the source the edit form re-selects it with.

    None across the board when `tools` is absent (a ListHarnesses row), so the
    caller can tell "not fetched" from "fetched, none".
    """
    if tools is None:
        return {"gateway_arns": None, "mcp_urls": None, "builtin_tools": None}
    gateway_arns: List[str] = []
    mcp_urls: List[str] = []
    builtin: List[str] = []
    for tool in tools:
        kind = tool.get("type")
        config = tool.get("config") or {}
        if kind == TOOL_GATEWAY:
            arn = (config.get("agentCoreGateway") or {}).get("gatewayArn")
            if arn:
                gateway_arns.append(arn)
        elif kind == TOOL_REMOTE_MCP:
            url = (config.get("remoteMcp") or {}).get("url")
            if url:
                mcp_urls.append(url)
        elif kind in BUILTIN_TOOL_TYPES:
            builtin.append(kind)
    return {"gateway_arns": gateway_arns, "mcp_urls": mcp_urls, "builtin_tools": builtin}


def _skill_sources(skills: Optional[List[Dict[str, Any]]]) -> Dict[str, Optional[List[str]]]:
    if skills is None:
        return {"skill_uris": None, "aws_skill_paths": None}
    uris: List[str] = []
    aws_paths: List[str] = []
    for skill in skills:
        uri = (skill.get("s3") or {}).get("uri")
        if uri:
            uris.append(uri)
        aws_paths.extend((skill.get("awsSkills") or {}).get("paths") or [])
    return {"skill_uris": uris, "aws_skill_paths": aws_paths}


def _to_harness_summary(harness: Dict[str, Any]) -> HarnessSummary:
    environment = harness.get("environment") or {}
    runtime_env = environment.get("agentCoreRuntimeEnvironment") or {}
    model = harness.get("model") or {}
    model_config = model.get("bedrockModelConfig") or {}
    prompt_blocks = harness.get("systemPrompt")
    system_prompt = (
        "\n".join(b.get("text", "") for b in prompt_blocks if isinstance(b, dict))
        if prompt_blocks is not None
        else None
    )
    return HarnessSummary(
        harness_id=harness.get("harnessId", ""),
        harness_arn=harness.get("arn", ""),
        harness_name=harness.get("harnessName", ""),
        status=harness.get("status"),
        runtime_arn=runtime_env.get("agentRuntimeArn"),
        failure_reason=harness.get("failureReason"),
        created_at=_iso(harness.get("createdAt")),
        updated_at=_iso(harness.get("updatedAt")),
        model_id=model_config.get("modelId"),
        tools=harness.get("tools"),
        skills=harness.get("skills"),
        version=harness.get("harnessVersion"),
        system_prompt=system_prompt,
        max_tokens=model_config.get("maxTokens"),
        max_iterations=harness.get("maxIterations"),
        timeout_seconds=harness.get("timeoutSeconds"),
        allowed_tools=harness.get("allowedTools"),
        truncation=TruncationSettings.from_api(harness.get("truncation")),
        memory=MemorySettings.from_api(harness.get("memory")),
        **_tool_sources(harness.get("tools")),
        **_skill_sources(harness.get("skills")),
    )


class HarnessService:
    def __init__(
        self,
        registry: Optional[RegistryService] = None,
        region: Optional[str] = None,
        execution_role_arn: Optional[str] = None,
        skills_bucket: Optional[str] = None,
        bedrock=None,
        teams=None,
    ):
        self.teams = teams
        self.registry = registry or RegistryService()
        self.region = region or AWS_REGION
        self.execution_role_arn = (
            execution_role_arn
            if execution_role_arn is not None
            else HARNESS_EXECUTION_ROLE_ARN
        )
        self.skills_bucket = (
            skills_bucket if skills_bucket is not None else SKILLS_BUCKET
        )
        self._control = None
        self._s3 = None
        self._bedrock = bedrock
        self._lock = threading.Lock()
        # Called with the companion runtime ARN once a composed harness is READY.
        # Wired by `core.dependencies` to `ObservabilityService.ensure_usage_logs`
        # so a new harness delivers per-session USAGE_LOGS from its first turn.
        # Best-effort: any exception is logged, never raised into composition.
        self.on_runtime_ready = None
        # Gateway target names change only when infra is redeployed, but they are
        # read on every gateway-record Usage open. Cache per gateway ARN so the
        # ListGatewayTargets call is paid once per TTL rather than per view. This
        # instance is process-wide (usage_service holds the one HarnessService),
        # so the cache is shared across requests.
        self._target_cache: Dict[str, Tuple[float, List[str]]] = {}
        # gateway ARN -> (read at, {target name: MCP server endpoint}). Same
        # lifetime and reason as `_target_cache`; read when a remote-MCP record's
        # Usage opens, to find the gateway target that fronts its URL.
        self._endpoint_cache: Dict[str, Tuple[float, Dict[str, str]]] = {}
        # harness ARN -> (read at, Team tag or None). Only successful reads are
        # cached: a failure must be retried, not remembered as "shared".
        self._team_tag_cache: Dict[str, Tuple[float, Optional[str]]] = {}
        self._target_cache_seconds = 300.0

    @property
    def control(self):
        if self._control is None:
            with self._lock:
                if self._control is None:
                    self._control = boto3.client(
                        _CONTROL_SERVICE, region_name=self.region, config=BOTO_CONFIG
                    )
        return self._control

    @property
    def bedrock(self):
        if self._bedrock is None:
            with self._lock:
                if self._bedrock is None:
                    self._bedrock = boto3.client(
                        "bedrock", region_name=self.region, config=BOTO_CONFIG
                    )
        return self._bedrock

    @property
    def s3(self):
        if self._s3 is None:
            with self._lock:
                if self._s3 is None:
                    self._s3 = boto3.client("s3", region_name=self.region)
        return self._s3

    def _model_id_for(self, model_id: str, agent_name: str) -> str:
        """The modelId to give bedrockModelConfig.

        With AIP enabled, route through a per-agent application inference profile
        so model spend carries this agent's AgentName tag in Cost Explorer. Any
        failure falls back to the base model id — a cost-attribution nicety must
        never block harness creation. See spec section G.
        """
        if not MODEL_COST_AIP_ENABLED:
            return model_id
        try:
            profile_name = f"{PLATFORM}-{agent_name}"[:64]
            try:
                resp = self.bedrock.create_inference_profile(
                    inferenceProfileName=profile_name,
                    description=f"Per-agent cost attribution for {agent_name}",
                    modelSource={"copyFrom": model_id},
                    tags=[
                        {"key": "AgentName", "value": agent_name},
                        {"key": "Platform", "value": PLATFORM},
                    ],
                )
                return resp["inferenceProfileArn"]
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") != "ConflictException":
                    raise
                # Already created for this agent on an earlier harness. Reuse it by
                # name — GetInferenceProfile takes an id/ARN, not a name, so the
                # existing one is found by listing APPLICATION profiles and matching
                # the name. Not found (or a list failure) falls through to base id.
                logger.debug("AIP %s exists; reusing", profile_name)
                return self._find_aip_arn_by_name(profile_name) or model_id
        except Exception:
            logger.warning("AIP resolution failed; using base model id", exc_info=True)
            return model_id

    def _find_aip_arn_by_name(self, profile_name: str) -> Optional[str]:
        """ARN of an existing APPLICATION inference profile with this name, or None.

        Paginates ListInferenceProfiles: an account with many profiles returns them
        a page at a time, and the one we want may not be on the first page.
        """
        token = None
        while True:
            params: Dict[str, Any] = {"typeEquals": "APPLICATION", "maxResults": 1000}
            if token:
                params["nextToken"] = token
            resp = self.bedrock.list_inference_profiles(**params)
            for summary in resp.get("inferenceProfileSummaries", []):
                if summary.get("inferenceProfileName") == profile_name:
                    return summary.get("inferenceProfileArn")
            token = resp.get("nextToken")
            if not token:
                return None

    def _require_configured(self) -> None:
        if not self.execution_role_arn:
            raise HarnessNotConfigured(
                "HARNESS_EXECUTION_ROLE_ARN is not configured on the server. "
                "Deploy infra/modules/iam and set the value from "
                "`terraform output harness_execution_role_arn`."
            )

    # --- composition inputs -------------------------------------------------

    def catalog(self) -> HarnessCatalog:
        """Registry records that can be composed, with per-record eligibility.

        Registry off or unavailable: the composable MCP/skill *records* cannot be
        read, so the catalog is built from what needs no registry — deployed
        gateways (attached by ARN), bucket skills, the built-in tools and the AWS
        skill catalogue. Harness creation from those alone still works.
        """
        types = (DESCRIPTOR_MCP, DESCRIPTOR_AGENT_SKILLS)
        composables: List[ComposableRecord] = []
        if registry_enabled():
            try:
                listings = fan_out(
                    lambda descriptor_type: self.registry.list_records(
                        descriptor_type=descriptor_type
                    ),
                    list(types),
                )
                composables = fan_out(
                    lambda pair: self._composable(pair[0], pair[1]),
                    [
                        (descriptor_type, summary)
                        for descriptor_type, summaries in zip(types, listings)
                        for summary in summaries
                    ],
                )
            except Exception as exc:
                if not is_registry_unavailable(exc):
                    raise
                logger.warning("Registry catalog unavailable; using fallback: %s", exc)
                composables = []

        return HarnessCatalog(
            mcp_servers=[c for c in composables if c.descriptor_type == DESCRIPTOR_MCP],
            skills=[
                c for c in composables if c.descriptor_type == DESCRIPTOR_AGENT_SKILLS
            ],
            # Bucket-discovered skills need no registry, so they populate in both
            # modes — the registry-off compose UI's only skill source.
            bucket_skills=self._list_bucket_skills(),
            aws_skill_categories=AWS_SKILL_CATEGORIES,
            builtin_tools=BUILTIN_TOOL_TYPES,
            default_model_id=BEDROCK_MODEL_ID,
            configured=bool(self.execution_role_arn),
        )

    def _list_bucket_skills(self) -> List[DiscoveredSkill]:
        """Uploaded skill bundles under `SKILLS_BUCKET/skills/`, for the catalog.

        Fail-open: an unconfigured or unlistable bucket yields no skills rather
        than an error, so the catalog still builds (`list_skills` swallows its own
        S3 failures).
        """
        return SkillBundleService(bucket=self.skills_bucket).list_skills()

    def _composable(
        self, descriptor_type: str, summary: RegistryRecordSummary
    ) -> ComposableRecord:
        """Whether a record carries the descriptor content composition needs."""
        reason: Optional[str] = None
        # The source this record becomes on a harness. The edit form maps an
        # existing harness's tools/skills back onto catalogue rows by these —
        # tool names are sanitised record names and not reliably reversible.
        url: Optional[str] = None
        gateway_arn: Optional[str] = None
        s3_uri: Optional[str] = None
        try:
            detail = self.registry.get_record(summary.record_id)
            if descriptor_type == DESCRIPTOR_MCP:
                # A gateway-backed record needs no URL: it attaches by ARN, and the
                # harness resolves the endpoint itself.
                gateway_arn = gateway_arn_of(detail.descriptor_content)
                url = _mcp_url(detail.descriptor_content)
                if not gateway_arn and not url:
                    reason = "No endpoint URL registered for this MCP server."
            else:
                source = skill_source_of(detail.descriptor_content)
                if source:
                    s3_uri = source["uri"]
                elif _skill_markdown(detail.descriptor_content):
                    # Inline markdown is published under this prefix at compose
                    # time (`_resolve_skills`), so that is where the harness
                    # points afterwards.
                    if self.skills_bucket:
                        s3_uri = f"s3://{self.skills_bucket}/skills/{_slug(detail.name)}/"
                else:
                    # Either an uploaded bundle in S3 or inline markdown is enough;
                    # a record with neither has nothing for the harness to fetch.
                    reason = "Record has no uploaded bundle and no SKILL.md content."
        except Exception as exc:
            logger.warning("Could not inspect record %s: %s", summary.record_id, exc)
            reason = "Could not read descriptor."

        return ComposableRecord(
            record_id=summary.record_id,
            name=summary.name,
            description=summary.description,
            descriptor_type=descriptor_type,
            status=summary.status,
            composable=reason is None,
            reason=reason,
            url=url,
            gateway_arn=gateway_arn,
            s3_uri=s3_uri,
        )

    def _resolve_mcp_tools(self, record_ids: List[str]) -> List[Dict[str, Any]]:
        tools: List[Dict[str, Any]] = []
        for detail in fan_out(self.registry.get_record, record_ids):
            gateway_arn = gateway_arn_of(detail.descriptor_content)
            if gateway_arn:
                # An AgentCore gateway speaks streamable-HTTP MCP like any other
                # server, but it demands SigV4 (or a JWT) that `remote_mcp` cannot
                # supply — so it attaches as the native gateway tool, which signs
                # with the harness execution role.
                tools.append(self._gateway_tool(gateway_arn))
                continue

            url = _mcp_url(detail.descriptor_content)
            if not url:
                raise ValueError(
                    f"MCP record '{detail.name}' has no endpoint URL and cannot be "
                    "composed. Re-register it with a remote URL."
                )
            tools.append(
                {
                    "type": TOOL_REMOTE_MCP,
                    "name": _tool_name(detail.name),
                    "config": {"remoteMcp": {"url": url}},
                }
            )
        return tools

    @staticmethod
    def _gateway_tool(gateway_arn: str) -> Dict[str, Any]:
        return {
            "type": TOOL_GATEWAY,
            "name": _tool_name(gateway_arn.rsplit("/", 1)[-1]),
            "config": {
                "agentCoreGateway": {
                    "gatewayArn": gateway_arn,
                    "outboundAuth": {"awsIam": {}},
                }
            },
        }

    def _resolve_skills(
        self, record_ids: List[str], aws_skill_paths: List[str]
    ) -> List[Dict[str, Any]]:
        """
        Turn skill records into harness skill sources.

        A record created from a bundle upload already names the S3 prefix holding
        its whole directory, so it is attached as-is — writing anything here would
        overwrite the uploaded `references/` and `scripts/` with a lone SKILL.md.
        Records whose markdown was typed in before uploads existed have no prefix,
        so those still get published one file at a time.
        """
        skills: List[Dict[str, Any]] = []

        if record_ids and not self.skills_bucket:
            raise HarnessNotConfigured(
                "SKILLS_BUCKET is not configured on the server, so registry skills "
                "cannot be published for the harness to fetch."
            )

        for detail in fan_out(self.registry.get_record, record_ids):
            source = skill_source_of(detail.descriptor_content)
            if source:
                skills.append({"s3": {"uri": source["uri"]}})
                continue

            markdown = _skill_markdown(detail.descriptor_content)
            if not markdown:
                raise ValueError(
                    f"Skill record '{detail.name}' has no uploaded bundle and no "
                    "SKILL.md content."
                )
            # The harness API has no inline skill source, so an inline record's
            # markdown is published to S3 and referenced by prefix.
            prefix = f"skills/{_slug(detail.name)}/"
            self.s3.put_object(
                Bucket=self.skills_bucket,
                Key=f"{prefix}SKILL.md",
                Body=markdown.encode("utf-8"),
                ContentType="text/markdown",
            )
            skills.append({"s3": {"uri": f"s3://{self.skills_bucket}/{prefix}"}})

        if aws_skill_paths:
            skills.append({"awsSkills": {"paths": aws_skill_paths}})

        return skills

    def _resolve_bucket_skills(self, uris: List[str]) -> List[Dict[str, Any]]:
        """Attach uploaded skill bundles chosen by their S3 prefix.

        The registry-free path: a bundle already lives under the skills bucket,
        so it is referenced as-is — no record to read, nothing to publish. Each
        URI is checked to be inside this server's own skills bucket, so a client
        cannot point the harness at an arbitrary S3 location.
        """
        if not uris:
            return []
        if not self.skills_bucket:
            raise HarnessNotConfigured(
                "SKILLS_BUCKET is not configured on the server, so uploaded skills "
                "cannot be attached to the harness."
            )
        allowed = f"s3://{self.skills_bucket}/skills/"
        skills: List[Dict[str, Any]] = []
        for uri in uris:
            if not uri.startswith(allowed):
                raise ValueError(f"Skill bundle is not in the skills bucket: {uri}")
            skills.append({"s3": {"uri": uri}})
        return skills

    def _compose_tools(
        self,
        mcp_record_ids: List[str],
        builtin_tools: List[str],
        gateway_arns: List[str],
    ) -> List[Dict[str, Any]]:
        """The harness `tools` list for a selection. Shared by create and update
        so an edit resolves records exactly the way the original compose did."""
        tools = self._resolve_mcp_tools(mcp_record_ids)
        for tool_type in builtin_tools:
            if tool_type not in BUILTIN_TOOL_TYPES:
                raise ValueError(f"Unsupported built-in tool: {tool_type}")
            tools.append({"type": tool_type, "name": tool_type})
        # Gateways attached by ARN rather than through a registry record. A record
        # resolves to the same tool, so skip any that _resolve_mcp_tools already added.
        attached = {
            (t.get("config") or {}).get("agentCoreGateway", {}).get("gatewayArn")
            for t in tools
        }
        for gateway_arn in gateway_arns:
            if gateway_arn not in attached:
                tools.append(self._gateway_tool(gateway_arn))
                attached.add(gateway_arn)
        return tools

    def _compose_skills(
        self,
        skill_record_ids: List[str],
        aws_skill_paths: List[str],
        skill_bucket_uris: List[str],
    ) -> List[Dict[str, Any]]:
        skills = self._resolve_skills(skill_record_ids, aws_skill_paths)
        skills += self._resolve_bucket_skills(skill_bucket_uris)
        # A bundle can be selected twice — as the registry record created from
        # its upload and as the bucket prefix itself — and both resolve to the
        # same S3 source. The harness should carry it once.
        seen: set = set()
        unique: List[Dict[str, Any]] = []
        for skill in skills:
            uri = (skill.get("s3") or {}).get("uri")
            key = uri.rstrip("/") if uri else None
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            unique.append(skill)
        return unique

    # --- lifecycle ---------------------------------------------------------

    def resolve_team(self, req: ComposeHarnessRequest, caller: Optional[AuthUser]) -> Optional[str]:
        """Which team this harness is for. Raises ValueError (→ 400) when the
        request names a team the caller is not in, or when a caller in several
        teams did not say which one."""
        teams = self.teams
        wanted = normalize_team(req.team)
        if teams is None or not teams.enabled:
            if wanted:
                raise ValueError("This deployment has no teams")
            return None
        known = {t.name for t in teams.list()}
        if wanted and wanted not in known:
            raise ValueError(f"Unknown team: {wanted}")
        if caller is None or caller.is_admin:
            return wanted
        mine = [t for t in caller.teams if t in known]
        if wanted:
            if wanted not in mine:
                raise ValueError(f"Not a member of team {wanted}")
            return wanted
        if len(mine) > 1:
            raise ValueError("Choose a team: " + ", ".join(mine))
        return mine[0] if mine else None

    def create_harness(self, req: ComposeHarnessRequest) -> HarnessSummary:
        self._require_configured()

        harness_name = sanitize_harness_name(req.name)
        if not _HARNESS_NAME_RE.match(harness_name):
            raise ValueError(f"Invalid harness name: {harness_name}")

        tools = self._compose_tools(
            req.mcp_record_ids, req.builtin_tools, req.gateway_arns
        )
        skills = self._compose_skills(
            req.skill_record_ids, req.aws_skill_paths, req.skill_bucket_uris
        )

        team = normalize_team(req.team)
        execution_role_arn = self.execution_role_arn
        allowed_tools = list(req.allowed_tools or [])
        tags = {"Platform": PLATFORM, "AgentName": harness_name}
        if team:
            team_cfg = self.teams.get(team) if self.teams is not None else None
            if team_cfg is None or not team_cfg.execution_role_arn:
                raise ValueError(f"Team {team} has no execution role")
            execution_role_arn = team_cfg.execution_role_arn
            requested_model = req.model_id or BEDROCK_MODEL_ID
            if team_cfg.allowed_models and requested_model not in team_cfg.allowed_models:
                raise ValueError(f"Model {requested_model} is not allowed for team {team}")
            if not allowed_tools and team_cfg.allowed_tools:
                allowed_tools = list(team_cfg.allowed_tools)
            tags["Team"] = team

        params: Dict[str, Any] = {
            "harnessName": harness_name,
            "executionRoleArn": execution_role_arn,
            # Propagate to the managed Runtime, endpoint and Memory this harness
            # creates, so activating them as cost allocation tags splits Cost
            # Explorer per agent (`infra/modules/cost_allocation_tags`).
            #
            # Here rather than after the fact because cost data is not
            # retroactive: `TagResource` does accept an existing harness and its
            # companion runtime — measured 2026-08-19, contrary to what this
            # comment used to claim — but the hours billed before the tag landed
            # stay in the untagged bucket forever.
            "tags": tags,
            "model": {
                "bedrockModelConfig": {
                    "modelId": self._model_id_for(
                        req.model_id or BEDROCK_MODEL_ID, harness_name
                    ),
                    # Per model call, not per invocation. Unset, Bedrock's own
                    # default (4096) ends a long reply as a fatal error — see
                    # HARNESS_MAX_TOKENS.
                    "maxTokens": req.max_tokens or HARNESS_MAX_TOKENS,
                }
            },
            # The harness persists conversation state in AgentCore Memory and
            # reloads it per session, so the client sends only the new message.
            # Expiry is pinned because the default is 30 days: conversations we
            # intend to keep would vanish without warning.
            # SUMMARIZATION only. Its default namespace is session-scoped
            # (/strategy/{id}/actor/{actorId}/session/{sessionId}/), so recall
            # never crosses chats. SEMANTIC is left off on purpose: its namespace
            # is actor-scoped, not session-scoped, so facts extracted in one chat
            # inject into the same user's other chats — the runtime path made the
            # same call (docs runtime-longterm-memory-design, "SEMANTIC 등은 YAGNI").
            "memory": {
                "managedMemoryConfiguration": {
                    "strategies": ["SUMMARIZATION"],
                    "eventExpiryDuration": req.memory_event_expiry_days or 365,
                }
            },
            # Context assembly now belongs to the harness, so name its truncation
            # strategy here rather than inheriting the default silently.
            "truncation": (req.truncation or TruncationSettings()).to_api(),
        }
        if req.system_prompt:
            params["systemPrompt"] = [{"text": req.system_prompt}]
        if tools:
            params["tools"] = tools
        if skills:
            params["skills"] = skills
        if allowed_tools:
            params["allowedTools"] = allowed_tools
        if req.max_iterations:
            params["maxIterations"] = req.max_iterations
        if req.timeout_seconds:
            params["timeoutSeconds"] = req.timeout_seconds

        resp = self.control.create_harness(**params)
        summary = _to_harness_summary(resp.get("harness", {}))
        if not summary.harness_id:
            raise RuntimeError("CreateHarness returned no harness id")
        return summary

    def update_harness(self, harness_id: str, req: UpdateHarnessRequest) -> HarnessSummary:
        """Change a harness in place; AgentCore makes a new version behind the
        same ARN and moves the DEFAULT endpoint to it once it is READY.

        UpdateHarness is a partial update (measured 2026-09-23), so only what the
        request names is sent. Never `memory`: re-submitting the managed-memory
        block risks recreating the Memory that holds live conversations, and the
        form has no reason to touch it. Never `harnessName` or `tags`: the API
        cannot rename, and the cost-allocation tags were set at creation.

        `tools`/`skills` are list replacements. They go whenever the request
        carries any selection field, empty list included — an edit that deselects
        every tool must clear them, and omitting the key would keep the old set.
        """
        self._require_configured()

        params: Dict[str, Any] = {"harnessId": harness_id}

        if req.system_prompt is not None:
            params["systemPrompt"] = [{"text": req.system_prompt}]
        if req.model_id:
            # bedrockModelConfig.modelId is required, so a cap-only change has to
            # resend the model id too; the form always sends both. The per-agent
            # inference profile is keyed by harness name, which only GetHarness
            # knows — read it only when AIP routing is on.
            agent_name = (
                self.get_harness(harness_id).harness_name if MODEL_COST_AIP_ENABLED else ""
            )
            params["model"] = {
                "bedrockModelConfig": {
                    "modelId": self._model_id_for(req.model_id, agent_name),
                    "maxTokens": req.max_tokens or HARNESS_MAX_TOKENS,
                }
            }
        if req.selects_tools:
            params["tools"] = self._compose_tools(
                req.mcp_record_ids or [], req.builtin_tools or [], req.gateway_arns or []
            )
        if req.selects_skills:
            params["skills"] = self._compose_skills(
                req.skill_record_ids or [],
                req.aws_skill_paths or [],
                req.skill_bucket_uris or [],
            )
        if req.allowed_tools is not None:
            params["allowedTools"] = req.allowed_tools
        if req.max_iterations:
            params["maxIterations"] = req.max_iterations
        if req.timeout_seconds:
            params["timeoutSeconds"] = req.timeout_seconds
        if req.truncation is not None:
            params["truncation"] = req.truncation.to_api()

        resp = self.control.update_harness(**params)
        return _to_harness_summary(resp.get("harness", {}))

    def _wait_ready(self, harness_id: str) -> HarnessSummary:
        """
        Block until a harness leaves CREATING/UPDATING.

        The companion runtime ARN needed to register the harness is only populated
        once it settles, and a failed transition must surface rather than be
        registered.
        """
        summary = self.get_harness(harness_id)
        for _ in range(_CREATE_POLL_ATTEMPTS):
            if summary.status not in ("CREATING", "UPDATING"):
                break
            time.sleep(_CREATE_POLL_INTERVAL_SECONDS)
            summary = self.get_harness(harness_id)

        if summary.status in ("CREATE_FAILED", "UPDATE_FAILED"):
            raise RuntimeError(
                f"Harness {summary.status.lower().replace('_', ' ')}: "
                f"{summary.failure_reason or 'unknown reason'}"
            )
        return summary

    def get_harness(self, harness_id: str) -> HarnessSummary:
        resp = self.control.get_harness(harnessId=harness_id)
        return _to_harness_summary(resp.get("harness", {}))

    def gateway_target_names(self, gateway_arn: str) -> List[str]:
        """The names of the targets on one AgentCore gateway.

        A gateway serves each of its targets' tools under `<target>___<tool>`, so
        the target names are what let a recorded tool call be traced to the gateway
        that served it (see `usage_service.mcp_gateway_usage`). The gateway id is
        the last ARN segment, which is what `ListGatewayTargets` addresses.

        Cached per ARN for `_target_cache_seconds`: the names change only on an
        infra redeploy, and this is read on every gateway-record Usage open.
        """
        cached = self._target_cache.get(gateway_arn)
        if cached is not None and (
            time.monotonic() - cached[0] < self._target_cache_seconds
        ):
            return cached[1]

        gateway_id = gateway_arn.rsplit("/", 1)[-1]
        names: List[str] = []
        params: Dict[str, Any] = {"gatewayIdentifier": gateway_id, "maxResults": 100}
        token: Optional[str] = None
        while True:
            if token:
                params["nextToken"] = token
            resp = self.control.list_gateway_targets(**params)
            names.extend(
                target["name"]
                for target in resp.get("items", [])
                if target.get("name")
            )
            token = resp.get("nextToken")
            if not token:
                break

        self._target_cache[gateway_arn] = (time.monotonic(), names)
        return names

    def gateway_target_endpoints(self, gateway_arn: str) -> Dict[str, str]:
        """Target name -> the MCP server URL that target proxies, for one gateway.

        Only `mcpServer` targets have an endpoint; Lambda, connector and OpenAPI
        targets are left out. This is how a registry record that describes a
        Runtime-hosted MCP server by its invocations URL is tied to the gateway
        target agents actually call it through (see `usage_service.record_reach`):
        `ListGatewayTargets` names the targets, `GetGatewayTarget` holds the URL.

        One GetGatewayTarget per target, so cached like the names.
        """
        cached = self._endpoint_cache.get(gateway_arn)
        if cached is not None and (
            time.monotonic() - cached[0] < self._target_cache_seconds
        ):
            return cached[1]

        gateway_id = gateway_arn.rsplit("/", 1)[-1]
        endpoints: Dict[str, str] = {}
        params: Dict[str, Any] = {"gatewayIdentifier": gateway_id, "maxResults": 100}
        token: Optional[str] = None
        while True:
            if token:
                params["nextToken"] = token
            resp = self.control.list_gateway_targets(**params)
            for target in resp.get("items", []):
                name = target.get("name")
                target_id = target.get("targetId")
                if not name or not target_id:
                    continue
                detail = self.control.get_gateway_target(
                    gatewayIdentifier=gateway_id, targetId=target_id
                )
                endpoint = (
                    ((detail.get("targetConfiguration") or {}).get("mcp") or {})
                    .get("mcpServer") or {}
                ).get("endpoint")
                if isinstance(endpoint, str) and endpoint:
                    endpoints[name] = endpoint
            token = resp.get("nextToken")
            if not token:
                break

        self._endpoint_cache[gateway_arn] = (time.monotonic(), endpoints)
        return endpoints

    def team_tag_of(self, harness_arn: str) -> Optional[str]:
        """The harness's `Team` tag (set at creation, see create_harness), or
        None for a shared harness. Raises when the tags cannot be read — the
        caller decides what an unknown team means (it hides the harness from
        non-admins). Tags never change after creation, so the cache is long."""
        hit = self._team_tag_cache.get(harness_arn)
        now = time.monotonic()
        if hit and now - hit[0] < _TEAM_TAG_CACHE_SECONDS:
            return hit[1]
        tags = self.control.list_tags_for_resource(resourceArn=harness_arn).get("tags") or {}
        team = normalize_team(tags.get("Team"))
        self._team_tag_cache[harness_arn] = (now, team)
        return team

    def list_harnesses(self, with_tools: bool = False) -> List[HarnessSummary]:
        summaries: List[HarnessSummary] = []
        params: Dict[str, Any] = {"maxResults": 100}
        token: Optional[str] = None
        while True:
            if token:
                params["nextToken"] = token
            resp = self.control.list_harnesses(**params)
            summaries.extend(
                _to_harness_summary(h) for h in resp.get("harnesses", [])
            )
            token = resp.get("nextToken")
            if not token:
                break

        # ListHarnesses omits `environment`, so the companion runtime ARN only
        # exists per-harness. Without it the sync service cannot recognise a
        # runtime as harness-owned and offers it as a directly-invokable agent —
        # which AgentCore refuses at chat time. A CREATING harness has no
        # runtime yet, so only settled ones are worth the GetHarness.
        #
        # `with_tools` widens that to the `tools`/`skills` lists, which ListHarnesses
        # also omits: the usage rollups match an agent to the MCP servers and skills
        # its harness attaches, and a summary with `tools=None` matches nothing — the
        # bug behind a gateway record whose Usage showed zero attached agents even
        # though agents called through it.
        def needs_hydration(summary: HarnessSummary) -> bool:
            if summary.status == "CREATING":
                return False
            if summary.runtime_arn is None:
                return True
            return with_tools and summary.tools is None and summary.skills is None

        stale = [
            index
            for index, summary in enumerate(summaries)
            if needs_hydration(summary)
        ]
        for index, hydrated in zip(
            stale, fan_out(lambda i: self._hydrate(summaries[i]), stale)
        ):
            summaries[index] = hydrated
        return summaries

    def _hydrate(self, summary: HarnessSummary) -> HarnessSummary:
        try:
            return self.get_harness(summary.harness_id)
        except Exception as exc:
            logger.warning(
                "Could not hydrate harness %s: %s", summary.harness_id, exc
            )
            return summary

    def delete_harness(self, harness_id: str) -> List[str]:
        """
        Delete the harness and deprecate the registry record that pointed at it.

        Without this the registry keeps advertising an agent whose harness is
        gone, and chat bound to that record fails at invoke time.
        """
        try:
            summary = self.get_harness(harness_id)
            arns: Tuple[Optional[str], ...] = (summary.harness_arn, summary.runtime_arn)
        except Exception as exc:
            # Deleting still has to work if the lookup fails; only cleanup is lost.
            logger.warning(
                "Could not read harness %s before delete: %s", harness_id, exc
            )
            arns = ()

        self.control.delete_harness(harnessId=harness_id)
        return self.registry.deprecate_records_for_arns(*arns)

    def compose_and_register(
        self, req: ComposeHarnessRequest, caller: Optional[AuthUser] = None
    ) -> ComposeHarnessResponse:
        """
        Create the harness and answer immediately; registration follows READY.

        The full chain — CreateHarness, the companion runtime reaching READY,
        registry record creation and approval — takes minutes, and every proxy
        between the browser and this server (Next.js middleware at 30s, the ALB
        at 60s) cuts a response held that long. So the response carries the
        CREATING harness, and a background thread publishes it to the registry
        as an A2A agent once it settles. The UI already knows how to show an
        unregistered harness and offer manual registration if this thread dies.
        """
        req = req.model_copy(update={"team": self.resolve_team(req, caller)})
        req = self._enforce_team_limits(req, caller)
        created = self.create_harness(req)
        # No registry, nothing to publish to: the harness is still chattable
        # through the deployed-resource fallback (`deployed_agent_records`).
        if registry_enabled():
            # The composer owns the record; the thread cannot read the request
            # context later, so the identity is resolved here.
            self._spawn_registration(created.harness_id, req, owner_identity(caller))
        return ComposeHarnessResponse(harness=created)

    def _enforce_team_limits(
        self, req: ComposeHarnessRequest, caller: Optional[AuthUser]
    ) -> ComposeHarnessRequest:
        """What a non-admin may compose, decided here rather than trusted from
        the form.

        - The team's `allowed_tools`, when set, replace the request's: the
          built-in tools gateway is LOG_ONLY by design, so that list (its
          `@builtin` entry above all) is the only thing between a member and
          tools the team admin withheld. Admins may still override.
        - Every MCP/skill record the harness is composed from must be one the
          caller can see; another team's record is refused by id (→ 400).
        """
        teams = self.teams
        if caller is None or caller.is_admin or teams is None or not teams.enabled:
            return req
        if req.team:
            team_cfg = teams.get(req.team)
            if team_cfg is not None and team_cfg.allowed_tools:
                req = req.model_copy(update={"allowed_tools": []})
        record_ids = list(req.mcp_record_ids or []) + list(req.skill_record_ids or [])
        if record_ids:
            details = fan_out(self._record_or_none, record_ids)
            for record_id, detail in zip(record_ids, details):
                visible = detail is not None and can_see(
                    team_of(detail), caller, teams_enabled=True, known=visibility_known(detail)
                )
                if not visible:
                    raise ValueError(f"Record {record_id} is not available to you")
        return req

    def _record_or_none(self, record_id: str) -> Optional[Any]:
        try:
            return self.registry.get_record(record_id)
        except Exception as exc:  # noqa: BLE001 — unreadable counts as not visible
            logger.warning("Could not read record %s for a visibility check: %s", record_id, exc)
            return None

    def _spawn_registration(
        self, harness_id: str, req: ComposeHarnessRequest, owner: Optional[str] = None
    ) -> None:
        threading.Thread(
            target=self._register_when_ready,
            args=(harness_id, req, owner),
            name=f"harness-register-{harness_id}",
            daemon=True,
        ).start()

    def _register_when_ready(
        self, harness_id: str, req: ComposeHarnessRequest, owner: Optional[str] = None
    ) -> None:
        """Publish a settling harness to the registry. Never raises: this runs
        detached, so an exception would only kill its own thread silently."""
        try:
            summary = self._wait_ready(harness_id)
        except Exception as exc:
            logger.error("Harness %s did not become ready: %s", harness_id, exc)
            return

        if self.on_runtime_ready is not None and summary.runtime_arn:
            try:
                self.on_runtime_ready(summary.runtime_arn)
            except Exception:
                logger.warning(
                    "Usage log delivery for %s could not be ensured", summary.runtime_arn,
                    exc_info=True,
                )

        try:
            # A manual sync may have registered it first; a second record for
            # the same harness would show up as a duplicate agent in chat.
            if summary.harness_arn in self.registry.agent_records_by_arn():
                logger.info("Harness %s is already registered", harness_id)
                return
            self.registry.create_record(
                CreateRecordRequest(
                    name=req.name,
                    description=req.description,
                    descriptor_type=DESCRIPTOR_A2A,
                    version="1.0",
                    harness_arn=summary.harness_arn,
                    agent_runtime_arn=summary.runtime_arn,
                    submit_for_approval=True,
                    custom_metadata={"team": req.team} if req.team else None,
                ),
                owner=owner,
            )
        except Exception as exc:
            # The harness exists and is usable; the UI's deployed-targets view
            # shows it as unregistered with a manual register button.
            logger.error(
                "Harness %s created but registry registration failed: %s",
                summary.harness_arn,
                exc,
            )
