"""
HTTP contract for /api/knowledge.

Three things the UI depends on and that unit tests below the route cannot show:
the status mapping (404 for someone else's knowledge base, 403 for read-only
access to a shared one, 501 when the feature is unconfigured), that creating a
knowledge base schedules provisioning as a background task instead of blocking
the response, and that listing revives whatever stalled.
"""
import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.auth as auth  # noqa: E402
import routes.knowledge as knowledge_routes  # noqa: E402
from models.knowledge import (  # noqa: E402
    STATUS_CREATING,
    STATUS_READY,
    KnowledgeBaseDetail,
    KnowledgeBaseRecord,
    KnowledgeDocument,
    SyncJob,
    now_iso,
)
from services.knowledge_service import (  # noqa: E402
    KnowledgeForbidden,
    KnowledgeNotConfigured,
    KnowledgeNotFound,
)

ALICE = auth.AuthUser(sub="user-1", username="alice", groups=[])
KB_KEY = "kb_docs_a1b2c3d4"


def record(status=STATUS_READY):
    return KnowledgeBaseRecord(
        kb_key=KB_KEY, owner_id=ALICE.sub, owner_name="alice", name="Docs",
        status=status, created_at=now_iso(), updated_at=now_iso(),
    )


class StubService:
    def __init__(self, error=None, records=None, stale=None, source_buckets=None,
                 sync=None):
        self.error = error
        self.records = records if records is not None else [record()]
        self.stale = stale or []
        self.advanced = []
        self.created = []
        self.uploads = []
        self.deleted = []
        self.deleted_documents = []
        self.created_sources = []
        self.synced = []
        self.source_buckets = list(source_buckets or [])
        self.sync = sync
        self.list_status = None

    def _boom(self):
        if self.error:
            raise self.error

    def list_visible(self, user, status=None):
        self._boom()
        self.list_status = status
        return list(self.records)

    def stale_keys(self, records):
        return list(self.stale)

    def advance(self, kb_key):
        self.advanced.append(kb_key)

    def create(self, user, req):
        self._boom()
        self.created.append((user.sub, req.name, req.shared))
        self.created_sources.append((req.source_type, dict(req.source_config)))
        return record(status=STATUS_CREATING)

    def detail(self, user, kb_key):
        self._boom()
        return KnowledgeBaseDetail(
            knowledge_base=record(),
            documents=[KnowledgeDocument(doc_id="a.txt_11111111", filename="a.txt",
                                         status="INDEXED")],
        )

    def add_document(self, user, kb_key, filename, content_type, body):
        self._boom()
        self.uploads.append((kb_key, filename, content_type, len(body)))
        return KnowledgeDocument(
            doc_id="a.txt_11111111", filename="a.txt", status="IN_PROGRESS"
        )

    def delete_document(self, user, kb_key, doc_id):
        self._boom()
        self.deleted_documents.append((kb_key, doc_id))

    def delete(self, user, kb_key, force=False):
        self._boom()
        self.deleted.append((kb_key, force))

    def start_sync(self, user, kb_key):
        self._boom()
        self.synced.append(kb_key)
        return SyncJob(job_id="job1", status="STARTING")

    def sync_status(self, user, kb_key):
        self._boom()
        return self.sync

    def source_bucket_info(self, user):
        self._boom()
        return {
            "buckets": list(self.source_buckets),
            "platform_available": True,
        }


@pytest.fixture
def client(monkeypatch):
    def build(**kwargs):
        service = StubService(**kwargs)
        monkeypatch.setattr(knowledge_routes, "_service", lambda: service)
        app = FastAPI()
        app.include_router(knowledge_routes.router)
        # Token verification has its own tests; these are about the HTTP contract.
        app.dependency_overrides[auth.current_user] = lambda: ALICE
        return TestClient(app, raise_server_exceptions=False), service

    return build


def test_the_listing_returns_records_with_a_count(client):
    http, _ = client()

    response = http.get("/api/knowledge")

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["knowledge_bases"][0]["kb_key"] == KB_KEY


