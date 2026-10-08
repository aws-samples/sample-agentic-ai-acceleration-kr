"""Recording a file the server never had in its hands.

The sweep path hands S3 a presigned PUT and the sandbox uploads the bytes
directly — 3MB in 0.07s, measured — because pulling them through the command
channel is impossible (60KB of base64 on stdout never completed). So the server
writes the DynamoDB row for an object that already exists, and must not
`put_object` a second time.

`content()` must refuse a binary kind explicitly. It ends in
`body.decode("utf-8")`, so a .docx would surface as a 500 with a UnicodeDecodeError
rather than as "this is not text".
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.artifact import ArtifactVersion  # noqa: E402
from services.artifact_service import (  # noqa: E402
    ArtifactNotText,
    ArtifactService,
)

DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class StubRepo:
    def __init__(self):
        self.saved = []
        self.versions = {}

    def put_version(self, version):
        self.saved.append(version)
        self.versions[(version.artifact_id, version.version)] = version
        return version

    def get_version(self, artifact_id, version):
        return self.versions.get((artifact_id, version))

    def latest(self, artifact_id):
        found = [v for (a, _), v in self.versions.items() if a == artifact_id]
        return max(found, key=lambda v: v.version) if found else None


class StubS3:
    def __init__(self, body=b""):
        self.body = body
        self.puts = []
        self.presigned = []

    def put_object(self, **kwargs):
        self.puts.append(kwargs)

    def get_object(self, **kwargs):
        return {"Body": type("B", (), {"read": lambda _self: self.body})()}

    def generate_presigned_url(self, operation, Params=None, ExpiresIn=None):
        self.presigned.append((operation, Params, ExpiresIn))
        return f"https://s3.test/{Params['Key']}?sig=1"


def service():
    svc = ArtifactService(repository=StubRepo(), bucket="bucket", region="us-east-1")
    svc._s3 = StubS3()
    return svc


def test_key_is_ascii_and_carries_the_real_extension():
    svc = service()
    key = svc.key_for("t1", "abc_bogo-1234abcd", 2, "docx")
    assert key == "artifacts/t1/abc_bogo-1234abcd/v2.docx"
    assert key.isascii()


def test_presign_put_is_a_put_and_short_lived():
    svc = service()
    url = svc.presign_put("artifacts/t1/a/v1.docx")
    operation, params, ttl = svc.s3.presigned[0]
    assert operation == "put_object"
    assert params == {"Bucket": "bucket", "Key": "artifacts/t1/a/v1.docx"}
    assert ttl == 300
    assert url.startswith("https://s3.test/")


def test_register_stored_writes_the_row_without_touching_s3():
    svc = service()
    stored = svc.register_stored(
        "t1",
        artifact_id="a1",
        version=1,
        filename="보고서.docx",
        s3_key="artifacts/t1/a1/v1.docx",
        size_bytes=54542,
        source_path="/home/보고서.docx",
        source_mtime=1786700440,
        message_id="msg-9",
    )

    assert svc.s3.puts == []                     # the bytes were never ours
    assert stored.kind == "file"
    assert stored.filename == "보고서.docx"
    assert stored.title == "보고서.docx"
    assert stored.content_type == DOCX_TYPE
    assert stored.source_mtime == 1786700440
    assert stored.message_id == "msg-9"
    assert svc.repository.saved == [stored]


def test_content_refuses_a_binary_artifact():
    svc = service()
    svc.register_stored(
        "t1",
        artifact_id="a1",
        version=1,
        filename="보고서.docx",
        s3_key="artifacts/t1/a1/v1.docx",
        size_bytes=10,
        source_path="/home/보고서.docx",
        source_mtime=1,
    )
    with pytest.raises(ArtifactNotText):
        svc.content("a1", 1)


def test_binary_body_returns_bytes_filename_and_type():
    svc = service()
    svc._s3 = StubS3(body=b"PK\x03\x04docx")
    svc.register_stored(
        "t1",
        artifact_id="a1",
        version=1,
        filename="보고서.docx",
        s3_key="artifacts/t1/a1/v1.docx",
        size_bytes=8,
        source_path="/home/보고서.docx",
        source_mtime=1,
    )
    body, filename, content_type = svc.binary_body("a1", 1)
    assert body == b"PK\x03\x04docx"
    assert filename == "보고서.docx"
    assert content_type == DOCX_TYPE


def test_text_artifacts_still_read_as_text():
    svc = service()
    svc._s3 = StubS3(body=b"# hello")
    svc.repository.put_version(
        ArtifactVersion(
            artifact_id="a2",
            version=1,
            thread_id="t1",
            title="notes",
            kind="markdown",
            s3_key="artifacts/t1/a2/v1.md",
            size_bytes=7,
            created_at="2026-08-14T00:00:00",
        )
    )
    assert svc.content("a2", 1).content == "# hello"


def test_share_url_sets_the_download_name_with_rfc5987():
    svc = service()
    svc.register_stored(
        "t1",
        artifact_id="a1",
        version=1,
        filename="보고서.docx",
        s3_key="artifacts/t1/a1/v1.docx",
        size_bytes=10,
        source_path="/home/보고서.docx",
        source_mtime=1,
    )
    svc.share_url("a1", 1, download=True)
    _, params, _ = svc.s3.presigned[-1]
    disposition = params["ResponseContentDisposition"]
    # The latin-1 header trap: the raw Korean name must never be the `filename=`
    # value, only the percent-encoded `filename*`.
    assert "보고서" not in disposition
    assert "filename*=UTF-8''" in disposition
    assert params["ResponseContentType"] == DOCX_TYPE


def test_the_s3_client_signs_with_sigv4():
    """A SigV2 presigned PUT cannot survive an uploader that sends Content-Type.

    SigV2 folds the request's Content-Type into the string to sign while the
    presigner signs none, so the sandbox's upload is rejected with
    SignatureDoesNotMatch. Measured against the deployed stack; under SigV4 only
    `host` is signed.
    """
    svc = ArtifactService(repository=StubRepo(), bucket="bucket", region="us-east-1")
    assert svc.s3.meta.config.signature_version == "s3v4"
