"""
Configuration settings for the FastAPI application
"""
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Load environment variables from .env file.
#
# override=True: locally, .env wins over values already exported in the shell
# (e.g. AWS_REGION). Without it a shell's AWS_REGION=us-east-1 silently overrode
# an .env pointing at ap-northeast-1 and the server failed at boot looking for
# the tables in the wrong region. Deployed containers (ECS) have no .env file,
# so load_dotenv is a no-op there and injected variables are unaffected.
env_path = Path(__file__).parent.parent / ".env"
load_dotenv(dotenv_path=env_path, override=True)

# AWS Configuration
AWS_REGION = os.getenv("AWS_REGION", "ap-northeast-1")

# Extra AWS regions to also scan when listing deployable AgentCore runtimes, so a
# runtime deployed in another region can be registered and chatted from this
# platform without moving it. Comma-separated; empty = this region only. The
# invoke region is always derived from each runtime ARN, never from this list.
AGENT_RUNTIME_DISCOVERY_REGIONS = [
    r.strip()
    for r in os.getenv("AGENT_RUNTIME_DISCOVERY_REGIONS", "").split(",")
    if r.strip()
]

# The value of the `Platform` cost-allocation tag put on every harness and every
# Terraform resource. Billing filters the AgentCore total on it, so the tag value
# and the filter must be one string — this is that string. Defaults to the project
# prefix; Terraform passes it as PLATFORM=var.project.
PLATFORM = os.getenv("PLATFORM", "bap")

# Cognito authentication (password login + access-token verification). The
# pool id alone enables token verification; the client id enables the
# /api/auth/login password flow and so the "아이디 · 비밀번호" option on the
# login screen.
COGNITO_USER_POOL_ID = os.getenv("COGNITO_USER_POOL_ID", "")
COGNITO_USER_POOL_CLIENT_ID = os.getenv("COGNITO_USER_POOL_CLIENT_ID", "")
COGNITO_REGION = os.getenv("COGNITO_REGION", "ap-northeast-1")
# Local opt-out so the server runs without any identity provider. Never set this
# in a deployed environment — the Terraform server_env deliberately omits it.
AUTH_ENFORCED = os.getenv("AUTH_ENFORCED", "true").lower() != "false"


def _csv(name: str) -> List[str]:
    return [v.strip() for v in os.getenv(name, "").split(",") if v.strip()]


