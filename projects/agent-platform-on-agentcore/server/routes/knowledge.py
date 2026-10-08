"""
Per-user knowledge base routes.

Every route depends on `current_user`: a knowledge base belongs to a Cognito
subject, so there is no meaningful anonymous behaviour here. Note the two
different "forbidden" shapes — a knowledge base the caller may not see at all is
404 (a 403 would confirm it exists), while one they may read but not modify is
403, which leaks nothing they cannot already see.
"""
import logging
import traceback
from typing import Optional

from botocore.exceptions import ClientError
from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    UploadFile,
)
from starlette.background import BackgroundTasks

from core.auth import AuthUser, current_user
from core.dependencies import knowledge_service
from models.knowledge import (
    CreateKnowledgeBaseRequest,
    KnowledgeBaseDetail,
    KnowledgeBaseRecord,
    KnowledgeDocument,
    SyncJob,
)
from services.knowledge_service import (
    KnowledgeForbidden,
    KnowledgeNotConfigured,
    KnowledgeNotFound,
    KnowledgeService,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])

# Marks the "still attached to a harness" case, which the UI turns into a
# confirm-and-force prompt rather than a plain validation error.
_ATTACHED_MARKER = "still use this knowledge base"


def _service() -> KnowledgeService:
    return knowledge_service


def _fail(exc: Exception) -> None:
    if isinstance(exc, KnowledgeNotConfigured):
        # 501, not 503: the request is fine and auth succeeded — the feature is
        # simply not provisioned in this environment.
        raise HTTPException(status_code=501, detail=str(exc))
    if isinstance(exc, KnowledgeNotFound):
        raise HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, KnowledgeForbidden):
        raise HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, ValueError):
        message = str(exc)
        if _ATTACHED_MARKER in message:
            raise HTTPException(status_code=409, detail=message)
        raise HTTPException(status_code=400, detail=message)
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {})
        raise HTTPException(
            status_code=502,
            detail=f"AWS error ({error.get('Code', 'Unknown')}): {error.get('Message', str(exc))}",
        )
    logger.error("Knowledge route error: %s", exc)
    logger.error(traceback.format_exc())
    raise HTTPException(status_code=500, detail=str(exc))


@router.get("")
def list_knowledge_bases(
    background: BackgroundTasks,
    status: Optional[str] = Query(None, description="Filter by status, e.g. READY"),
    user: AuthUser = Depends(current_user),
):
    """The caller's knowledge bases plus every shared one.

    Also restarts provisioning for anything that stalled. The UI polls this while
    a knowledge base is building, so the poll is the recovery mechanism — no
    worker process and no scheduler.
    """
    try:
        service = _service()
        records = service.list_visible(user, status=status)
        for kb_key in service.stale_keys(records):
            logger.info("Reviving stalled provisioning for %s", kb_key)
            background.add_task(service.advance, kb_key)
        return {"knowledge_bases": records, "count": len(records)}
    except Exception as exc:
        _fail(exc)


@router.post("", response_model=KnowledgeBaseRecord)
def create_knowledge_base(
    req: CreateKnowledgeBaseRequest,
    background: BackgroundTasks,
    user: AuthUser = Depends(current_user),
):
    """Record the knowledge base and start building it.

    Returns as soon as the record exists. Provisioning takes three to six
    minutes, which no HTTP request should hold open; the UI polls the listing for
    progress and that same listing restarts the work if this container dies.
    """
    try:
        record = _service().create(user, req)
        background.add_task(_service().advance, record.kb_key)
        return record
    except Exception as exc:
        _fail(exc)


@router.get("/source-buckets")
def list_source_buckets(user: AuthUser = Depends(current_user)):
    """Buckets an S3-backed knowledge base may read and platform availability.

    Declared before `/{kb_key}` so the path parameter does not swallow it. The
    `buckets` list is admin-only; `platform_available` tells the UI whether the
    platform's managed S3 source is ready for non-admin users.
    """
    try:
        return _service().source_bucket_info(user)
    except Exception as exc:
        _fail(exc)


@router.get("/{kb_key}", response_model=KnowledgeBaseDetail)
def get_knowledge_base(kb_key: str, user: AuthUser = Depends(current_user)):
    try:
        return _service().detail(user, kb_key)
    except Exception as exc:
        _fail(exc)


@router.delete("/{kb_key}")
def delete_knowledge_base(
    kb_key: str,
    force: bool = Query(False, description="Delete even if a harness uses it"),
    user: AuthUser = Depends(current_user),
):
    try:
        _service().delete(user, kb_key, force=force)
        return {"deleted": kb_key}
    except Exception as exc:
        _fail(exc)


@router.post("/{kb_key}/documents", response_model=KnowledgeDocument)
async def upload_document(
    kb_key: str,
    file: UploadFile = File(...),
    user: AuthUser = Depends(current_user),
):
    try:
        body = await file.read()
        return _service().add_document(
            user,
            kb_key,
            file.filename or "document",
            file.content_type or "",
            body,
        )
    except Exception as exc:
        _fail(exc)


@router.delete("/{kb_key}/documents/{doc_id}")
def delete_document(
    kb_key: str, doc_id: str, user: AuthUser = Depends(current_user)
):
    try:
        _service().delete_document(user, kb_key, doc_id)
        return {"deleted": doc_id}
    except Exception as exc:
        _fail(exc)


@router.post("/{kb_key}/sync", response_model=SyncJob)
def start_sync(kb_key: str, user: AuthUser = Depends(current_user)):
    """Pull the latest content from the knowledge base's source.

    Writable-only, like an upload: a sync changes what the knowledge base returns,
    so read access to a shared knowledge base is not enough.
    """
    try:
        return _service().start_sync(user, kb_key)
    except Exception as exc:
        _fail(exc)


@router.get("/{kb_key}/sync", response_model=Optional[SyncJob])
def get_sync(kb_key: str, user: AuthUser = Depends(current_user)):
    """The most recent sync, or null if the source has never been synced."""
    try:
        return _service().sync_status(user, kb_key)
    except Exception as exc:
        _fail(exc)