def test_the_listing_passes_the_status_filter_through(client):
    """The harness composer asks for READY only."""
    http, service = client()

    http.get("/api/knowledge?status=READY")

    assert service.list_status == STATUS_READY


def test_the_listing_revives_stalled_provisioning(client):
    """Polling the list is the recovery path for a driver that died."""
    http, service = client(stale=["kb_stalled_000001"])

    http.get("/api/knowledge")

    assert service.advanced == ["kb_stalled_000001"]


def test_creating_returns_immediately_and_provisions_in_the_background(client):
    http, service = client()

    response = http.post("/api/knowledge", json={"name": "Product Docs"})

    assert response.status_code == 200
    assert response.json()["status"] == STATUS_CREATING
    # TestClient runs background tasks after the response is sent.
    assert service.advanced == [KB_KEY]


def test_creating_forwards_the_shared_flag_so_the_service_can_refuse_it(client):
    http, service = client()

    http.post("/api/knowledge", json={"name": "Docs", "shared": True})

    assert service.created == [(ALICE.sub, "Docs", True)]


def test_a_non_admin_asking_for_shared_is_403(client):
    http, _ = client(error=KnowledgeForbidden("admin only"))

    assert http.post("/api/knowledge", json={"name": "D", "shared": True}).status_code == 403


def test_an_invalid_name_is_400(client):
    http, _ = client(error=ValueError("Knowledge base name is required."))

    assert http.post("/api/knowledge", json={"name": " "}).status_code == 400


def test_detail_includes_the_documents(client):
    http, _ = client()

    response = http.get(f"/api/knowledge/{KB_KEY}")

    assert response.status_code == 200
    assert response.json()["documents"][0]["filename"] == "a.txt"


def test_someone_elses_knowledge_base_is_404(client):
    http, _ = client(error=KnowledgeNotFound("nope"))

    assert http.get(f"/api/knowledge/{KB_KEY}").status_code == 404


def test_read_only_access_to_a_shared_knowledge_base_is_403_on_write(client):
    """403 is safe here: the caller can already see that it exists."""
    http, _ = client(error=KnowledgeForbidden("read only"))

    response = http.post(
        f"/api/knowledge/{KB_KEY}/documents",
        files={"file": ("a.txt", b"hi", "text/plain")},
    )

    assert response.status_code == 403


def test_an_upload_reaches_the_service_with_its_filename_and_type(client):
    http, service = client()

    response = http.post(
        f"/api/knowledge/{KB_KEY}/documents",
        files={"file": ("Q3 report.pdf", b"%PDF-1.7", "application/pdf")},
    )

    assert response.status_code == 200
    assert service.uploads == [(KB_KEY, "Q3 report.pdf", "application/pdf", 8)]


def test_an_unsupported_upload_is_400_not_500(client):
    http, _ = client(error=ValueError("Unsupported file type video/mp4"))

    response = http.post(
        f"/api/knowledge/{KB_KEY}/documents",
        files={"file": ("c.mp4", b"\x00", "video/mp4")},
    )

    assert response.status_code == 400
    assert "video/mp4" in response.json()["detail"]


def test_deleting_a_document(client):
    http, service = client()

    response = http.delete(f"/api/knowledge/{KB_KEY}/documents/a.txt_11111111")

    assert response.status_code == 200
    assert service.deleted_documents == [(KB_KEY, "a.txt_11111111")]


def test_deleting_a_knowledge_base(client):
    http, service = client()

    response = http.delete(f"/api/knowledge/{KB_KEY}")

    assert response.status_code == 200
    assert response.json()["deleted"] == KB_KEY
    assert service.deleted == [(KB_KEY, False)]


def test_force_is_forwarded(client):
    http, service = client()

    http.delete(f"/api/knowledge/{KB_KEY}?force=true")

    assert service.deleted == [(KB_KEY, True)]


def test_an_attached_knowledge_base_is_409_so_the_ui_can_offer_force(client):
    """A plain 400 would read as "bad request" rather than "confirm and retry"."""
    http, _ = client(error=ValueError("support_bot still use this knowledge base."))

    response = http.delete(f"/api/knowledge/{KB_KEY}")

    assert response.status_code == 409
    assert "support_bot" in response.json()["detail"]


