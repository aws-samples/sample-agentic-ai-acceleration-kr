"""
HTTP contract for /api/registry/skills.

Covers what the upload flow depends on and the service-level tests cannot show:
that an invalid bundle is reported in full without anything reaching S3, that the
created record carries the S3 pointer the harness needs, that a duplicate name is
refused rather than silently overwriting someone else's bundle, and that deleting
a skill record takes its bundle with it.
"""
import io
import os
import sys
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.auth as auth  # noqa: E402
import routes.registry as registry_routes  # noqa: E402
from models.registry import (  # noqa: E402
    DESCRIPTOR_AGENT_SKILLS,
    DESCRIPTOR_MCP,
    RegistryRecordDetail,
    RegistryRecordSummary,
)
from models.skill import SkillFile, SkillSource  # noqa: E402
from services.registry_service import SKILL_SOURCE_META_KEY  # noqa: E402
from services.skill_bundle_service import SkillBundleService  # noqa: E402

ADMIN = auth.AuthUser(sub="admin-1", username="root", groups=[auth.ADMIN_GROUP])

SKILL_MD = """---
name: pdf-processing
description: Extract text and tables from PDFs. Use when handling PDF documents.
---

# PDF processing

Run `scripts/extract.py`.
"""

URI = "s3://ap-skills/skills/pdf-processing/"


