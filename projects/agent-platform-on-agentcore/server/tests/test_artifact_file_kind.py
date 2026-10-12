"""A binary artifact has to be describable before it can be delivered.

The existing model is text-only by construction: the extension comes from the
`kind`, the content type comes from the `kind`, and the id slug is built with
`str.isalnum()` — which is *not* ASCII-only (`'보'.isalnum()` is True). Swept
filenames are the first user-supplied strings to reach that slug, and an S3 key
must stay ASCII.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.artifact import (  # noqa: E402
    ARTIFACT_KINDS,
    ArtifactVersion,
    content_type_for,
    extension_for,
    is_binary_kind,
    normalize_kind,
)
from services.artifact_service import scoped_id  # noqa: E402


def test_file_is_a_kind():
    assert "file" in ARTIFACT_KINDS
    assert normalize_kind("file") == "file"
    assert is_binary_kind("file")
    assert not is_binary_kind("markdown")


def test_extension_comes_from_the_filename_for_a_file():
    assert extension_for("file", filename="보고서.docx") == "docx"
    assert extension_for("file", filename="archive.tar.gz") == "gz"
    # No extension at all must not produce a key ending in a bare dot.
    assert extension_for("file", filename="README") == "bin"


def test_extension_for_text_kinds_is_unchanged():
    assert extension_for("markdown") == "md"
    assert extension_for("code", "python") == "py"


def test_content_type_is_guessed_from_the_filename_for_a_file():
    assert content_type_for("file", "보고서.docx") == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert content_type_for("file", "sheet.xlsx") == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert content_type_for("file", "mystery.zzz") == "application/octet-stream"
    assert content_type_for("markdown") == "text/markdown; charset=utf-8"


def test_scoped_id_is_ascii_even_for_a_korean_name():
    artifact_id = scoped_id("thread-1", "보고서.docx")
    assert artifact_id.isascii(), artifact_id
    # And two different Korean names must not collapse into the same id.
    assert artifact_id != scoped_id("thread-1", "발표자료.docx")


def test_scoped_id_still_namespaces_by_thread():
    assert scoped_id("thread-1", "report.docx") != scoped_id("thread-2", "report.docx")


def test_new_fields_round_trip_through_the_repository_item():
    from repositories.artifact_repository import _to_model

    version = ArtifactVersion(
        artifact_id="a1",
        version=2,
        thread_id="t1",
        title="보고서.docx",
        kind="file",
        s3_key="artifacts/t1/a1/v2.docx",
        size_bytes=54542,
        created_at="2026-08-14T09:40:00",
        filename="보고서.docx",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        source_path="/home/보고서.docx",
        source_mtime=1786700440,
        preview_key="artifacts/t1/a1/v2.preview.md",
    )
    item = {
        "artifact_id": version.artifact_id,
        "version": version.version,
        "thread_id": version.thread_id,
        "title": version.title,
        "kind": version.kind,
        "s3_key": version.s3_key,
        "size_bytes": version.size_bytes,
        "created_at": version.created_at,
        "filename": version.filename,
        "content_type": version.content_type,
        "source_path": version.source_path,
        "source_mtime": version.source_mtime,
        "preview_key": version.preview_key,
    }
    assert _to_model(item) == version


def test_two_long_korean_paths_do_not_collapse_to_one_id():
    """The digest is the only thing separating these two, so it must survive.

    Both paths are 48 characters and strip to the identical ASCII skeleton
    (`-home-reports-2026-2026-_3--_-----_-----_v3-docx`), which is exactly 48 —
    so appending the digest and then truncating to 48 threw the whole digest
    away and produced one id for two files. One document's versions would have
    been appended onto the other's artifact.
    """
    finished = "/home/reports/2026/2026년_3분기_최종보고서_검토완료본_v3.docx"
    draft = "/home/reports/2026/2026년_3분기_최종보고서_검토미완료_v3.docx"

    assert scoped_id("t1", finished) != scoped_id("t1", draft)
    assert scoped_id("t1", finished).isascii()
    assert scoped_id("t1", draft).isascii()


def test_office_types_do_not_depend_on_the_system_mime_registry(monkeypatch):
    """The container has no /etc/mime.types, so mimetypes alone returns nothing.

    Measured inside the deployed image: `guess_type("a.docx")` is `(None, None)`.
    Without an explicit table every office file is served as octet-stream, and a
    .pdf then downloads instead of rendering in the panel's iframe.
    """
    import mimetypes

    monkeypatch.setattr(mimetypes, "guess_type", lambda *a, **k: (None, None))

    assert content_type_for("file", "보고서.docx") == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert content_type_for("file", "결과.xlsx") == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert content_type_for("file", "보고서.pdf") == "application/pdf"
    assert content_type_for("file", "mystery.zzz") == "application/octet-stream"
