"""
Thread routes.

Every route depends on `current_user`, including the writes: creating a thread
and streaming a run *are* an ordinary user's chat, so `require_admin` here would
leave non-admins unable to start a conversation at all.

A session alone is not enough, though — a thread belongs to the Cognito `sub`
that created it, and every route below is scoped through
`ThreadService.require_owned`: 404 for a thread that does not exist, 403 for one
that belongs to someone else. The attachment routes are included because their
bytes are addressed by thread id, so leaving them ungated would leak the files
out of an otherwise closed thread.

Admin sees and may delete everything, matching knowledge bases. The one
exception is streaming a run: that is not administration and it would write
another user's conversation into the admin's own AgentCore Memory scope — see
`ThreadService.get_or_create_thread`.

Streaming carries a second check on a different axis: a thread is pinned to one
registry agent, and a turn addressed to another is 409. Ownership asks *who* may
speak; this asks *which agent* may answer, and mixing two would collide their
AgentCore Memory scopes — see `Thread.agent_record_id`.

test_thread_ownership.py is the regression guard. test_route_auth.py pins the
auth dependency of each route but says nothing about ownership; a new route here
needs a case in both.
"""
import json
import unicodedata
from urllib.parse import quote
from fastapi import APIRouter, Depends, HTTPException, Query, Body, Request, File, UploadFile
from fastapi.responses import StreamingResponse, Response
from typing import Optional, Dict, Any
from models import (
    Thread,
    ThreadStatus,
    ThreadStateUpdate,
    StreamRequest,
)
from core.auth import AuthUser, current_user
from core.dependencies import (
    thread_service,
    streaming_service,
    attachment_service,
    browser_screenshot_service,
)
from models.attachment import Attachment, UnsupportedAttachment
from services.attachment_service import AttachmentNotFound, AttachmentsNotConfigured
from services.browser_screenshot_service import (
    ScreenshotNotFound,
    ScreenshotsNotConfigured,
)
from services.streaming_service import AgentNotApproved
from services.agent_access import AgentTargetMismatch, BasicChatUnavailable
from services.thread_service import (
    ThreadAgentMismatch,
    ThreadForbidden,
    ThreadNotFound,
)
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/threads", tags=["threads"])


def _owned(thread_id: str, user: AuthUser) -> Thread:
    """A thread this caller may use, or the matching HTTP error.

    Every handler starts here rather than each translating the two exceptions
    itself, so a route cannot accidentally report "not found" for a thread it
    refused on ownership grounds, or vice versa.

    Admin passes, on writes as well as reads.
    """
    try:
        return thread_service.require_owned(
            thread_id, user.sub, is_admin=user.is_admin
        )
    except ThreadNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ThreadForbidden as exc:
        raise HTTPException(status_code=403, detail=str(exc))


#
# The handlers below are deliberately plain `def`, not `async def`: every one of
# them calls straight into blocking boto3. Declared `async`, they run *on* the
# event loop, so a single DynamoDB scan stalls every other request in the process
# — concurrent /threads calls serialised at ~200ms each and degraded to ~5s under
# load, which is what made the thread list hang while a chat was streaming. As
# plain `def`, FastAPI dispatches them to its threadpool instead. Handlers that
# genuinely await (the streaming ones) stay `async def`.
#


@router.get("/{thread_id}")
def get_thread(
    thread_id: str,
    user: AuthUser = Depends(current_user),
):
    """Get thread by ID"""
    return _owned(thread_id, user)


@router.get("")
def search_threads(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    sort_by: str = Query("updated_at", regex="^(updated_at|created_at)$"),
    sort_order: str = Query("desc", regex="^(asc|desc)$"),
    status: Optional[ThreadStatus] = Query(None),
    metadata: Optional[str] = Query(None),  # JSON string
    user: AuthUser = Depends(current_user),
):
    """The caller's own threads — every thread, for an admin"""
    try:
        metadata_dict = json.loads(metadata) if metadata else {}
    except json.JSONDecodeError:
        metadata_dict = {}

    threads = thread_service.search_threads(
        # None means unscoped. Admin sees the whole table, so the sidebar
        # matches what the per-thread routes will actually open.
        owner_sub=None if user.is_admin else user.sub,
        limit=limit,
        offset=offset,
        sort_by=sort_by,
        sort_order=sort_order,
        status=status,
        metadata=metadata_dict,
    )

    # Return as list (LangGraph SDK expects a list)
    return threads


@router.post("")
def create_thread(
    thread_data: Optional[Dict[str, Any]] = Body(None),
    user: AuthUser = Depends(current_user),
):
    """Create a new thread owned by the caller"""
    # The owner comes from the verified token, never from thread_data: the body
    # is stored as the thread's `values` and is entirely caller-controlled.
    return thread_service.create_thread(
        owner_sub=user.sub,
        thread_data=thread_data,
    )


@router.get("/{thread_id}/state")
def get_thread_state(
    thread_id: str,
    user: AuthUser = Depends(current_user),
):
    """Get thread state"""
    thread = _owned(thread_id, user)

    return {
        "values": thread.values or {},
        "metadata": thread.metadata or {},
    }


@router.patch("/{thread_id}/state")
def update_thread_state(
    thread_id: str,
    update: ThreadStateUpdate,
    user: AuthUser = Depends(current_user),
):
    """Update thread state"""
    _owned(thread_id, user)
    try:
        thread = thread_service.update_thread_state(thread_id, update)
        return thread
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


