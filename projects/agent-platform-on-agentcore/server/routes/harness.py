"""AgentCore Managed Agent Harness routes."""
import asyncio
import logging
import traceback

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException

from core.auth import AuthUser, current_user, require_admin
from models.harness import (
    ComposeHarnessRequest,
    ComposeHarnessResponse,
    HarnessSummary,
    UpdateHarnessRequest,
)
from services.harness_service import HarnessNotConfigured, HarnessService
from services.registry_service import RegistryNotConfigured

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/harnesses", tags=["harness"])


_service = HarnessService()

from core.dependencies import team_service  # noqa: E402 — after the singleton, like routes/registry.py

_service.teams = team_service


def _harness() -> HarnessService:
    return _service


def _fail(exc: Exception) -> None:
    if isinstance(exc, (HarnessNotConfigured, RegistryNotConfigured)):
        raise HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {})
        raise HTTPException(
            status_code=502,
            detail=f"AWS error ({error.get('Code', 'Unknown')}): {error.get('Message', str(exc))}",
        )
    logger.error("Harness route error: %s", exc)
    logger.error(traceback.format_exc())
    raise HTTPException(status_code=500, detail=str(exc))


@router.get("/catalog")
def get_catalog(_: AuthUser = Depends(current_user)):
    try:
        return _harness().catalog()
    except Exception as exc:
        _fail(exc)


@router.get("")
def list_harnesses(_: AuthUser = Depends(current_user)):
    try:
        harnesses = _harness().list_harnesses()
        return {"harnesses": harnesses, "count": len(harnesses)}
    except Exception as exc:
        _fail(exc)


@router.get("/{harness_id}", response_model=HarnessSummary)
def get_harness(harness_id: str, _: AuthUser = Depends(current_user)):
    try:
        return _harness().get_harness(harness_id)
    except Exception as exc:
        _fail(exc)


@router.post("", response_model=ComposeHarnessResponse)
def compose_harness(
    req: ComposeHarnessRequest, user: AuthUser = Depends(current_user)
):
    """Composing an agent is open to any signed-in user; deleting one is not.

    Registry publication runs server-side under this process's own credentials
    (`_spawn_registration`), so a non-admin's create still completes end to end
    without `POST /api/registry/sync`, which stays admin-only because it writes
    records for every unregistered runtime in the account, not just this one.

    The team (and so the execution role) is decided server-side from the
    caller's groups; see HarnessService.resolve_team.
    """
    try:
        return _harness().compose_and_register(req, caller=user)
    except Exception as exc:
        _fail(exc)


@router.put("/{harness_id}", response_model=HarnessSummary)
def update_harness(
    harness_id: str,
    req: UpdateHarnessRequest,
    _: AuthUser = Depends(require_admin),
):
    """Edit in place. Admin-only, like delete and for the same reason: harnesses
    carry no owner, so any caller could rewrite anyone's system prompt or tool
    set — and unlike compose, that changes an agent other people already use.
    The harness ARN does not change, so the registry record
    bound to it stays valid; AgentCore versions the definition and moves the
    DEFAULT endpoint once the new version is READY (about two minutes,
    measured). The response is the UPDATING summary; the client polls GET.
    """
    try:
        return _harness().update_harness(harness_id, req)
    except Exception as exc:
        _fail(exc)


@router.delete("/{harness_id}")
async def delete_harness(harness_id: str, user: AuthUser = Depends(require_admin)):
    """Admin-only: harnesses carry no owner, so any caller could delete anyone's.

    The Harness's Slack integration is cleaned up first. If that cannot finish,
    the Harness stays (409): the place to retry the cleanup is that Harness's
    Slack dialog, which would be gone with it. `async` only for that await; the
    blocking delete itself runs in a worker thread.
    """
    from core.dependencies import slack_service
    from integrations.slack.service import DeletionIncomplete

    try:
        await slack_service.cleanup_for_harness(harness_id, user)
    except DeletionIncomplete as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Slack 연동 정리가 끝나지 않아 Harness를 삭제하지 않았습니다. Slack 연동 화면에서 삭제를 다시 시도해 주세요.",
                "steps": exc.steps,
            },
        )
    except Exception as exc:
        logger.error("Slack cleanup before Harness delete failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=409,
            detail="Slack 연동 정리가 끝나지 않아 Harness를 삭제하지 않았습니다. Slack 연동 화면에서 삭제를 다시 시도해 주세요.",
        )
    try:
        deprecated = await asyncio.to_thread(_harness().delete_harness, harness_id)
        return {"deleted": harness_id, "deprecated_records": deprecated}
    except Exception as exc:
        _fail(exc)
