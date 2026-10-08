"""AWS Bedrock AgentCore Agent Registry routes."""
import logging
import traceback

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile

from core.auth import AuthUser, current_user, require_admin
from core.dependencies import knowledge_repository
from models.registry import (
    CreateRecordRequest,
    DESCRIPTOR_AGENT_SKILLS,
    RegistryRecordDetail,
    RegistryRecordSummary,
    SyncAgentsRequest,
    SyncAgentsResponse,
    UpdateRecordRequest,
    UpdateStatusRequest,
)
from models.skill import (
    InvalidSkillBundle,
    SkillBundleInfo,
    SkillFilesResponse,
    SkillStoreNotConfigured,
)
from services.agent_sync_service import AgentSyncService, is_harness_managed_runtime
from services.harness_service import HarnessNotConfigured
from services.registry_service import (
    RegistryNotConfigured,
    RegistryService,
    is_deprecated,
    is_registry_unavailable,
    registry_enabled,
    skill_source_of,
)
from services.skill_bundle_service import (
    DiscoveredSkill,
    SkillBundleService,
    StagedBundle,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/registry", tags=["registry"])


_service = RegistryService()
_sync_service = AgentSyncService(registry=_service, knowledge=knowledge_repository)
_skills = SkillBundleService()


def _registry() -> RegistryService:
    return _service


def _sync() -> AgentSyncService:
    return _sync_service


def _bundles() -> SkillBundleService:
    return _skills


def _deployed_detail(record_id: str) -> RegistryRecordDetail:
    """Detail for a deployed-fallback record, synthesised from the deployed
    resource instead of fetched from the registry.

    A `deployed:<arn>` id is not a real registry record — it exists only in the
    registry-off fallback (`AgentSyncService.deployed_agent_records`) — so handing
    it to the AgentCore GetRegistryRecord API gets rejected as a malformed
    recordId (a 502). The list route already answers these records from the same
    fallback; this is the single-record counterpart, so opening one by id degrades
    the same way the listing does. 404 when the id names no live deployment.
    """
    for summary in _sync().deployed_agent_records():
        if summary.record_id == record_id:
            return RegistryRecordDetail(**summary.model_dump())
    raise HTTPException(status_code=404, detail=f"Record not found: {record_id}")


def _fail(exc: Exception) -> None:
    if isinstance(
        exc, (RegistryNotConfigured, HarnessNotConfigured, SkillStoreNotConfigured)
    ):
        raise HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, InvalidSkillBundle):
        # Every broken rule at once, so one upload attempt is enough to fix them.
        raise HTTPException(status_code=400, detail="\n".join(exc.errors))
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {})
        raise HTTPException(
            status_code=502,
            detail=f"AWS error ({error.get('Code', 'Unknown')}): {error.get('Message', str(exc))}",
        )
    logger.error("Registry route error: %s", exc)
    logger.error(traceback.format_exc())
    raise HTTPException(status_code=500, detail=str(exc))


@router.get("/info")
def get_registry_info(_: AuthUser = Depends(current_user)):
    try:
        return _registry().get_registry_info()
    except Exception as exc:
        _fail(exc)


@router.get("/records")
def list_records(
    type: str | None = Query(None, description="Filter by descriptorType"),
    status: str | None = Query(None, description="Filter by record status"),
    name: str | None = Query(None, description="Exact record name"),
    _: AuthUser = Depends(current_user),
):
    # Registry off: the chattable agents are the deployed resources themselves.
    if not registry_enabled():
        records = _sync().deployed_agent_records()
        return {"records": records, "count": len(records)}
    try:
        records = _registry().list_records(
            descriptor_type=type, status=status, name=name
        )
        return {"records": records, "count": len(records)}
    except Exception as exc:
        # Registry configured but unreachable (SCP, retired namespace, outage):
        # degrade to the same fallback rather than a blank page.
        if is_registry_unavailable(exc):
            logger.warning("Registry unavailable; listing deployed agents: %s", exc)
            records = _sync().deployed_agent_records()
            return {"records": records, "count": len(records)}
        _fail(exc)


@router.get("/search")
def search_records(
    q: str = Query(..., min_length=1, max_length=256),
    type: list[str] | None = Query(None, description="descriptorType filter, repeatable"),
    _: AuthUser = Depends(current_user),
):
    try:
        records = _registry().search_records(query=q, descriptor_types=type)
        return {"records": records, "count": len(records)}
    except Exception as exc:
        _fail(exc)


