"""
Shared dependencies for FastAPI routes
"""
from repositories.artifact_repository import ArtifactRepository
from repositories.thread_repository import ThreadRepository
from repositories.knowledge_repository import KnowledgeRepository
from repositories.usage_repository import UsageRepository
from repositories.prefs_repository import PrefsRepository
from repositories.user_repository import UserRepository
from repositories.team_repository import TeamRepository
from services.artifact_service import ArtifactService
from services.thread_service import ThreadService
from services.mcp_apps_service import shared_relay
from services.streaming_service import StreamingService
from services.run_broker import RunBroker
from services.harness_output_service import HarnessOutputService
from services.artifact_preview_service import ArtifactPreviewService
from services.knowledge_provisioner import KnowledgeProvisioner
from services.knowledge_service import KnowledgeService
from services.attachment_service import AttachmentService
from services.browser_screenshot_service import BrowserScreenshotService
from services.usage_service import UsageService
from services.telemetry_service import TelemetryService
from services.trace_service import TraceService
from services.billing_service import BillingService
from services.pricing_service import PricingService
from services.rate_card_service import RateCardService
from services.evaluation_service import EvaluationService
from services.layout_service import LayoutService
from services.nav_service import NavService
from services.team_service import TeamService
from services.collector_service import CollectorService
from services.observability_service import ObservabilityService
from services.directory_service import DirectoryService
from services.org_directory_service import OrgDirectoryService
from services.audit_service import AuditService
from services.registry_service import set_owner_resolver
from agents.agentcore_client import AgentCoreClient
from core.config import (
    MCP_GATEWAY_ID,
    TEAMS_TABLE,
    COGNITO_REGION,
    COGNITO_USER_POOL_ID,
    ARTIFACTS_BUCKET,
    ARTIFACTS_TABLE,
    AWS_REGION,
    DYNAMODB_THREADS_TABLE,
    KNOWLEDGE_BUCKET,
    KNOWLEDGE_TABLE,
    KB_SERVICE_ROLE_ARN,
    KB_GATEWAY_ROLE_ARN,
    KNOWLEDGE_PLATFORM_SOURCE_BUCKET,
    PREFS_TABLE,
    USAGE_TABLE,
    PLATFORM,
    USAGE_LOG_GROUP,
    USAGE_LOG_DESTINATION_ARN,
    USERS_TABLE,
    TEAM_ROLE_BOUNDARY_ARN,
    POLICY_ENGINE_ID,
    MCP_GATEWAY_ARN,
    TEAM_SHARED_ACTIONS,
    SKILLS_BUCKET,
)
import boto3

# Initialize repository instances (DynamoDB)
thread_repository = ThreadRepository(
    table_name=DYNAMODB_THREADS_TABLE,
    region_name=AWS_REGION
)

# The base repository raises when the table is missing, so only construct it once
# artifact storage is configured. Without it artifacts still stream to the UI,
# just unpersisted.
artifact_repository = (
    ArtifactRepository(table_name=ARTIFACTS_TABLE, region_name=AWS_REGION)
    if ARTIFACTS_TABLE
    else None
)

# Initialize service instances
thread_service = ThreadService(thread_repository)
artifact_service = ArtifactService(
    repository=artifact_repository,
    bucket=ARTIFACTS_BUCKET,
    region=AWS_REGION,
)

attachment_service = AttachmentService()

# Reads only, and out of a bucket this server never writes: the built-in-tools
# gateway Lambda owns the objects. Unconfigured is a supported state — the route
# reports 503 and the chat still shows the live presigned URL.
browser_screenshot_service = BrowserScreenshotService()

# Chat is served exclusively by agents deployed to AgentCore Runtime.
agentcore_client = AgentCoreClient(
    region_name=AWS_REGION,
    attachment_service=attachment_service,
)

# Preview service for extracted binary artifacts.
artifact_preview_service = ArtifactPreviewService(artifact_service=artifact_service)

# A harness executes its tools inside AWS, so files it produces never reach this
# process on their own; this collects them after the turn. Given the same
# artifact service so swept files land beside the runtime path's artifacts.
harness_output_service = HarnessOutputService(
    artifact_service=artifact_service,
    preview_service=artifact_preview_service,
)