def bundle_zip(skill_md=SKILL_MD, extra=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("SKILL.md", skill_md)
        for path, content in (extra or {}).items():
            archive.writestr(path, content)
    return buf.getvalue()


def upload(data, filename="pdf-processing.zip"):
    return {"file": (filename, data, "application/zip")}


class StubRegistry:
    """Stands in for RegistryService, recording what the route asked of it."""

    def __init__(self, existing=None, detail=None):
        self.existing = existing or []
        self.detail = detail
        self.created = []
        self.updated = []
        self.deleted = []

    def list_records(self, descriptor_type=None, status=None, name=None):
        return [
            record
            for record in self.existing
            if (descriptor_type is None or record.descriptor_type == descriptor_type)
            and (name is None or record.name == name)
        ]

    def create_record(self, req, owner=None):
        self.created.append(req)
        return RegistryRecordSummary(
            record_id="rec-new",
            name=req.name,
            description=req.description,
            descriptor_type=req.descriptor_type,
            status="APPROVED",
        )

    def get_record(self, record_id):
        if self.detail is None:
            raise KeyError(record_id)
        return self.detail

    def update_record(self, record_id, req):
        self.updated.append((record_id, req))
        return self.detail

    def delete_record(self, record_id):
        self.deleted.append(record_id)


class StubBundles:
    """Stands in for SkillBundleService's S3 half, keeping the real validation."""

    def __init__(self, files=None):
        self.real = SkillBundleService(bucket="ap-skills", region="us-east-1")
        self.published = []
        self.deleted = []
        self.files = files or [SkillFile(path="SKILL.md", size_bytes=120)]

    def inspect(self, filename, body):
        return self.real.inspect(filename, body)

    def publish(self, staged):
        self.published.append(sorted(staged.files))
        return SkillSource(
            uri=self.real.uri_for(staged.info.name),
            files=sorted(staged.files),
            total_bytes=staged.info.total_bytes,
        )

    def list_files(self, uri):
        return list(self.files)

    def delete_prefix(self, uri):
        self.deleted.append(uri)
        return len(self.files)


def skill_record(name="pdf-processing", source=True, record_id="rec-1"):
    definition = {}
    if source:
        definition["_meta"] = {SKILL_SOURCE_META_KEY: {"type": "s3", "uri": URI}}
    return RegistryRecordDetail(
        record_id=record_id,
        name=name,
        descriptor_type=DESCRIPTOR_AGENT_SKILLS,
        status="APPROVED",
        descriptor_content={"skillMd": SKILL_MD, "skillDefinition": definition},
    )


@pytest.fixture
def client(monkeypatch):
    def build(existing=None, detail=None, files=None):
        registry = StubRegistry(existing=existing, detail=detail)
        bundles = StubBundles(files=files)
        monkeypatch.setattr(registry_routes, "_registry", lambda: registry)
        monkeypatch.setattr(registry_routes, "_bundles", lambda: bundles)
        app = FastAPI()
        app.include_router(registry_routes.router)
        # Token verification has its own tests; these are about the HTTP contract.
        app.dependency_overrides[auth.current_user] = lambda: ADMIN
        app.dependency_overrides[auth.require_admin] = lambda: ADMIN
        return TestClient(app, raise_server_exceptions=False), registry, bundles

    return build


# --- validation -------------------------------------------------------------


def test_validating_a_good_bundle_reports_its_contents(client):
    http, _, bundles = client()

    response = http.post(
        "/api/registry/skills/validate",
        files=upload(bundle_zip(extra={"scripts/extract.py": "print(1)"})),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "pdf-processing"
    assert body["description"].startswith("Extract text and tables")
    assert [f["path"] for f in body["files"]] == ["SKILL.md", "scripts/extract.py"]
    assert body["errors"] == []
    # A dry run must not touch storage, whatever the bundle looks like.
    assert bundles.published == []


def test_an_invalid_bundle_validates_as_200_with_every_broken_rule(client):
    """
    The report is the answer, not a failure: the dialog lists all the problems at
    once rather than making the uploader fix them one 400 at a time.
    """
    http, _, bundles = client()

    response = http.post(
        "/api/registry/skills/validate",
        files=upload(bundle_zip(skill_md="---\nname: PDF--Processing\n---\n")),
    )

    assert response.status_code == 200
    errors = response.json()["errors"]
    assert len(errors) == 2
    assert any("name" in e for e in errors)
    assert any("description" in e for e in errors)
    assert bundles.published == []


# --- create -----------------------------------------------------------------


def test_creating_publishes_the_bundle_and_records_the_s3_pointer(client):
    http, registry, bundles = client()

    response = http.post(
        "/api/registry/skills",
        files=upload(bundle_zip(extra={"references/REFERENCE.md": "# ref"})),
        data={"version": "1.0.0"},
    )

    assert response.status_code == 200
    assert response.json()["name"] == "pdf-processing"
    # The whole directory reached S3, not just the markdown.
    assert bundles.published == [["SKILL.md", "references/REFERENCE.md"]]

    req = registry.created[0]
    assert req.descriptor_type == DESCRIPTOR_AGENT_SKILLS
    # Name and description come from the frontmatter, never from the form.
    assert req.name == "pdf-processing"
    assert req.description.startswith("Extract text and tables")
    assert req.skill_source["uri"] == URI
    assert req.skill_markdown == SKILL_MD


def test_creating_from_a_single_md_file_works(client):
    """A one-file skill should not have to be zipped to be uploaded."""
    http, registry, bundles = client()

    response = http.post(
        "/api/registry/skills",
        files=upload(SKILL_MD.encode("utf-8"), filename="anything.md"),
    )

    assert response.status_code == 200
    assert bundles.published == [["SKILL.md"]]
    assert registry.created[0].name == "pdf-processing"


def test_an_invalid_bundle_is_rejected_before_anything_is_written(client):
    http, registry, bundles = client()

    response = http.post(
        "/api/registry/skills",
        files=upload(bundle_zip(skill_md="# no frontmatter at all\n")),
    )

    assert response.status_code == 400
    assert "frontmatter" in response.json()["detail"]
    assert bundles.published == []
    assert registry.created == []


def test_a_duplicate_skill_name_is_refused_rather_than_overwritten(client):
    """One prefix per name, so a second record would clobber the first's bundle."""
    http, registry, bundles = client(
        existing=[
            RegistryRecordSummary(
                record_id="rec-1",
                name="pdf-processing",
                descriptor_type=DESCRIPTOR_AGENT_SKILLS,
                status="APPROVED",
            )
        ]
    )

    response = http.post("/api/registry/skills", files=upload(bundle_zip()))

    assert response.status_code == 409
    assert "번들을 교체" in response.json()["detail"]
    assert bundles.published == []
    assert registry.created == []


def test_a_deprecated_record_does_not_block_the_name(client):
    """Deprecation is terminal in AWS, so re-registering is the only way back."""
    http, registry, bundles = client(
        existing=[
            RegistryRecordSummary(
                record_id="rec-old",
                name="pdf-processing",
                descriptor_type=DESCRIPTOR_AGENT_SKILLS,
                status="DEPRECATED",
            )
        ]
    )

    response = http.post("/api/registry/skills", files=upload(bundle_zip()))

    assert response.status_code == 200
    assert bundles.published == [["SKILL.md"]]


# --- replace ----------------------------------------------------------------


def test_replacing_republishes_and_updates_the_record(client):
    http, registry, bundles = client(detail=skill_record())

    response = http.put(
        "/api/registry/skills/rec-1",
        files=upload(bundle_zip(extra={"scripts/new.py": "x"})),
    )

    assert response.status_code == 200
    assert bundles.published == [["SKILL.md", "scripts/new.py"]]
    record_id, req = registry.updated[0]
    assert record_id == "rec-1"
    assert req.skill_source["uri"] == URI
    assert req.skill_markdown == SKILL_MD


def test_replacing_with_a_different_name_is_refused(client):
    """The prefix is keyed on the name, so a rename would orphan the old bundle."""
    http, registry, bundles = client(detail=skill_record(name="other-skill"))

    response = http.put("/api/registry/skills/rec-1", files=upload(bundle_zip()))

    assert response.status_code == 400
    assert "새 스킬로 등록" in response.json()["detail"]
    assert bundles.published == []
    assert registry.updated == []


def test_replacing_a_non_skill_record_is_refused(client):
    http, registry, bundles = client(
        detail=RegistryRecordDetail(
            record_id="rec-1",
            name="weather",
            descriptor_type=DESCRIPTOR_MCP,
            status="APPROVED",
        )
    )

    response = http.put("/api/registry/skills/rec-1", files=upload(bundle_zip()))

    assert response.status_code == 400
    assert bundles.published == []


# --- read and delete --------------------------------------------------------


def test_listing_files_reads_the_prefix_named_by_the_record(client):
    http, _, _ = client(
        detail=skill_record(),
        files=[
            SkillFile(path="SKILL.md", size_bytes=120),
            SkillFile(path="references/REFERENCE.md", size_bytes=40),
        ],
    )

    response = http.get("/api/registry/skills/rec-1/files")

    assert response.status_code == 200
    body = response.json()
    assert body["uri"] == URI
    assert [f["path"] for f in body["files"]] == ["SKILL.md", "references/REFERENCE.md"]
    assert body["total_bytes"] == 160


def test_an_inline_record_lists_no_files_rather_than_failing(client):
    """Records predating bundle uploads have no prefix; that is not an error."""
    http, _, _ = client(detail=skill_record(source=False))

    response = http.get("/api/registry/skills/rec-1/files")

    assert response.status_code == 200
    assert response.json() == {"uri": None, "files": [], "total_bytes": 0}


def test_deleting_a_skill_record_removes_its_bundle(client):
    http, registry, bundles = client(detail=skill_record())

    response = http.delete("/api/registry/records/rec-1")

    assert response.status_code == 200
    assert registry.deleted == ["rec-1"]
    assert bundles.deleted == [URI]


def test_deleting_a_non_skill_record_touches_no_bundle(client):
    http, registry, bundles = client(
        detail=RegistryRecordDetail(
            record_id="rec-1",
            name="weather",
            descriptor_type=DESCRIPTOR_MCP,
            status="APPROVED",
        )
    )

    response = http.delete("/api/registry/records/rec-1")

    assert response.status_code == 200
    assert registry.deleted == ["rec-1"]
    assert bundles.deleted == []


def test_a_failed_bundle_cleanup_does_not_fail_the_delete(client):
    """The record is already gone by then; reporting a 500 would be a lie."""
    http, registry, bundles = client(detail=skill_record())

    def boom(uri):
        raise RuntimeError("AccessDenied")

    bundles.delete_prefix = boom

    response = http.delete("/api/registry/records/rec-1")

    assert response.status_code == 200
    assert registry.deleted == ["rec-1"]