@router.get("/runtimes")
def list_agent_runtimes(_: AuthUser = Depends(current_user)):
    """Runtimes offered when registering an A2A agent by hand.

    A harness's companion runtime is left out: it rejects InvokeAgentRuntime, so a
    record bound to it is an agent that can never answer. The harness itself is the
    registerable target, and it is offered by /deployed instead.
    """
    try:
        runtimes = [
            runtime
            for runtime in _registry().list_agent_runtimes()
            if not is_harness_managed_runtime(runtime)
        ]
        return {"runtimes": runtimes, "count": len(runtimes)}
    except Exception as exc:
        _fail(exc)


@router.get("/gateways")
def list_gateways(_: AuthUser = Depends(current_user)):
    """Deployed MCP gateways, offered as composable tool surfaces."""
    try:
        gateways = _sync().composable_gateways()
        return {"gateways": gateways, "count": len(gateways)}
    except Exception as exc:
        _fail(exc)


@router.get("/deployed")
def list_deployed_targets(_: AuthUser = Depends(current_user)):
    """Deployed runtimes, harnesses and gateways, annotated with registration state."""
    try:
        targets = _sync().list_deployed_targets()
        return {
            "targets": targets,
            "count": len(targets),
            # Matches what a bulk sync would register, so the UI's drift badge
            # never promises more than pressing the button delivers. Retired
            # targets need explicit intent and so are counted separately.
            "unregistered": sum(
                1 for t in targets if not t.registered and not t.reason and not t.retired
            ),
            "retired": sum(1 for t in targets if t.retired and not t.reason),
        }
    except Exception as exc:
        _fail(exc)


@router.post("/sync", response_model=SyncAgentsResponse)
def sync_deployed_agents(
    req: SyncAgentsRequest | None = None,
    _: AuthUser = Depends(require_admin),
):
    """Register deployed agents that have no registry record yet."""
    try:
        return _sync().sync(req or SyncAgentsRequest())
    except Exception as exc:
        _fail(exc)


# --- skill bundles ---------------------------------------------------------
#
# A skill is a directory, and the registry's AGENT_SKILLS descriptor holds only
# two inline strings — AWS documents its markdown as discovery metadata and says
# outright that the registry does not store the other files. So an upload is
# validated, published to `s3://SKILLS_BUCKET/skills/<name>/`, and the record is
# created pointing at that prefix. The harness fetches the prefix; nobody touches
# S3 by hand.


async def _stage(file: UploadFile) -> StagedBundle:
    body = await file.read()
    return _bundles().inspect(file.filename or "", body)


@router.post("/skills/validate", response_model=SkillBundleInfo)
async def validate_skill_bundle(
    file: UploadFile = File(...),
    _: AuthUser = Depends(current_user),
):
    """
    Check a `.md` or `.zip` against the AgentSkills spec without storing it.

    Open to any signed-in user because it writes nothing: it exists so the upload
    dialog can show what is wrong before asking to publish. A failed check is a
    200 with a populated `errors` list, not a 4xx — the caller wants the whole
    report, and "this bundle is invalid" is the answer to the question, not an
    error in asking it.
    """
    try:
        return (await _stage(file)).info
    except Exception as exc:
        _fail(exc)


# --- bucket-only skills (registry-off) ------------------------------------
#
# The registry-off half of the skills flow. When there is no registry there is no
# AGENT_SKILLS record to create, but the bundle still belongs in the skills bucket
# so the harness's bucket-skill picker (catalog.bucket_skills) can offer it. These
# read and write the bucket directly and never touch the registry. Registered
# before the `/skills/{record_id}` routes so the literal `bucket` path wins.


@router.get("/skills/bucket")
def list_bucket_skills(_: AuthUser = Depends(current_user)):
    """Skill bundles in SKILLS_BUCKET, listed without the registry — the
    registry-off counterpart to what `GET /records` does for agents."""
    try:
        skills = _bundles().list_skills()
        return {"skills": skills, "count": len(skills)}
    except Exception as exc:
        _fail(exc)