# Same conditional construction as artifact_repository: the base repository probes
# its table at import time, so without the table name the container would fail to
# start instead of reporting the feature as unconfigured.
usage_repository = (
    UsageRepository(table_name=USAGE_TABLE, region_name=AWS_REGION)
    if USAGE_TABLE
    else None
)

usage_service = UsageService(repository=usage_repository)

# Read at request time and never mirrored — the same reasoning as
# knowledge-document status: a mirror drifts and the drift is invisible.
telemetry_service = TelemetryService(region_name=AWS_REGION)

# Logs Insights is asynchronous and several seconds slow, so this is only ever
# reached when a user expands one turn — never from a list view.
trace_service = TraceService(region_name=AWS_REGION)

# Cost Explorer is charged per request, so this instance's six-hour cache is
# shared process-wide rather than created per request.
billing_service = BillingService(region_name=AWS_REGION)

# The published AgentCore rate card. Free to read and changes on the order of
# quarters, so it caches for a day — but shared process-wide anyway, because the
# point of the cache is that a poll never waits on it.
pricing_service = PricingService(region_name=AWS_REGION)

# Batch evaluations: on-demand LLM-as-judge runs over real sessions. Shared
# process-wide for the cache across evaluators().
evaluation_service = EvaluationService(region_name=AWS_REGION)

# Initialize streaming service. The relay is passed so an app-mount signal can be
# stamped with the MCP record that serves it: the runtime knows only the ui:// URI,
# and the relay resolves endpoints from records alone.
# One broker for the process: the routes that attach to and cancel runs must see
# the same runs the start route created. See services/run_broker.py.
run_broker = RunBroker()

streaming_service = StreamingService(
    thread_service=thread_service,
    agentcore_client=agentcore_client,
    artifact_service=artifact_service,
    mcp_apps_relay=shared_relay(),
    harness_output_service=harness_output_service,
    usage_service=usage_service,
    run_broker=run_broker,
)

# Same conditional construction as artifact_repository: the base repository
# probes its table at import time, so without the table name the container would
# fail to start instead of reporting the feature as unconfigured.
knowledge_repository = (
    KnowledgeRepository(table_name=KNOWLEDGE_TABLE, region_name=AWS_REGION)
    if KNOWLEDGE_TABLE
    else None
)

knowledge_provisioner = KnowledgeProvisioner(
    repository=knowledge_repository,
    region=AWS_REGION,
    service_role_arn=KB_SERVICE_ROLE_ARN,
    gateway_role_arn=KB_GATEWAY_ROLE_ARN,
)

knowledge_service = KnowledgeService(
    repository=knowledge_repository,
    provisioner=knowledge_provisioner,
    bucket=KNOWLEDGE_BUCKET,
    region=AWS_REGION,
    platform_source_bucket=KNOWLEDGE_PLATFORM_SOURCE_BUCKET,
    # The stored owner name is the UUID-valued username; listings resolve the
    # email through the directory below. A lambda because directory_service is
    # constructed further down, and is only needed per request.
    owner_names=lambda subs: directory_service.emails(subs),
)

# Per-user preferences: same conditional construction as artifact_repository.
# The base repository probes its table at import time, so without the table name
# the container would fail to start instead of reporting the feature as
# unconfigured. A degraded layout service operates in unpersisted mode.
prefs_repository = (
    PrefsRepository(table_name=PREFS_TABLE, region_name=AWS_REGION)
    if PREFS_TABLE
    else None
)

layout_service = LayoutService(repository=prefs_repository)

# Which sidebar menus a plain user sees; one platform-wide document in the same table.
nav_service = NavService(repository=prefs_repository)

# Teams: rows in their own table when TEAMS_TABLE is set (Organization page),
# else none.
team_repository = (
    TeamRepository(table_name=TEAMS_TABLE, region_name=AWS_REGION)
    if TEAMS_TABLE
    else None
)
team_service = TeamService(repository=team_repository)
# Attached here because streaming_service is built above, before teams exist (module order).
streaming_service.team_service = team_service

import core.auth as _auth
_auth.entra_team_resolver = team_service.teams_for_entra_groups

from services.team_provisioner import TeamProvisioner, account_id_of

