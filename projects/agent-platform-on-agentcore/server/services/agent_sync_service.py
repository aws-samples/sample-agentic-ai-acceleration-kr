"""
Reconciles deployed AgentCore agents and gateways with the Agent Registry.

Agents reach AgentCore two ways — `agent-runtime/scripts/deploy.sh` (a container
runtime) and the harness composer — but only the composer registers itself. AWS
has no publish-on-deploy hook (record synchronization pulls from HTTP MCP/A2A
endpoints, not from runtime ARNs), so the gap is closed here: list what is
deployed, diff it against the registry, and register the remainder.

MCP gateways are covered too, and are the reason a "deployed target" is not
always an agent. A gateway is a *tool surface*: it registers as an MCP record
rather than an A2A one, which is both what keeps it out of the chat agent picker
and what puts it in the harness composer's catalog.
"""
import logging
from typing import Any, Dict, List, Optional, Tuple

from models.registry import (
    CreateRecordRequest,
    DeployedTarget,
    DESCRIPTOR_A2A,
    DESCRIPTOR_MCP,
    RegistryRecordSummary,
    SyncAgentsRequest,
    SyncAgentsResponse,
    SyncFailure,
    TARGET_GATEWAY,
    TARGET_HARNESS,
    TARGET_RUNTIME,
)
from core.config import PLATFORM
from services.harness_service import HarnessService
from services.registry_service import (
    RegistryService,
    fan_out,
    is_deprecated,
    mcp_endpoint_of,
)

logger = logging.getLogger(__name__)

# Only settled deployments are worth registering; CREATING has no usable endpoint
# yet and CREATE_FAILED never will. Runtimes report READY, harnesses ACTIVE.
_READY_STATUSES = {"READY", "ACTIVE"}

# Gateway authorizers a harness can actually authenticate against. The only
# alternative to SigV4 is an OAuth credential provider, which is configured per
# gateway rather than discoverable here, so CUSTOM_JWT gateways are not auto-registered.
_SIGNABLE_AUTHORIZERS = {"AWS_IAM"}

# A runtime deployed with `-p MCP` serves tools instead of answering prompts, so it
# cannot back an A2A agent record. It belongs in the registry as an MCP record with a
# remote_url, which is a different shape than this reconciler produces.
_MCP_PROTOCOL = "MCP"

# AWS runs a harness's companion runtime from its own published image
# (`public.ecr.aws/<id>/harness-<region>:latest`); a hand-deployed runtime comes from
# an account ECR repository. That makes the image the one harness marker the runtime
# carries — nothing on it names its harness.
_HARNESS_IMAGE_REGISTRY = "public.ecr.aws/"
_HARNESS_IMAGE_MARKER = "/harness-"


def is_harness_managed_runtime(runtime: Any, companion_arns: Any = ()) -> bool:
    """Whether this runtime belongs to a harness rather than standing on its own.

    Two independent signals, because neither alone holds. The companion ARN set is
    authoritative but incomplete: it comes from a GetHarness per harness (ListHarnesses
    omits `environment`), so a harness the listing has not caught up to — a few seconds
    after CreateHarness — or one whose hydration failed contributes nothing to it. The
    image is the runtime's own evidence and needs no harness listing at all.

    Getting this wrong registers the companion as its own agent, and the mistake only
    surfaces at the first prompt: InvokeAgentRuntime answers ValidationException
    ("managed by a harness"), leaving a record in the chat picker that can never reply.
    """
    if runtime.agent_runtime_arn in companion_arns:
        return True
    image = runtime.container_uri or ""
    return image.startswith(_HARNESS_IMAGE_REGISTRY) and _HARNESS_IMAGE_MARKER in image