@router.post("/skills/bucket", response_model=DiscoveredSkill)
async def publish_bucket_skill(
    file: UploadFile = File(...),
    _: AuthUser = Depends(require_admin),
):
    """Publish a bundle straight to the bucket, with no registry record.

    `publish` writes `skills/<name>/` and needs no registry, so this populates the
    same store the harness picker reads when the registry is off. Name and
    description come from the SKILL.md frontmatter; a same-named bundle is
    overwritten (publish prunes what it replaced), which is the registry-off
    equivalent of replacing a record's bundle.
    """
    try:
        staged = await _stage(file)
        info = staged.info
        if not info.valid:
            raise InvalidSkillBundle(
                "번들이 AgentSkills 형식을 만족하지 않습니다.", info.errors
            )
        source = _bundles().publish(staged)
        return DiscoveredSkill(
            name=info.name, description=info.description or "", uri=source.uri
        )
    except HTTPException:
        raise
    except Exception as exc:
        _fail(exc)


@router.delete("/skills/bucket")
def delete_bucket_skill(
    uri: str = Query(..., description="The s3://.../skills/<name>/ prefix to remove"),
    _: AuthUser = Depends(require_admin),
):
    """Remove an uploaded bundle by its S3 prefix, without the registry.

    The uri is checked to be inside this server's own skills bucket, so a caller
    cannot aim delete at an arbitrary location.
    """
    bucket = _bundles().bucket
    if not bucket:
        raise HTTPException(
            status_code=503, detail="Skill storage (SKILLS_BUCKET) is not configured."
        )
    if not uri.startswith(f"s3://{bucket}/skills/"):
        raise HTTPException(status_code=400, detail=f"Not a skills-bucket uri: {uri}")
    try:
        removed = _bundles().delete_prefix(uri)
        return {"deleted": uri, "removed": removed}
    except Exception as exc:
        _fail(exc)


@router.post("/skills", response_model=RegistryRecordSummary)
async def create_skill_record(
    file: UploadFile = File(...),
    version: str | None = Form(None),
    submit_for_approval: bool = Form(True),
    _: AuthUser = Depends(require_admin),
):
    """Publish a bundle to S3 and register it as an AGENT_SKILLS record.

    Name and description come from the SKILL.md frontmatter rather than the form:
    the frontmatter is what the agent reads and what the S3 prefix is keyed on, so
    a second, editable copy could only ever disagree with it.
    """
    try:
        staged = await _stage(file)
        info = staged.info
        if not info.valid:
            raise InvalidSkillBundle(
                "번들이 AgentSkills 형식을 만족하지 않습니다.", info.errors
            )

        registry = _registry()
        clash = next(
            (
                record
                for record in registry.list_records(
                    descriptor_type=DESCRIPTOR_AGENT_SKILLS, name=info.name
                )
                if not is_deprecated(record)
            ),
            None,
        )
        if clash:
            # One prefix per skill name, so a second record of the same name would
            # silently overwrite the first one's bundle.
            raise HTTPException(
                status_code=409,
                detail=(
                    f"'{info.name}' 스킬이 이미 등록되어 있습니다. 새 레코드를 만들지 "
                    "말고 해당 레코드에서 번들을 교체해주세요."
                ),
            )

        source = _bundles().publish(staged)
        return registry.create_record(
            CreateRecordRequest(
                name=info.name,
                description=info.description,
                descriptor_type=DESCRIPTOR_AGENT_SKILLS,
                version=version or None,
                skill_markdown=info.skill_md,
                skill_source=source.model_dump(),
                submit_for_approval=submit_for_approval,
            )
        )
    except HTTPException:
        raise
    except Exception as exc:
        _fail(exc)


@router.put("/skills/{record_id:path}", response_model=RegistryRecordDetail)
async def replace_skill_bundle(
    record_id: str,
    file: UploadFile = File(...),
    _: AuthUser = Depends(require_admin),
):
    """Republish an existing skill's bundle and update its record.

    On an APPROVED record this creates a new DRAFT revision, as any edit does —
    the approved revision stays in search until a curator approves the new one.
    """
    try:
        registry = _registry()
        current = registry.get_record(record_id)
        if current.descriptor_type != DESCRIPTOR_AGENT_SKILLS:
            raise ValueError(
                f"'{current.name}' 은 스킬 레코드가 아니라 "
                f"{current.descriptor_type} 레코드입니다."
            )

        staged = await _stage(file)
        info = staged.info
        if not info.valid:
            raise InvalidSkillBundle(
                "번들이 AgentSkills 형식을 만족하지 않습니다.", info.errors
            )
        if info.name != current.name:
            # The S3 prefix is keyed on the name, so a rename here would publish to
            # a prefix the record does not point at and orphan the old one.
            raise ValueError(
                f"업로드한 SKILL.md의 name은 '{info.name}' 인데 레코드 이름은 "
                f"'{current.name}' 입니다. 이름을 바꾸려면 새 스킬로 등록해주세요."
            )

        source = _bundles().publish(staged)
        return registry.update_record(
            record_id,
            UpdateRecordRequest(
                description=info.description,
                skill_markdown=info.skill_md,
                skill_source=source.model_dump(),
            ),
        )
    except Exception as exc:
        _fail(exc)