async def _stream(thread_id: str, request: StreamRequest, user: AuthUser):
    """Shared body of the two streaming routes.

    Not `_owned`: the client chooses the thread id and a first turn legitimately
    creates it, so the check lives in `get_or_create_thread` — unknown id means
    "make it, owned by the caller", not 404.
    """
    try:
        return await streaming_service.stream_thread_execution(
            thread_id,
            request,
            # `sub`, not `username`: Memory scopes are long-lived and a
            # reassigned username would inherit the previous account's memory.
            actor_id=user.sub or None,
            owner_sub=user.sub,
        )
    except ThreadForbidden as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except AgentNotApproved as exc:
        # Also 403, but about the agent rather than the caller: only an APPROVED
        # registry record may be pinned to a new thread. The UI keeps unapproved
        # agents out of the picker; this holds the same line against a request
        # that names the record id directly.
        raise HTTPException(status_code=403, detail=str(exc))
    except (AgentTargetMismatch, BasicChatUnavailable) as exc:
        # The ARNs or model the request named are not the bound agent's. The
        # server derives the target itself (services/agent_access.py); a client
        # that disagrees is trying to reach something it was not given.
        raise HTTPException(status_code=403, detail=str(exc))
    except ThreadAgentMismatch as exc:
        # 409, not 403: the caller owns this thread, so it is a conflict with the
        # thread's state rather than a permission failure. The client turns this
        # into "start a new chat", which is the only resolution.
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/{thread_id}/runs/stream")
async def stream_thread_run(
    thread_id: str,
    request: StreamRequest = Body(...),
    user: AuthUser = Depends(current_user),
):
    """Stream thread execution (Server-Sent Events) - LangGraph SDK compatible endpoint"""
    return await _stream(thread_id, request, user)


@router.post("/{thread_id}/stream")
async def stream_thread_direct(
    thread_id: str,
    request: StreamRequest,
    user: AuthUser = Depends(current_user),
):
    """Stream thread execution (direct endpoint)"""
    return await _stream(thread_id, request, user)


@router.delete("/{thread_id}")
def delete_thread(
    thread_id: str,
    user: AuthUser = Depends(current_user),
):
    """Delete a thread"""
    _owned(thread_id, user)
    try:
        thread_service.delete_thread(thread_id)
        return {"status": "deleted"}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{thread_id}/attachments", response_model=Attachment)
async def upload_attachment(
    thread_id: str,
    file: UploadFile = File(...),
    user: AuthUser = Depends(current_user),
):
    """Store one attachment and return the reference the message will carry.

    `async def` here unlike its neighbours: this handler genuinely awaits the
    upload body. The S3 write inside is blocking but brief.
    """
    _owned(thread_id, user)
    body = await file.read()
    try:
        return attachment_service.store(thread_id, file.filename or "file", body)
    except AttachmentsNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except UnsupportedAttachment as exc:
        # Oversize gets its own code: the UI says something different for it.
        status = 413 if "too large" in str(exc).lower() else 400
        raise HTTPException(status_code=status, detail=str(exc))


@router.get("/{thread_id}/attachments/{attachment_id}")
def get_attachment(
    thread_id: str,
    attachment_id: str,
    user: AuthUser = Depends(current_user),
):
    """Stream an attachment back for a thumbnail or a download."""
    _owned(thread_id, user)
    try:
        body, media_type, filename = attachment_service.fetch(thread_id, attachment_id)
    except AttachmentsNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except AttachmentNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    # HTTP headers are latin-1, so a non-ASCII filename cannot go in `filename=`
    # — the ASGI server raises UnicodeEncodeError and the request becomes a 500.
    # RFC 5987's `filename*` carries the real name UTF-8 percent-encoded, with an
    # ASCII `filename` kept alongside for clients that ignore the extended form.
    stripped = (
        unicodedata.normalize("NFKD", filename)
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    # A wholly non-ASCII name leaves nothing but the extension, so fall back to
    # the attachment id rather than serving a file called ".png". Every current
    # browser prefers `filename*` anyway; this is for the ones that do not.
    stem = stripped.rsplit(".", 1)[0] if "." in stripped else stripped
    ascii_fallback = stripped if stem else f"{attachment_id}{stripped}"
    disposition = (
        f'inline; filename="{ascii_fallback}"; '
        f"filename*=UTF-8''{quote(filename, safe='')}"
    )

    return Response(
        content=body,
        media_type=media_type,
        headers={"Content-Disposition": disposition},
    )


@router.get("/{thread_id}/browser-screenshots/{tool_call_id}")
def get_browser_screenshot(
    thread_id: str,
    tool_call_id: str,
    user: AuthUser = Depends(current_user),
):
    """The PNG a `browser_screenshot` tool call captured, by tool call id.

    Addressed by tool call rather than by S3 key so that owning the thread is the
    only thing the caller needs to prove — see browser_screenshot_service. The
    key lives in the thread's own stored messages, which is also why this only
    answers once the turn has been persisted: mid-stream the UI still has the
    Lambda's presigned URL and uses that.
    """
    thread = _owned(thread_id, user)
    try:
        body = browser_screenshot_service.fetch(
            (thread.values or {}).get("messages") or [], tool_call_id
        )
    except ScreenshotsNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except ScreenshotNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    # No Content-Disposition: this is rendered inline in a chat bubble, never
    # downloaded, and the filename is a millisecond timestamp nobody wants.
    return Response(content=body, media_type="image/png")