def _json_dict(name: str) -> Dict[str, str]:
    """A JSON object of string -> string from an env var; anything else reads as {}.

    Used for TEAM_EXECUTION_ROLES, which terraform `jsonencode`s. A malformed
    value must not keep the server from starting - teams are a feature, not a
    precondition - so bad JSON and non-string values are dropped, not raised.
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("%s is not valid JSON; ignoring", name)
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, str) and v}


# ── OIDC login providers (Microsoft Entra ID and the like) ───────────────────
# Each provider is a login button next to (or instead of) the Cognito password
# form. The browser runs Authorization Code + PKCE against the provider and
# sends the id_token as the bearer; core/auth.py routes the token by `iss` to
# the provider and applies that provider's groups-claim / admin rules.
#
# Terraform injects OIDC_PROVIDERS_JSON, a JSON array of objects:
#   {id, label, issuer_url, audience, client_id, redirect_uris[], scopes,
#    groups_claim, required_group, admin_groups[], admin_emails[]}
# For a single provider the flat OIDC_* variables below do the same job (local
# .env). admin_groups / admin_emails are server-side only and never leave via
# /api/auth/config.
OIDC_ISSUER_URL = os.getenv("OIDC_ISSUER_URL", "")  # https://login.microsoftonline.com/<tenant>/v2.0
OIDC_AUDIENCE = os.getenv("OIDC_AUDIENCE", "")      # Entra: the Application (client) ID
OIDC_CLIENT_ID = os.getenv("OIDC_CLIENT_ID", "")
OIDC_REDIRECT_URIS = _csv("OIDC_REDIRECT_URIS")     # https://<host>/auth/callback[,http://localhost:3000/auth/callback]
OIDC_SCOPES = os.getenv("OIDC_SCOPES", "openid profile email offline_access")
OIDC_PROVIDER_ID = os.getenv("OIDC_PROVIDER_ID", "entra")
OIDC_PROVIDER_LABEL = os.getenv("OIDC_PROVIDER_LABEL", "Microsoft 계정 (Entra ID)")
OIDC_GROUPS_CLAIM = os.getenv("OIDC_GROUPS_CLAIM", "groups")
# Members of this group may sign in; everyone else gets 403. Empty = whole tenant.
OIDC_REQUIRED_GROUP = os.getenv("OIDC_REQUIRED_GROUP", "")
OIDC_ADMIN_GROUPS = _csv("OIDC_ADMIN_GROUPS")
OIDC_ADMIN_EMAILS = [e.lower() for e in _csv("OIDC_ADMIN_EMAILS")]
OIDC_EMAIL_CLAIM = os.getenv("OIDC_EMAIL_CLAIM", "email")
OIDC_NAME_CLAIM = os.getenv("OIDC_NAME_CLAIM", "name")
OIDC_JWKS_CACHE_TTL_SECONDS = int(os.getenv("OIDC_JWKS_CACHE_TTL_SECONDS", "3600"))
OIDC_DISCOVERY_URL_OVERRIDE = os.getenv("OIDC_DISCOVERY_URL_OVERRIDE", "")

# Who has signed in through an OIDC provider, recorded on first login. Cognito
# users are resolved to a name via ListUsers; Entra users have no pool, so
# without this table the admin Insights views could only show their sub.
USERS_TABLE = os.getenv("USERS_TABLE", "")


@dataclass(frozen=True)
class OIDCProvider:
    id: str
    label: str
    issuer_url: str
    audience: str = ""
    client_id: str = ""
    redirect_uris: Tuple[str, ...] = ()
    scopes: str = "openid profile email"
    groups_claim: str = "groups"
    required_group: str = ""
    admin_groups: Tuple[str, ...] = ()
    admin_emails: Tuple[str, ...] = ()  # stored lower-cased

    @property
    def issuer(self) -> str:
        """`iss` comparison key and verifier cache key. No trailing slash."""
        return self.issuer_url.rstrip("/")

    def redirect_uri_for(self, origin: str) -> str:
        """The registered callback whose scheme+host matches the request Origin,
        so production (https://<host>) and a local dev server
        (http://localhost:3000) can share one provider. No match falls back to
        the first URI: the IdP then rejects the redirect_uri mismatch loudly,
        which beats the server failing silently."""
        if not self.redirect_uris:
            return ""
        origin_norm = (origin or "").rstrip("/").lower()
        for uri in self.redirect_uris:
            scheme, sep, rest = uri.partition("://")
            if not sep:
                continue
            if f"{scheme}://{rest.split('/', 1)[0]}".lower() == origin_norm:
                return uri
        return self.redirect_uris[0]

    def groups_of(self, claims: dict) -> List[str]:
        value = claims.get(self.groups_claim)
        return [str(g) for g in value] if isinstance(value, list) else []

    def allows(self, groups: List[str]) -> bool:
        """required_group unset admits everyone; set, only its members."""
        return not self.required_group or self.required_group in groups

    def role_for(self, email: str, groups: List[str]) -> str:
        if (email or "").lower() in self.admin_emails:
            return "admin"
        if any(g in self.admin_groups for g in groups):
            return "admin"
        return "user"


def _provider_from_dict(p: dict) -> OIDCProvider:
    uris = p.get("redirect_uris")
    if not isinstance(uris, list):
        single = (p.get("redirect_uri") or "").strip()
        uris = [single] if single else []
    pid = (p.get("id") or "").strip()
    return OIDCProvider(
        id=pid,
        label=(p.get("label") or pid),
        issuer_url=(p.get("issuer_url") or "").strip(),
        audience=(p.get("audience") or ""),
        client_id=(p.get("client_id") or ""),
        redirect_uris=tuple(u.strip() for u in uris if isinstance(u, str) and u.strip()),
        scopes=(p.get("scopes") or "openid profile email"),
        groups_claim=(p.get("groups_claim") or "groups"),
        required_group=(p.get("required_group") or ""),
        admin_groups=tuple(g for g in (p.get("admin_groups") or []) if g),
        admin_emails=tuple(e.lower() for e in (p.get("admin_emails") or []) if e),
    )


def _load_oidc_providers() -> List[OIDCProvider]:
    """OIDC_PROVIDERS_JSON first; otherwise one provider from the flat OIDC_*
    variables when an issuer is set. Entries without an issuer or id are
    dropped rather than half-configured."""
    raw = os.getenv("OIDC_PROVIDERS_JSON", "").strip()
    providers: List[OIDCProvider] = []
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = []
        if isinstance(data, list):
            providers = [
                _provider_from_dict(p)
                for p in data
                if isinstance(p, dict) and (p.get("issuer_url") or "").strip() and (p.get("id") or "").strip()
            ]
    if not providers and OIDC_ISSUER_URL:
        providers = [
            OIDCProvider(
                id=OIDC_PROVIDER_ID or "oidc",
                label=OIDC_PROVIDER_LABEL,
                issuer_url=OIDC_ISSUER_URL,
                audience=OIDC_AUDIENCE,
                client_id=OIDC_CLIENT_ID,
                redirect_uris=tuple(OIDC_REDIRECT_URIS),
                scopes=OIDC_SCOPES,
                groups_claim=OIDC_GROUPS_CLAIM,
                required_group=OIDC_REQUIRED_GROUP,
                admin_groups=tuple(OIDC_ADMIN_GROUPS),
                admin_emails=tuple(OIDC_ADMIN_EMAILS),
            )
        ]
    return providers


OIDC_PROVIDERS: List[OIDCProvider] = _load_oidc_providers()

# DynamoDB Configuration
DYNAMODB_THREADS_TABLE = os.getenv("DYNAMODB_THREADS_TABLE", "bap-threads")

# Bedrock Configuration
BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "global.anthropic.claude-sonnet-5-5")

# Models a per-thread override may pick, for every agent (runtime or harness).
# The operator's ceiling: a team list (services/team_service.py) can only narrow
# it. Empty = model overrides are refused and each agent runs its own default.
# Use inference-profile ids (global.anthropic.…): Claude 5 foundation-model ids
# are rejected by ConverseStream with an on-demand-throughput error.
ALLOWED_MODELS = _csv("ALLOWED_MODELS")
# team name -> harness execution role ARN, one per `teams` entry in terraform.
# Empty = the deployment declared no teams, and every team feature stays off.
TEAM_EXECUTION_ROLES: Dict[str, str] = _json_dict("TEAM_EXECUTION_ROLES")
# The retired basic-chat path pinned this synthetic id onto its threads. Kept
# only to recognise those threads: the next turn moves them onto the default
# agent's record (services/agent_access.py), and Insights names the history
# they left in the usage ledger (routes/insights.py).
LEGACY_BASIC_CHAT_RECORD_ID = "__basic_chat__"
LEGACY_BASIC_CHAT_LABEL = "기본 채팅 (이전)"
# Thread.metadata key holding the per-thread model / system-prompt override the
# web resends on every turn. The name predates overrides on runtime agents and
# stays so threads written under it keep their settings.
OVERRIDES_METADATA_KEY = "harness_overrides"

# AgentCore Agent Registry (created by infra/modules/agent_registry)
AGENT_REGISTRY_ID = os.getenv("AGENT_REGISTRY_ID", "")

# Whether to use the Agent Registry at all. "auto" (default): on when
# AGENT_REGISTRY_ID is set. "0"/"false": force off — records, chat binding and
# harness composition fall back to the deployed AgentCore resources themselves.
# "1"/"true": force on. A registry that is configured but answers with an AWS
# error degrades to the same fallback at read time (registry_service).
AP_USE_REGISTRY = os.getenv("AP_USE_REGISTRY", "auto").strip().lower()
# IAM role AWS Agent Registry assumes to SigV4-sign record synchronisation
# fetches against servers on AgentCore Runtime/Gateway (service `agent-registry`).
# The server task role needs iam:PassRole on it. Empty = the Register dialog
# offers only unauthenticated synchronisation.
REGISTRY_SYNC_ROLE_ARN = os.getenv("REGISTRY_SYNC_ROLE_ARN", "").strip()

# Managed Agent Harness (infra/modules/iam, infra/modules/s3_skills)
HARNESS_EXECUTION_ROLE_ARN = os.getenv("HARNESS_EXECUTION_ROLE_ARN", "")
SKILLS_BUCKET = os.getenv("SKILLS_BUCKET", "")

# Agent artifacts (infra/modules/s3_artifacts, infra/modules/dynamodb)
# Unset means artifacts still stream to the UI but are not persisted.
ARTIFACTS_BUCKET = os.getenv("ARTIFACTS_BUCKET", "")
ARTIFACTS_TABLE = os.getenv("ARTIFACTS_TABLE", "")

# Per-turn usage rollup (infra/modules/dynamodb). Unset means turns are not
# counted and the insights routes report themselves unconfigured, matching how
# knowledge reports 501 — the chat stream is never affected either way.
USAGE_TABLE = os.getenv("USAGE_TABLE", "")

# The calendar the usage counters bucket days in — an IANA name, e.g. Asia/Seoul.
# Defaults to UTC so nothing shifts on a redeploy. See core/clock.py for what
# changing it does to days that were already written.
USAGE_TIMEZONE = os.getenv("USAGE_TIMEZONE", "UTC")

# Per-user preferences (dashboard layout). Absent means the layout routes serve
# the default and accept no writes — the page still works, unpersisted.
PREFS_TABLE = os.getenv("PREFS_TABLE", "")
# The platform gateway whose policy-engine decision metrics Insights collects.
MCP_GATEWAY_ID = os.getenv("MCP_GATEWAY_ID", "").strip()

# Harness output sweep. A harness runs its tools inside AWS, so a file it produces
# stays in its sandbox; the server collects it after the turn (see
# services/harness_output_service.py). Unset means the feature is off, matching
# how artifact storage reports itself as unconfigured.
HARNESS_OUTPUT_SWEEP = os.getenv("HARNESS_OUTPUT_SWEEP", "true").lower() != "false"
HARNESS_OUTPUT_ROOTS = [
    path.strip()
    for path in os.getenv("HARNESS_OUTPUT_ROOTS", "/home").split(",")
    if path.strip()
]
# Only "output-shaped" files. Without this, a turn's helper scripts (.py, .xml,
# the node script that built the .docx) all land in the panel as artifacts.
HARNESS_OUTPUT_EXTENSIONS = [
    extension.strip().lower().lstrip(".")
    for extension in os.getenv(
        "HARNESS_OUTPUT_EXTENSIONS",
        "docx,xlsx,pptx,pdf,csv,md,html,png,jpg,svg,zip",
    ).split(",")
    if extension.strip()
]
HARNESS_OUTPUT_MAX_FILE_BYTES = int(
    os.getenv("HARNESS_OUTPUT_MAX_FILE_BYTES", str(50 * 1024 * 1024))
)
HARNESS_OUTPUT_MAX_FILES = int(os.getenv("HARNESS_OUTPUT_MAX_FILES", "10"))
HARNESS_OUTPUT_COMMAND_TIMEOUT = int(os.getenv("HARNESS_OUTPUT_COMMAND_TIMEOUT", "60"))

# Where the built-in-tools gateway Lambda writes browser screenshots
# (infra/builtin_tools_gateway — its own SCREENSHOT_BUCKET must name the same
# bucket). The server only reads it, to serve a screenshot back into the chat
# after the Lambda's presigned URL has expired — which widens the window from one
# hour to the bucket's 7-day expiry, not to forever. Unset -> that route reports
# 503 and the UI keeps whatever the live URL showed.
BROWSER_SCREENSHOT_BUCKET = os.getenv("BROWSER_SCREENSHOT_BUCKET", "")

# Knowledge bases (infra/modules/s3_knowledge, knowledge_roles, dynamodb).
# All four are required together; with any of them missing the knowledge routes
# return 501 rather than half-working.
KNOWLEDGE_BUCKET = os.getenv("KNOWLEDGE_BUCKET", "")
KNOWLEDGE_TABLE = os.getenv("KNOWLEDGE_TABLE", "")
KB_SERVICE_ROLE_ARN = os.getenv("KB_SERVICE_ROLE_ARN", "")
KB_GATEWAY_ROLE_ARN = os.getenv("KB_GATEWAY_ROLE_ARN", "")

# Buckets a user may attach as a knowledge base source, comma separated. Empty by
# default, which leaves upload-backed knowledge bases as the only kind. This
# mirrors the `source_buckets` Terraform variable that grants the kb-service role
# read access — a bucket named here but not there fails at sync time instead of at
# create time, so the two are meant to be changed together.
KNOWLEDGE_SOURCE_BUCKETS = [
    bucket.strip()
    for bucket in os.getenv("KNOWLEDGE_SOURCE_BUCKETS", "").split(",")
    if bucket.strip()
]
# The stack's own kb-source bucket, where managed S3 knowledge bases keep their
# per-user folders (users/{sub}/{kb_key}/). Distinct from
# KNOWLEDGE_SOURCE_BUCKETS: those are read-only external attachments an admin
# picks from, while this one the server also writes to. Unset -> ordinary users
# cannot create S3-backed knowledge bases (400 at create, not 501).
KNOWLEDGE_PLATFORM_SOURCE_BUCKET = os.getenv("KNOWLEDGE_PLATFORM_SOURCE_BUCKET", "")

# Route each harness's model call through a per-agent application inference
# profile so Bedrock model spend carries the AgentName tag (per-agent actual
# cost in Cost Explorer). Default off: turn on only after a live check confirms
# AgentCore Converse accepts an inference-profile ARN as modelId — otherwise
# harness creation would fail. See spec section G.
MODEL_COST_AIP_ENABLED = os.getenv("MODEL_COST_AIP_ENABLED", "false").lower() == "true"

# --- Insights collector & vended usage logs ----------------------------------
# The background collector reads CloudWatch's vended AgentCore quantities per day
# and writes priced day items into the usage table, so `/summary` never touches
# CloudWatch itself. Off in local dev by default: it needs the usage table, IAM
# for GetMetricData/GetProducts, and one process per stack (a DDB lease guards
# against two).
COLLECTOR_ENABLED = os.getenv("COLLECTOR_ENABLED", "false").lower() == "true"
COLLECTOR_INTERVAL_SECONDS = int(os.getenv("COLLECTOR_INTERVAL_SECONDS", "300"))
# Where AgentCore Runtime USAGE_LOGS (per-session vCPU/GB-hours) are delivered.
# Empty means session-level runtime attribution is off and the page says so.
USAGE_LOG_GROUP = os.getenv("USAGE_LOG_GROUP", "")
USAGE_LOG_DESTINATION_ARN = os.getenv("USAGE_LOG_DESTINATION_ARN", "")
