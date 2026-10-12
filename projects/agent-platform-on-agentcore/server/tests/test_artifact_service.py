"""
Tests for persisting agent-produced artifacts.

S3 and DynamoDB are faked: these cover the versioning, key layout and
degradation behaviour rather than boto3 itself.
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.artifact import ArtifactVersion  # noqa: E402
from services.artifact_service import (  # noqa: E402
    ArtifactNotFound,
    ArtifactService,
    ArtifactsNotConfigured,
)

THREAD_ID = "thread-1"


class FakeRepository:
    """In-memory stand-in for ArtifactRepository."""

    def __init__(self):
        self.items = {}

    def put_version(self, artifact: ArtifactVersion) -> ArtifactVersion:
        self.items[(artifact.artifact_id, artifact.version)] = artifact
        return artifact

    def _for_id(self, artifact_id):
        return sorted(
            (a for (aid, _), a in self.items.items() if aid == artifact_id),
            key=lambda a: a.version,
            reverse=True,
        )

    def latest(self, artifact_id):
        versions = self._for_id(artifact_id)
        return versions[0] if versions else None

    def get_version(self, artifact_id, version):
        return self.items.get((artifact_id, version))

    def list_versions(self, artifact_id):
        return self._for_id(artifact_id)

    def list_by_thread(self, thread_id):
        return [a for a in self.items.values() if a.thread_id == thread_id]


class FakeS3:
    def __init__(self, fail=False):
        self.objects = {}
        self.presigned = []
        self.fail = fail

    def put_object(self, Bucket, Key, Body, ContentType):
        if self.fail:
            raise RuntimeError("access denied")
        self.objects[(Bucket, Key)] = {"Body": Body, "ContentType": ContentType}

    def get_object(self, Bucket, Key):
        stored = self.objects[(Bucket, Key)]

        class Stream:
            def read(self_inner):
                return stored["Body"]

        return {"Body": Stream()}

    def generate_presigned_url(self, op, Params, ExpiresIn):
        self.presigned.append((op, Params, ExpiresIn))
        return f"https://s3.example/{Params['Key']}?e={ExpiresIn}"


def build_service(fail_s3=False, bucket="artifacts-bucket", repository=None):
    service = ArtifactService(
        repository=FakeRepository() if repository is None else repository,
        bucket=bucket,
        region="us-east-1",
    )
    service._s3 = FakeS3(fail=fail_s3)
    return service


def artifact_event(content="# Hello", **overrides):
    artifact = {
        "toolCallId": "tooluse-1",
        "artifactId": "my-report",
        "title": "My Report",
        "kind": "markdown",
        "content": content,
    }
    artifact.update(overrides)
    return {"event": {"artifact": artifact}}


def persist(service, event):
    return asyncio.run(service.persist_and_enrich(THREAD_ID, event))


def test_first_version_is_stored_with_scoped_key():
    service = build_service()

    result = persist(service, artifact_event())
    artifact = result["event"]["artifact"]

    assert artifact["stored"] is True
    assert artifact["version"] == 1
    assert artifact["s3Key"].startswith(f"artifacts/{THREAD_ID}/")
    assert artifact["s3Key"].endswith("/v1.md")
    # The agent-chosen id is namespaced so two threads can both use "my-report".
    assert artifact["artifactId"].endswith("_my-report")
    assert artifact["artifactId"] != "my-report"
    assert artifact["sizeBytes"] == len(b"# Hello")
    assert (("artifacts-bucket", artifact["s3Key"])) in service._s3.objects


def test_same_artifact_id_appends_a_version():
    service = build_service()

    first = persist(service, artifact_event())["event"]["artifact"]
    second = persist(service, artifact_event(content="# Hello v2"))["event"]["artifact"]

    assert (first["version"], second["version"]) == (1, 2)
    assert first["artifactId"] == second["artifactId"]
    assert second["s3Key"].endswith("/v2.md")


def test_ids_are_scoped_per_thread():
    service = build_service()

    same_id = artifact_event()
    first = asyncio.run(service.persist_and_enrich("thread-a", same_id))
    second = asyncio.run(service.persist_and_enrich("thread-b", artifact_event()))

    assert first["event"]["artifact"]["artifactId"] != second["event"]["artifact"]["artifactId"]
    # Neither thread appends onto the other's artifact.
    assert second["event"]["artifact"]["version"] == 1


def test_code_artifact_uses_language_extension():
    service = build_service()

    artifact = persist(
        service,
        artifact_event(content="print(1)", kind="code", language="python"),
    )["event"]["artifact"]

    assert artifact["s3Key"].endswith("/v1.py")


def test_unknown_kind_falls_back_to_text():
    service = build_service()

    artifact = persist(service, artifact_event(kind="spreadsheet"))["event"]["artifact"]

    assert artifact["kind"] == "text"
    assert artifact["s3Key"].endswith("/v1.txt")


def test_unconfigured_bucket_streams_unstored_artifact():
    service = ArtifactService(repository=None, bucket="", region="us-east-1")

    artifact = persist(service, artifact_event())["event"]["artifact"]

    assert artifact["stored"] is False
    assert artifact["version"] == 1
    # The panel can still render from the inline content.
    assert artifact["content"] == "# Hello"


def test_s3_failure_does_not_break_the_stream():
    service = build_service(fail_s3=True)

    artifact = persist(service, artifact_event())["event"]["artifact"]

    assert artifact["stored"] is False
    assert "access denied" in artifact["storeError"]
    assert artifact["content"] == "# Hello"


def test_reads_require_configuration():
    service = ArtifactService(repository=None, bucket="", region="us-east-1")

    with pytest.raises(ArtifactsNotConfigured):
        service.detail("anything")


def test_detail_lists_versions_newest_first():
    service = build_service()
    persist(service, artifact_event())
    artifact = persist(service, artifact_event(content="v2"))["event"]["artifact"]

    detail = service.detail(artifact["artifactId"])

    assert detail.versions == [2, 1]
    assert detail.latest.version == 2


def test_content_round_trips_through_s3():
    service = build_service()
    artifact = persist(service, artifact_event(content="# Round trip"))["event"]["artifact"]

    content = service.content(artifact["artifactId"], 1)

    assert content.content == "# Round trip"
    assert content.kind == "markdown"


def test_missing_version_raises_not_found():
    service = build_service()
    artifact = persist(service, artifact_event())["event"]["artifact"]

    with pytest.raises(ArtifactNotFound):
        service.content(artifact["artifactId"], 99)


def test_share_url_clamps_expiry_to_seven_days():
    service = build_service()
    artifact = persist(service, artifact_event())["event"]["artifact"]

    url, ttl = service.share_url(artifact["artifactId"], 1, expires_in=999999)

    assert ttl == 604800
    assert url.startswith("https://s3.example/")


def test_share_url_download_sets_content_disposition():
    service = build_service()
    artifact = persist(service, artifact_event())["event"]["artifact"]

    service.share_url(artifact["artifactId"], 1, download=True)

    _op, params, _ttl = service._s3.presigned[-1]
    # RFC 5987 with percent-encoded name alongside ASCII fallback
    assert 'filename="My Report.md"' in params["ResponseContentDisposition"]
    assert "filename*=UTF-8''My%20Report.md" in params["ResponseContentDisposition"]