class AgentSyncService:
    def __init__(
        self,
        registry: Optional[RegistryService] = None,
        harness: Optional[HarnessService] = None,
        knowledge: Optional[Any] = None,
    ):
        self.registry = registry or RegistryService()
        self.harness = harness or HarnessService(registry=self.registry)
        # A KnowledgeRepository when the feature is configured, else None. Used
        # only to filter gateways out of the listing below.
        self.knowledge = knowledge

    def _knowledge_gateways(self) -> set:
        """Gateway ARNs owned by knowledge bases, which are not registry targets.

        A knowledge base's gateway is private plumbing: registering it would put
        one user's documents into the catalog every harness composer reads. Fails
        open — a listing that shows a few extra rows is better than a registry
        page that will not load.
        """
        if not self.knowledge:
            return set()
        try:
            return self.knowledge.all_gateway_arns()
        except Exception as exc:
            logger.warning("Could not read knowledge-base gateways: %s", exc)
            return set()

    def composable_gateways(self) -> List[Any]:
        """Deployed gateways a composer may pick, with knowledge bases removed.

        The registry's gateway listing feeds the manual register dialog, so it
        needs the same exclusion the bulk sync gets: otherwise an admin can hand-
        register another user's knowledge base as a public MCP record.
        """
        knowledge_gateways = self._knowledge_gateways()
        return [
            gateway
            for gateway in self.registry.list_gateways()
            if gateway.gateway_arn not in knowledge_gateways
        ]

    def list_deployed_targets(self) -> List[DeployedTarget]:
        """Everything deployed to AgentCore, annotated with its registry status."""
        # Two indexes: live records decide `registered`, while the deprecated-only
        # view explains *why* a live deployment has no active record. Deprecation
        # is terminal in AWS, so re-registering means creating a new record — which
        # a bulk sync must not do on its own. Both are built from one registry
        # scan, alongside the deployment listings.
        agent_records, harnesses, runtimes, gateways, gateway_records = fan_out(
            lambda call: call(),
            [
                self.registry.agent_records,
                self.harness.list_harnesses,
                self.registry.list_agent_runtimes,
                self.registry.list_gateways,
                self.registry.gateway_arns,
            ],
        )

        records: Dict[str, Any] = {}
        deprecated: Dict[str, Any] = {}
        for record in agent_records:
            index = deprecated if is_deprecated(record) else records
            for arn in (record.harness_arn, record.agent_runtime_arn):
                if arn:
                    index.setdefault(arn, record)
        retired = {
            arn: record for arn, record in deprecated.items() if arn not in records
        }
        targets: List[DeployedTarget] = []
        # A harness's companion runtime also shows up in ListAgentRuntimes, but it
        # rejects InvokeAgentRuntime — it must not be offered as its own agent.
        companion_runtimes = {h.runtime_arn for h in harnesses if h.runtime_arn}

        def binding(
            description: Optional[str], *arns: Optional[str]
        ) -> Dict[str, Any]:
            """Registry fields for a deployment, preferring a live record."""
            live = next((records[a] for a in arns if a and a in records), None)
            gone = next((retired[a] for a in arns if a and a in retired), None)
            record = live or gone
            return {
                "registered": live is not None,
                "retired": live is None and gone is not None,
                "record_id": record.record_id if record else None,
                "record_name": record.name if record else None,
                "record_status": record.status if record else None,
                # Harnesses have no description field in the AWS API, so the bound
                # record's is the only one there is; for runtimes it is a fallback.
                "description": description
                or (record.description if record else None),
            }

        for harness in harnesses:
            reason: Optional[str] = None
            if (harness.status or "").upper() not in _READY_STATUSES:
                reason = f"Harness status is {harness.status or 'unknown'}."
            targets.append(
                DeployedTarget(
                    kind=TARGET_HARNESS,
                    name=harness.harness_name,
                    arn=harness.harness_arn,
                    status=harness.status,
                    runtime_arn=harness.runtime_arn,
                    reason=reason,
                    **binding(None, harness.harness_arn, harness.runtime_arn),
                )
            )

        for runtime in runtimes:
            if is_harness_managed_runtime(runtime, companion_runtimes):
                continue
            reason = None
            if (runtime.status or "").upper() not in _READY_STATUSES:
                reason = f"Runtime status is {runtime.status or 'unknown'}."
            elif (runtime.server_protocol or "").upper() == _MCP_PROTOCOL:
                # Chat would bind to an endpoint that serves tools and never answers a
                # prompt. Registering it as an MCP record needs a remote_url the caller
                # supplies, so it is offered manually rather than swept up in a sync.
                reason = (
                    "Runtime serves the MCP protocol, not an agent — register it as an "
                    "MCP tool source instead of an agent."
                )
            targets.append(
                DeployedTarget(
                    kind=TARGET_RUNTIME,
                    name=runtime.name,
                    arn=runtime.agent_runtime_arn,
                    status=runtime.status,
                    reason=reason,
                    **binding(runtime.description, runtime.agent_runtime_arn),
                )
            )

        knowledge_gateways = self._knowledge_gateways()

        for gateway in gateways:
            if gateway.gateway_arn in knowledge_gateways:
                continue
            reason = None
            if (gateway.status or "").upper() not in _READY_STATUSES:
                reason = f"Gateway status is {gateway.status or 'unknown'}."
            elif (gateway.authorizer_type or "").upper() not in _SIGNABLE_AUTHORIZERS:
                # A harness signs its outbound gateway calls with SigV4 from its
                # execution role. A CUSTOM_JWT gateway rejects that with 401 at the
                # first tool call — and the harness still reaches READY, so the
                # breakage only shows up in use. Registering it would advertise a
                # tool surface nothing on this platform can call, so it is listed
                # with a reason instead.
                reason = (
                    f"Gateway uses {gateway.authorizer_type or 'unknown'} auth; a "
                    "harness can only call gateways that accept SigV4 (AWS_IAM)."
                )
            record = gateway_records.get(gateway.gateway_arn)
            targets.append(
                DeployedTarget(
                    kind=TARGET_GATEWAY,
                    name=gateway.name,
                    arn=gateway.gateway_arn,
                    status=gateway.status,
                    description=gateway.description
                    or (record.description if record else None),
                    gateway_url=gateway.gateway_url,
                    reason=reason,
                    # Gateway records are indexed live-only, so there is no separate
                    # retired state to report: a deprecated record simply leaves the
                    # gateway unregistered, and re-registering is the normal path.
                    registered=record is not None,
                    record_id=record.record_id if record else None,
                    record_name=record.name if record else None,
                    record_status=record.status if record else None,
                )
            )

        return targets

    @staticmethod
    def _record_request(
        target: DeployedTarget, submit_for_approval: bool
    ) -> CreateRecordRequest:
        """The registry record that binds a deployed target."""
        description = target.description or (
            f"Auto-registered from deployed AgentCore {target.kind}."
        )
        # The same cost-allocation tag every harness and gateway carries, so the
        # catalog's records are attributable to this stack too.
        tags = {"Platform": PLATFORM}
        if target.kind == TARGET_GATEWAY:
            # MCP, not A2A: a gateway serves tools rather than answering prompts, so
            # an A2A record would offer it in the chat agent picker as something that
            # cannot hold a conversation. The MCP type is also what the harness
            # composer reads its catalog from.
            #
            # No `sync_url` here on purpose: synchronising would overwrite the
            # record's name/description/version with what the gateway advertises
            # and collide with any other record synced from the same server. An
            # admin can opt into it per record from the Register dialog.
            return CreateRecordRequest(
                name=target.name,
                description=description,
                descriptor_type=DESCRIPTOR_MCP,
                version="1.0.0",
                remote_url=mcp_endpoint_of(target.gateway_url) if target.gateway_url else None,
                gateway_arn=target.arn,
                tags=tags,
                submit_for_approval=submit_for_approval,
            )

        return CreateRecordRequest(
            name=target.name,
            description=description,
            descriptor_type=DESCRIPTOR_A2A,
            version="1.0",
            tags=tags,
            harness_arn=(target.arn if target.kind == TARGET_HARNESS else None),
            agent_runtime_arn=(
                target.runtime_arn if target.kind == TARGET_HARNESS else target.arn
            ),
            # Runtimes are invoked against their default endpoint; harness records
            # are invoked through InvokeHarness.
            qualifier=("DEFAULT" if target.kind == TARGET_RUNTIME else None),
            submit_for_approval=submit_for_approval,
        )

    def deployed_agent_records(self) -> List[RegistryRecordSummary]:
        """Chattable agents synthesised from deployed AgentCore resources.

        The registry-off fallback for `GET /records`. Uses only registry-id-free
        control-plane reads, so it works with the registry disabled, blocked by
        an SCP, or retired. Mirrors the binding `_record_request` builds for a
        bulk sync: a harness binds its own ARN plus its companion runtime; a
        standalone runtime binds itself with the DEFAULT qualifier. Companion
        runtimes, MCP-protocol runtimes and not-ready targets are left out —
        none can answer a chat. The synthetic `deployed:<arn>` id is what the
        chat pins, so it must be stable across calls: the ARN is.
        """
        harnesses = self.harness.list_harnesses()
        runtimes = self.registry.list_agent_runtimes()
        companion = {h.runtime_arn for h in harnesses if h.runtime_arn}

        records: List[RegistryRecordSummary] = []
        ready = [
            h for h in harnesses
            if (h.status or "").upper() in _READY_STATUSES and h.runtime_arn
        ]
        for h, (known, team) in zip(ready, fan_out(self._harness_team, ready)):
            records.append(
                RegistryRecordSummary(
                    record_id=f"deployed:{h.harness_arn}",
                    name=h.harness_name,
                    description=None,
                    descriptor_type=DESCRIPTOR_A2A,
                    status="APPROVED",
                    harness_arn=h.harness_arn,
                    agent_runtime_arn=h.runtime_arn,
                    source="deployed",
                    # The same field a registry record carries its team in, so
                    # the one team filter covers both (services/team_access.py).
                    custom_metadata={"team": team} if team else None,
                    visibility_known=known,
                )
            )
        for r in runtimes:
            if is_harness_managed_runtime(r, companion):
                continue
            if (r.status or "").upper() not in _READY_STATUSES:
                continue
            if (r.server_protocol or "").upper() == _MCP_PROTOCOL:
                continue
            records.append(
                RegistryRecordSummary(
                    record_id=f"deployed:{r.agent_runtime_arn}",
                    name=r.name,
                    description=r.description,
                    descriptor_type=DESCRIPTOR_A2A,
                    status="APPROVED",
                    agent_runtime_arn=r.agent_runtime_arn,
                    qualifier="DEFAULT",
                    source="deployed",
                )
            )
        return records

    def _harness_team(self, harness: Any) -> Tuple[bool, Optional[str]]:
        """(known, team) of a deployed harness from its `Team` tag.

        A teamed harness runs as its team's execution role, so a fallback
        record that dropped the team would let any user run it (and its role's
        tools) through a `deployed:<arn>` id. Unreadable tags → unknown, which
        the team filter hides from non-admins.
        """
        reader = getattr(self.harness, "team_tag_of", None)
        if reader is None:
            return False, None
        try:
            return True, reader(harness.harness_arn)
        except Exception as exc:  # noqa: BLE001 — degrade to "unknown", never 500
            logger.warning("Could not read tags of harness %s: %s", harness.harness_arn, exc)
            return False, None

    def sync(self, req: SyncAgentsRequest, owner: Optional[str] = None) -> SyncAgentsResponse:
        """
        Register every unregistered, ready target (optionally a chosen subset).
        `owner` is stamped on each new record's metadata (the admin running it).

        A target whose only record was deprecated is skipped unless it is named
        explicitly: deprecation is terminal, so re-registering creates a second
        record, and a curator who retired an agent should not have a blanket sync
        undo that decision behind their back.
        """
        wanted = {t.strip() for t in req.targets if t.strip()}
        response = SyncAgentsResponse()

        for target in self.list_deployed_targets():
            named = target.arn in wanted or target.name in wanted
            if wanted and not named:
                continue
            if target.registered or target.reason:
                response.skipped.append(target)
                continue
            if target.retired and not named:
                response.skipped.append(target)
                continue

            try:
                record = self.registry.create_record(
                    self._record_request(target, req.submit_for_approval), owner=owner
                )
                response.registered.append(record)
            except Exception as exc:
                logger.error("Failed to register %s (%s): %s", target.name, target.arn, exc)
                response.failed.append(
                    SyncFailure(name=target.name, arn=target.arn, error=str(exc))
                )

        return response
