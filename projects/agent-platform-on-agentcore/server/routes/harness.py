"""AgentCore Managed Agent Harness routes."""
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
    req: ComposeHarnessRequest, _: AuthUser = Depends(current_user)
):
    """Composing an agent is open to any signed-in user; deleting one is not.

    Registry publication runs server-side under this process's own credentials
    (`_spawn_registration`), so a non-admin's create still completes end to end
    without `POST /api/registry/sync`, which stays admin-only because it writes
    records for every unregistered runtime in the account, not just this one.
    """
    try:
        return _harness().compose_and_register(req)
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
def delete_harness(harness_id: str, _: AuthUser = Depends(require_admin)):
    """Admin-only: harnesses carry no owner, so any caller could delete anyone's."""
    try:
        deprecated = _harness().delete_harness(harness_id)
        return {"deleted": harness_id, "deprecated_records": deprecated}
    except Exception as exc:
        _fail(exc)