def test_an_unconfigured_server_is_501_on_every_route(client):
    http, _ = client(error=KnowledgeNotConfigured("not configured"))

    assert http.get("/api/knowledge").status_code == 501
    assert http.get(f"/api/knowledge/{KB_KEY}").status_code == 501
    assert http.post("/api/knowledge", json={"name": "D"}).status_code == 501


def test_an_aws_error_is_502_with_the_code_in_the_detail(client):
    from botocore.exceptions import ClientError

    http, _ = client(error=ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "Ingest"
    ))

    response = http.get("/api/knowledge")

    assert response.status_code == 502
    assert "ThrottlingException" in response.json()["detail"]


def test_the_source_bucket_listing_is_not_swallowed_by_the_key_route(client):
    """`/source-buckets` sits where `/{kb_key}` would match it."""
    http, _ = client(source_buckets=["corp-docs", "team-exports"])

    response = http.get("/api/knowledge/source-buckets")

    assert response.status_code == 200
    assert response.json() == {"buckets": ["corp-docs", "team-exports"], "platform_available": True}


def test_the_source_bucket_listing_is_empty_when_none_are_registered(client):
    http, _ = client()

    assert http.get("/api/knowledge/source-buckets").json() == {"buckets": [], "platform_available": True}


def test_creating_passes_the_source_type_through(client):
    http, service = client()

    http.post("/api/knowledge", json={
        "name": "Corp", "source_type": "S3",
        "source_config": {"bucket_name": "corp-docs", "prefix": "exports/"},
    })

    assert service.created_sources == [
        ("S3", {"bucket_name": "corp-docs", "prefix": "exports/"})
    ]


def test_creating_without_a_source_type_still_works(client):
    """The shape every caller written before source types existed sends."""
    http, service = client()

    response = http.post("/api/knowledge", json={"name": "Docs"})

    assert response.status_code == 200
    assert service.created_sources == [("UPLOAD", {})]


def test_sync_returns_the_job(client):
    http, service = client()

    response = http.post(f"/api/knowledge/{KB_KEY}/sync")

    assert response.status_code == 200
    assert response.json()["status"] == "STARTING"
    assert service.synced == [KB_KEY]


def test_reading_sync_before_the_first_one_returns_null(client):
    http, _ = client()

    response = http.get(f"/api/knowledge/{KB_KEY}/sync")

    assert response.status_code == 200
    assert response.json() is None


def test_reading_sync_reports_the_statistics(client):
    http, _ = client(sync=SyncJob(
        job_id="job1", status="COMPLETE", documents_scanned=12,
        documents_indexed=3,
    ))

    body = http.get(f"/api/knowledge/{KB_KEY}/sync").json()

    assert body["documents_scanned"] == 12
    assert body["documents_indexed"] == 3


def test_syncing_the_wrong_source_type_is_a_400(client):
    """The service refuses it; this pins the status the UI sees."""
    http, _ = client(error=ValueError("nothing to synchronise"))

    assert http.post(f"/api/knowledge/{KB_KEY}/sync").status_code == 400


def test_uploading_to_the_wrong_source_type_is_a_400(client):
    http, _ = client(error=ValueError("cannot upload files to"))

    response = http.post(
        f"/api/knowledge/{KB_KEY}/documents",
        files={"file": ("a.txt", b"hello", "text/plain")},
    )

    assert response.status_code == 400


def test_the_routes_require_a_caller(monkeypatch):
    """Without the override, an anonymous request must not reach the service."""
    monkeypatch.setattr(knowledge_routes, "_service", lambda: StubService())
    monkeypatch.setattr(auth.config, "AUTH_ENFORCED", True, raising=False)
    monkeypatch.setattr(auth.config, "COGNITO_USER_POOL_ID", "us-east-1_pool", raising=False)
    app = FastAPI()
    app.include_router(knowledge_routes.router)
    http = TestClient(app, raise_server_exceptions=False)

    assert http.get("/api/knowledge").status_code == 401


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
