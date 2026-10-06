"""Artifact routes — read, download and share agent-produced artifacts.

Every route depends on `current_user`, `/share` included: minting a presigned
URL grants nothing the caller could not already read through this same router,
so gating it on admin would only break the panel's share button for everyone
else.

An artifact is produced inside a thread and inherits that thread's ownership, so
each route resolves the owning thread and defers to `ThreadService.require_owned`
— see routes/threads.py. The artifact id itself is not a capability: it is
derived from the thread id (via `scoped_id` in artifact_service), and every
version record carries `thread_id`, which is what makes the check cheap here.
"""
import logging
import traceback
from typing import List

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, Query, Response

from core.auth import AuthUser, current_user
from core.dependencies import artifact_service, thread_service
from models.artifact import (
    ArtifactContent,
    ArtifactDetail,
    ArtifactShareResponse,
    ArtifactVersion,
)
from services.artifact_service import (
    ArtifactNotFound,
    ArtifactNotText,
    ArtifactsNotConfigured,
    content_disposition,
)
from services.thread_service import ThreadForbidden, ThreadNotFound

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/artifacts", tags=["artifacts"])


def _require_thread(thread_id: str, user: AuthUser) -> None:
    """Refuse unless the caller may use the thread the artifact belongs to.

    404 for a thread that has gone: the artifact rows outlive a deleted thread,
    and an orphan is not something the caller may read either.
    """
    try:
        thread_service.require_owned(thread_id, user.sub, is_admin=user.is_admin)
    except ThreadNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ThreadForbidden as exc:
        raise HTTPException(status_code=403, detail=str(exc))


def _require_artifact_thread(artifact_id: str, user: AuthUser) -> None:
    """Same check, for the routes addressed by artifact id rather than thread id."""
    _require_thread(artifact_service.thread_of(artifact_id), user)


def _fail(exc: Exception) -> None:
    # HTTPException first: _require_thread raises it from inside the handlers'
    # try blocks, and without this it would be flattened into a 500.
    if isinstance(exc, HTTPException):
        raise exc
    if isinstance(exc, ArtifactNotText):
        # 415, not 500: the artifact exists and the caller may read it — just not
        # as text.
        raise HTTPException(status_code=415, detail=str(exc))
    if isinstance(exc, ArtifactsNotConfigured):
        raise HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, ArtifactNotFound):
        raise HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {})
        raise HTTPException(
            status_code=502,
            detail=f"AWS error ({error.get('Code', 'Unknown')}): {error.get('Message', str(exc))}",
        )
    logger.error("Artifact route error: %s", exc)
    logger.error(traceback.format_exc())
    raise HTTPException(status_code=500, detail=str(exc))


@router.get("/thread/{thread_id}", response_model=List[ArtifactVersion])
async def list_thread_artifacts(
    thread_id: str, user: AuthUser = Depends(current_user)
):
    """Every artifact version produced in a thread, newest first."""
    try:
        _require_thread(thread_id, user)
        return artifact_service.list_for_thread(thread_id)
    except Exception as exc:
        _fail(exc)


@router.get("/{artifact_id}", response_model=ArtifactDetail)
async def get_artifact(artifact_id: str, user: AuthUser = Depends(current_user)):
    try:
        _require_artifact_thread(artifact_id, user)
        return artifact_service.detail(artifact_id)
    except Exception as exc:
        _fail(exc)


@router.get("/{artifact_id}/versions/{version}/content", response_model=ArtifactContent)
async def get_artifact_content(
    artifact_id: str, version: int, user: AuthUser = Depends(current_user)
):
    try:
        _require_artifact_thread(artifact_id, user)
        return artifact_service.content(artifact_id, version)
    except Exception as exc:
        _fail(exc)


@router.post("/{artifact_id}/versions/{version}/share", response_model=ArtifactShareResponse)
async def share_artifact(
    artifact_id: str,
    version: int,
    expires_in: int = Query(604800, ge=60, le=604800),
    download: bool = Query(False),
    user: AuthUser = Depends(current_user),
):
    """Presigned S3 URL for one artifact version."""
    try:
        _require_artifact_thread(artifact_id, user)
        url, ttl = artifact_service.share_url(
            artifact_id, version, expires_in=expires_in, download=download
        )
        return ArtifactShareResponse(
            artifact_id=artifact_id, version=version, url=url, expires_in=ttl
        )
    except Exception as exc:
        _fail(exc)


@router.get("/{artifact_id}/versions/{version}/download")
async def download_artifact(
    artifact_id: str, version: int, user: AuthUser = Depends(current_user)
):
    """The artifact's bytes, named for saving.

    Serving through this route rather than a presigned URL keeps the ownership
    check on the read path; `/share` remains the way to hand a link to someone
    who is not the owner.
    """
    try:
        _require_artifact_thread(artifact_id, user)
        body, filename, content_type = artifact_service.binary_body(
            artifact_id, version
        )
        return Response(
            content=body,
            media_type=content_type,
            headers={
                "Content-Disposition": content_disposition(filename, artifact_id)
            },
        )
    except Exception as exc:
        _fail(exc)


@router.get("/{artifact_id}/versions/{version}/preview")
async def get_artifact_preview(
    artifact_id: str, version: int, user: AuthUser = Depends(current_user)
):
    """The extracted preview for a binary artifact, if one exists.

    404 rather than an empty body when there is none: "no preview" is the normal
    state for .zip and .pdf, and the panel decides what to show from the status.
    """
    try:
        _require_artifact_thread(artifact_id, user)
        preview = artifact_service.preview(artifact_id, version)
        if not preview:
            raise HTTPException(status_code=404, detail="No preview for this artifact")
        return preview
    except Exception as exc:
        _fail(exc)