@router.get("/skills/{record_id:path}/files", response_model=SkillFilesResponse)
def list_skill_files(record_id: str, _: AuthUser = Depends(current_user)):
    """A skill record's published bundle, listed from S3 rather than the record.

    The descriptor's file list is a snapshot from publish time; this reads the
    prefix, so it also tells the truth about a bundle written outside the app.
    """
    try:
        detail = _registry().get_record(record_id)
        source = skill_source_of(detail.descriptor_content)
        if not source:
            # An inline-markdown record: no bundle, and that is not an error.
            return SkillFilesResponse()
        files = _bundles().list_files(source["uri"])
        return SkillFilesResponse(
            uri=source["uri"],
            files=files,
            total_bytes=sum(f.size_bytes for f in files),
        )
    except Exception as exc:
        _fail(exc)


# `{record_id:path}` on every record route: a deployed-fallback id is
# `deployed:<arn>` and an ARN carries slashes (`…:harness/name`), which a plain
# path segment stops at — the route 404s and the UI spins forever.
@router.get("/records/{record_id:path}", response_model=RegistryRecordDetail)
def get_record(record_id: str, _: AuthUser = Depends(current_user)):
    # A deployed-fallback id has no registry record to fetch; answer it from the
    # same synthesis the list route uses rather than 502 against the AWS API.
    if record_id.startswith("deployed:"):
        return _deployed_detail(record_id)
    try:
        return _registry().get_record(record_id)
    except Exception as exc:
        _fail(exc)


@router.post("/records", response_model=RegistryRecordSummary)
def create_record(req: CreateRecordRequest, _: AuthUser = Depends(require_admin)):
    try:
        return _registry().create_record(req)
    except Exception as exc:
        _fail(exc)


@router.patch("/records/{record_id:path}", response_model=RegistryRecordDetail)
def update_record(
    record_id: str,
    req: UpdateRecordRequest,
    _: AuthUser = Depends(require_admin),
):
    try:
        return _registry().update_record(record_id, req)
    except Exception as exc:
        _fail(exc)


@router.post("/records/{record_id:path}/status", response_model=RegistryRecordSummary)
def update_status(
    record_id: str,
    req: UpdateStatusRequest,
    _: AuthUser = Depends(require_admin),
):
    try:
        return _registry().update_status(record_id, req.action, reason=req.reason)
    except Exception as exc:
        _fail(exc)


@router.delete("/records/{record_id:path}")
def delete_record(record_id: str, _: AuthUser = Depends(require_admin)):
    """Delete a record, and with it any bundle it was the only pointer to.

    Deprecation deliberately does not do this: a deprecated record is kept for the
    audit trail, and harnesses already composed against its prefix keep working.
    Deleting the record removes the last thing that knew where the bundle was, so
    leaving the objects behind would just be unreachable storage.
    """
    try:
        registry = _registry()
        source = None
        try:
            detail = registry.get_record(record_id)
            if detail.descriptor_type == DESCRIPTOR_AGENT_SKILLS:
                source = skill_source_of(detail.descriptor_content)
        except Exception as exc:
            # A record that cannot be read can still be deleted; losing the bundle
            # cleanup is better than refusing the delete.
            logger.warning("Could not read %s before delete: %s", record_id, exc)

        registry.delete_record(record_id)

        if source:
            try:
                removed = _bundles().delete_prefix(source["uri"])
                logger.info("Removed %d bundle object(s) for %s", removed, record_id)
            except Exception as exc:
                # The record is already gone, so this cannot fail the request.
                logger.warning(
                    "Could not remove bundle %s for %s: %s",
                    source["uri"],
                    record_id,
                    exc,
                )
        return {"deleted": record_id}
    except Exception as exc:
        _fail(exc)
