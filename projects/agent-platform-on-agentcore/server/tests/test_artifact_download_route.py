"""Downloading a binary artifact, header encoding included.

This test drives the route through a real ASGI stack, which is the only way the
`Content-Disposition` encoding is actually exercised: header values are latin-1,
and the existing route tests stub the service and never encode a real header — the
reason two Korean-filename 500s shipped green before.
"""
import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.fixture
def client(monkeypatch):
    import core.dependencies as dependencies
    import routes.artifacts as artifacts_route
    from core.auth import AuthUser, current_user
    from services.artifact_service import ArtifactNotText

    class StubArtifacts:
        def __init__(self):
            self.binary = True

        def thread_of(self, artifact_id):
            return "t1"

        def binary_body(self, artifact_id, version):
            return b"PK\x03\x04docx-bytes", "보고서.docx", DOCX_TYPE

        def content(self, artifact_id, version):
            raise ArtifactNotText(
                f"Artifact {artifact_id} v{version} is a binary file; "
                "use the download route."
            )

    class StubThreads:
        def require_owned(self, thread_id, sub, is_admin=False):
            return None

    monkeypatch.setattr(artifacts_route, "artifact_service", StubArtifacts())
    monkeypatch.setattr(artifacts_route, "thread_service", StubThreads())

    app = FastAPI()
    app.include_router(artifacts_route.router)
    app.dependency_overrides[current_user] = lambda: AuthUser(
        sub="u1", username="u1", is_admin=False
    )
    return TestClient(app)


def test_download_returns_the_bytes(client):
    response = client.get("/api/artifacts/a1/versions/1/download")
    assert response.status_code == 200
    assert response.content == b"PK\x03\x04docx-bytes"
    assert response.headers["content-type"] == DOCX_TYPE


def test_download_header_survives_a_korean_filename(client):
    response = client.get("/api/artifacts/a1/versions/1/download")
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    # The real name only in the percent-encoded form; a raw Korean byte in the
    # header is a 500 at the ASGI layer.
    assert "보고서" not in disposition
    assert "filename*=UTF-8''%EB%B3%B4%EA%B3%A0%EC%84%9C.docx" in disposition
    # Wholly non-ASCII stem, so the ASCII fallback uses the artifact id.
    assert 'filename="a1.docx"' in disposition


def test_content_route_reports_415_for_a_binary_artifact(client):
    response = client.get("/api/artifacts/a1/versions/1/content")
    assert response.status_code == 415
    assert "download" in response.json()["detail"]