# Creates the Cognito group, IAM role and Cedar policies behind a team made on
# the Organization page. `enabled` is False without the boundary policy (then
# the page can list but not create teams).
team_provisioner = TeamProvisioner(
    boto3.client("cognito-idp", region_name=COGNITO_REGION),
    boto3.client("iam", region_name=AWS_REGION),
    boto3.client("bedrock-agentcore-control", region_name=AWS_REGION),
    user_pool_id=COGNITO_USER_POOL_ID,
    project=PLATFORM,
    region=AWS_REGION,
    account_id=account_id_of(TEAM_ROLE_BOUNDARY_ARN),
    boundary_arn=TEAM_ROLE_BOUNDARY_ARN,
    skills_bucket=SKILLS_BUCKET,
    policy_engine_id=POLICY_ENGINE_ID,
    gateway_arn=MCP_GATEWAY_ARN,
    shared_actions=TEAM_SHARED_ACTIONS,
)

audit_service = AuditService(repository=team_repository)

# The rate card an admin can read into and fill. Shares the usage service (the
# overlay lives in its table and it reprices) and the Price List client.
rate_card_service = RateCardService(usage=usage_service, pricing=pricing_service)

# The collector shares the usage repository and the pricing cache. It is only
# *constructed* here; `main.py`'s lifespan decides whether it runs, so importing
# this module never starts a background loop (tests import it too).
collector_service = (
    CollectorService(
        repository=usage_repository,
        cloudwatch=boto3.client("cloudwatch", region_name=AWS_REGION),
        pricing=pricing_service,
        registry=None,
        harness=None,
        region=AWS_REGION,
    )
    if usage_repository is not None
    else None
)
# sub -> email for the admin views that name people. The pool signs in by email,
# so the access token carries no email and the ledger (keyed by sub, on purpose)
# never saw one; the name is looked up at read time and cached. Without a pool id
# (local development) it resolves nothing and calls nothing.
# OIDC (Entra) sign-ins land here on login so the directory can name them too;
# None without the table, and then only the pool is consulted.
user_repository = (
    UserRepository(table_name=USERS_TABLE, region_name=AWS_REGION)
    if USERS_TABLE
    else None
)
_cognito_client = boto3.client("cognito-idp", region_name=COGNITO_REGION)
directory_service = DirectoryService(
    cognito=_cognito_client,
    user_pool_id=COGNITO_USER_POOL_ID,
    users=user_repository,
)

# The Organization page's people list: the pool live (users + group members),
# Entra as recorded at login. Shares the cognito client with directory_service.
org_directory_service = OrgDirectoryService(
    cognito=_cognito_client,
    user_pool_id=COGNITO_USER_POOL_ID,
    users=user_repository,
    entra_teams=team_service.teams_for_entra_groups,
)
# Registry records are stamped with their registering user's e-mail; a Cognito
# access token only names the sub, so the directory resolves it.
set_owner_resolver(lambda sub: directory_service.emails([sub]).get(sub))

# Per-session runtime usage (vended USAGE_LOGS). Disabled — `enabled` False — until
# terraform provides the log group and delivery destination; the collector then
# stays metrics-only and the page reports session attribution as unavailable.
observability_service = ObservabilityService(
    logs=boto3.client("logs", region_name=AWS_REGION),
    log_group=USAGE_LOG_GROUP,
    destination_arn=USAGE_LOG_DESTINATION_ARN,
    project=PLATFORM,
    repository=usage_repository,
    runtime_rates=pricing_service.runtime_rates,
)

if collector_service is not None:
    # Lazily resolved like UsageService's own properties: a registry or harness
    # client failure at import must not take the server down with it.
    collector_service.registry = usage_service.registry
    collector_service.harness = usage_service.harness
    collector_service.billing = billing_service
    collector_service.reconciler = collector_service.reconcile
    collector_service.usage = usage_service
    collector_service.policy_gateway_ids = [MCP_GATEWAY_ID] if MCP_GATEWAY_ID else []
    if observability_service.enabled:
        collector_service.session_collector = observability_service.collect_sessions
        collector_service.usage_logs_ensurer = observability_service.ensure_usage_logs
        # A harness composed from the UI gets its delivery the moment its companion
        # runtime is READY, rather than on the collector's next pass.
        usage_service.harness.on_runtime_ready = observability_service.ensure_usage_logs
