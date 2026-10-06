"""
HTTP contract for the attachment endpoints.

Three things the UI depends on: the status codes it branches on (503 when storage
is unconfigured, 400 for a rejected format, 413 for oversize), that upload
requires an authenticated caller like every other thread route, and that a fetch
comes back with a usable Content-Type.
"""
import io
import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.auth as auth  # noqa: E402
import routes.threads as thread_routes  # noqa: E402
from models.attachment import Attachment, UnsupportedAttachment  # noqa: E402
from models.thread import Thread  # noqa: E402
from services.attachment_service import (  # noqa: E402
    AttachmentNotFound,
    AttachmentsNotConfigured,
)

ALICE = auth.AuthUser(sub="user-1", username="alice", groups=[])
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class StubService:
    def __init__(self, raises=None):
        self.raises = raises
        self.stored = []

    def store(self, thread_id, filename, body):
        if self.raises:
            raise self.raises
        self.stored.append((thread_id, filename, len(body)))
        return Attachment(
            attachment_id="att_abc123def456",
            filename=filename,
            media_type="image/png",
            size_bytes=len(body),
            kind="image",
        )

    def fetch(self, thread_id, attachment_id):
        if self.raises:
            raise self.raises
        return PNG, "image/png", "shot.png"


class StubThreadService:
    """Says every thread belongs to the caller.

    Both attachment routes check thread ownership first, so without this the
    real thread_service would run and these tests would fail on a DynamoDB
    lookup instead of exercising the status codes they are about. Refusals are
    covered in test_thread_ownership.py.
    """

    def require_owned(self, thread_id, owner_sub, is_admin=False):
        return Thread(
            thread_id=thread_id,
            created_at="2026-08-07T00:00:00",
            updated_at="2026-08-07T00:00:00",
            owner_sub=owner_sub,
        )


def client_with(service):
    app = FastAPI()
    app.include_router(thread_routes.router)
    app.dependency_overrides[auth.current_user] = lambda: ALICE
    thread_routes.attachment_service = service
    thread_routes.thread_service = StubThreadService()
    return TestClient(app)


def upload(service, filename="shot.png", body=PNG):
    return client_with(service).post(
        "/threads/t-1/attachments",
        files={"file": (filename, io.BytesIO(body), "image/png")},
    )


def test_upload_returns_the_reference_fields():
    response = upload(StubService())

    assert response.status_code == 200
    body = response.json()
    assert body["attachment_id"] == "att_abc123def456"
    assert body["kind"] == "image"
    assert body["filename"] == "shot.png"


def test_upload_passes_the_thread_and_filename_through():
    service = StubService()
    upload(service)

    assert service.stored == [("t-1", "shot.png", len(PNG))]


def test_unconfigured_storage_is_a_503():
    """Matches the artifact routes, so the UI can branch on one code."""
    response = upload(StubService(raises=AttachmentsNotConfigured("no bucket")))

    assert response.status_code == 503


def test_a_rejected_format_is_a_400_with_a_readable_reason():
    response = upload(
        StubService(raises=UnsupportedAttachment("Unsupported file type 'exe'.")),
        filename="payload.exe",
    )

    assert response.status_code == 400
    assert "exe" in response.json()["detail"]


def test_an_oversize_file_is_a_413():
    """A distinct code, because the UI's message for it is different."""
    response = upload(StubService(raises=UnsupportedAttachment("'big.png' is too large (9000 KB).")))

    assert response.status_code == 413


def test_fetch_returns_the_bytes_with_a_content_type():
    response = client_with(StubService()).get("/threads/t-1/attachments/att_abc123def456")

    assert response.status_code == 200
    assert response.content == PNG
    assert response.headers["content-type"] == "image/png"


def test_a_missing_attachment_is_a_404():
    response = client_with(StubService(raises=AttachmentNotFound("gone"))).get(
        "/threads/t-1/attachments/att_gone"
    )

    assert response.status_code == 404


def test_upload_requires_authentication():
    """No dependency override: the route must reject an anonymous caller."""
    app = FastAPI()
    app.include_router(thread_routes.router)
    thread_routes.attachment_service = StubService()

    response = TestClient(app).post(
        "/threads/t-1/attachments",
        files={"file": ("shot.png", io.BytesIO(PNG), "image/png")},
    )

    assert response.status_code in (401, 403)


class KoreanNameService(StubService):
    def fetch(self, thread_id, attachment_id):
        return PNG, "image/png", "보고서.png"


def test_a_non_ascii_filename_does_not_break_the_response():
    """HTTP headers are latin-1.

    Putting a non-ASCII filename straight into `filename=` makes the ASGI server
    raise UnicodeEncodeError, turning the download into a 500. RFC 5987's
    `filename*` carries the real name; an ASCII `filename` stays for clients that
    ignore the extended form.
    """
    response = client_with(KoreanNameService()).get(
        "/threads/t-1/attachments/att_abc123def456"
    )

    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert "filename*=UTF-8''%EB%B3%B4%EA%B3%A0%EC%84%9C.png" in disposition
    # The fallback must still name something useful, not just ".png".
    assert 'filename="att_abc123def456.png"' in disposition


def test_an_ascii_filename_keeps_its_plain_form():
    response = client_with(StubService()).get(
        "/threads/t-1/attachments/att_abc123def456"
    )

    assert 'filename="shot.png"' in response.headers["content-disposition"]
