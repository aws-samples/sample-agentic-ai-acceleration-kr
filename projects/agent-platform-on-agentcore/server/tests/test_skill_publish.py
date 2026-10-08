"""
Publishing a skill bundle to S3, and what the harness does with the result.

The registry cannot hold a skill directory — its AGENT_SKILLS descriptor is two
inline strings, and AWS documents the markdown as discovery metadata only. So S3
is the store of record and the registry record carries a pointer to the prefix.
These cover the three joints in that arrangement: what publish writes and prunes,
that the pointer survives a descriptor round trip, and that the harness composer
attaches the prefix instead of overwriting it with a lone SKILL.md.
"""
import io
import os
import sys
import zipfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.harness import ComposeHarnessRequest  # noqa: E402
from models.registry import (  # noqa: E402
    CreateRecordRequest,
    UpdateRecordRequest,
    DESCRIPTOR_AGENT_SKILLS,
    RegistryRecordDetail,
)
from models.skill import InvalidSkillBundle  # noqa: E402
from services.harness_service import HarnessService  # noqa: E402
from services.registry_service import (  # noqa: E402
    SKILL_SOURCE_META_KEY,
    _build_descriptors,
    _extract_descriptor_content,
    _merge_descriptor_content,
    skill_source_of,
)
from services.skill_bundle_service import SkillBundleService  # noqa: E402

BUCKET = "ap-skills"
URI = f"s3://{BUCKET}/skills/pdf-processing/"

SKILL_MD = """---
name: pdf-processing
description: Extract text and tables from PDFs. Use when handling PDF documents.
---

# PDF processing

Run `scripts/extract.py`.
"""


class FakeS3:
    """Just enough S3 to see what was written, listed and deleted."""

    def __init__(self, existing=()):
        self.objects = {key: 1 for key in existing}
        self.put = []
        self.deleted = []

    def put_object(self, Bucket, Key, Body, ContentType=None, **_):
        self.put.append((Key, ContentType))
        self.objects[Key] = len(Body)

    def list_objects_v2(self, Bucket, Prefix, **_):
        return {
            "Contents": [
                {"Key": key, "Size": size}
                for key, size in sorted(self.objects.items())
                if key.startswith(Prefix)
            ]
        }

    def delete_objects(self, Bucket, Delete):
        for obj in Delete["Objects"]:
            self.deleted.append(obj["Key"])
            self.objects.pop(obj["Key"], None)


def service(existing=()):
    svc = SkillBundleService(bucket=BUCKET, region="us-east-1")
    svc._s3 = FakeS3(existing)
    return svc


def bundle(files=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("SKILL.md", SKILL_MD)
        for path, content in (files or {}).items():
            archive.writestr(path, content)
    return buf.getvalue()


# --- publish ----------------------------------------------------------------


def test_publish_writes_the_whole_bundle_under_the_skill_name():
    """The prefix is the frontmatter name, which is what the harness fetches."""
    svc = service()

    source = svc.publish(
        svc.inspect(
            "b.zip",
            bundle({"references/REFERENCE.md": "# ref", "scripts/extract.py": "1"}),
        )
    )

    assert [key for key, _ in svc._s3.put] == [
        "skills/pdf-processing/SKILL.md",
        "skills/pdf-processing/references/REFERENCE.md",
        "skills/pdf-processing/scripts/extract.py",
    ]
    assert source.uri == URI
    assert source.files == ["SKILL.md", "references/REFERENCE.md", "scripts/extract.py"]
    assert source.published_at


def test_publish_sets_a_readable_content_type_per_file():
    svc = service()

    svc.publish(svc.inspect("b.zip", bundle({"scripts/x.py": "1", "assets/a.png": "p"})))

    types = dict(svc._s3.put)
    assert types["skills/pdf-processing/SKILL.md"].startswith("text/markdown")
    assert types["skills/pdf-processing/scripts/x.py"].startswith("text/x-python")
    assert types["skills/pdf-processing/assets/a.png"] == "image/png"


def test_publish_prunes_files_the_new_bundle_dropped():
    """
    Without this, a replaced bundle keeps serving the file it removed: the harness
    fetches the whole prefix, not the list in the record.
    """
    svc = service(
        existing=[
            "skills/pdf-processing/SKILL.md",
            "skills/pdf-processing/scripts/old.py",
            "skills/pdf-processing/references/gone.md",
        ]
    )

    svc.publish(svc.inspect("b.zip", bundle({"scripts/new.py": "1"})))

    assert sorted(svc._s3.deleted) == [
        "skills/pdf-processing/references/gone.md",
        "skills/pdf-processing/scripts/old.py",
    ]
    assert sorted(svc._s3.objects) == [
        "skills/pdf-processing/SKILL.md",
        "skills/pdf-processing/scripts/new.py",
    ]


def test_publish_leaves_another_skills_prefix_alone():
    svc = service(existing=["skills/other-skill/SKILL.md"])

    svc.publish(svc.inspect("a.md", SKILL_MD.encode()))

    assert svc._s3.deleted == []
    assert "skills/other-skill/SKILL.md" in svc._s3.objects


def test_publish_refuses_an_invalid_bundle():
    svc = service()

    with pytest.raises(InvalidSkillBundle) as caught:
        svc.publish(svc.inspect("a.md", b"---\nname: Bad_Name\n---\n"))

    assert len(caught.value.errors) == 2
    assert svc._s3.put == []


def test_delete_prefix_removes_the_whole_bundle():
    svc = service(
        existing=[
            "skills/pdf-processing/SKILL.md",
            "skills/pdf-processing/scripts/x.py",
            "skills/other-skill/SKILL.md",
        ]
    )

    removed = svc.delete_prefix(URI)

    assert removed == 2
    assert sorted(svc._s3.objects) == ["skills/other-skill/SKILL.md"]


def test_a_uri_from_another_bucket_is_refused_rather_than_probed():
    """The server's role has no grant there, so AccessDenied would be the answer."""
    svc = service()

    with pytest.raises(ValueError):
        svc.list_files("s3://someone-elses-bucket/skills/x/")

    assert svc.delete_prefix("s3://someone-elses-bucket/skills/x/") == 0


# --- the record's pointer ---------------------------------------------------


def source_dump(uri=URI, files=("SKILL.md",)):
    return {"type": "s3", "uri": uri, "files": list(files), "total_bytes": 10}


def test_the_pointer_survives_a_descriptor_round_trip():
    descriptors = _build_descriptors(
        CreateRecordRequest(
            name="pdf-processing",
            description="d",
            descriptor_type=DESCRIPTOR_AGENT_SKILLS,
            skill_markdown=SKILL_MD,
            skill_source=source_dump(),
        )
    )

    skills = descriptors["agentSkillsDefinition"]
    assert skills["additionalData"]["skillMd"]["data"] == SKILL_MD
    # `_meta` is the extension point the 0.1.0 skill-definition schema reserves.
    content = _extract_descriptor_content(descriptors)
    assert skill_source_of(content)["uri"] == URI


def test_a_record_without_a_pointer_reads_as_none():
    for content in (
        None,
        {},
        {"skillDefinition": {}},
        {"skillDefinition": {"_meta": {}}},
        # A non-s3 URI is not something the harness can fetch.
        {"skillDefinition": {"_meta": {SKILL_SOURCE_META_KEY: {"uri": "https://x"}}}},
    ):
        assert skill_source_of(content) is None


def test_editing_a_record_without_touching_the_bundle_keeps_the_pointer():
    """
    A description-only edit rebuilds the descriptor, so a pointer that lived only
    in the request would be erased by the very next rename.
    """
    current = RegistryRecordDetail(
        record_id="rec-1",
        name="pdf-processing",
        description="old",
        descriptor_type=DESCRIPTOR_AGENT_SKILLS,
        descriptor_content={
            "skillMd": SKILL_MD,
            "skillDefinition": {"_meta": {SKILL_SOURCE_META_KEY: source_dump()}},
        },
    )

    merged = _merge_descriptor_content(current, UpdateRecordRequest(description="new"))
    rebuilt = _extract_descriptor_content(_build_descriptors(merged))

    assert skill_source_of(rebuilt)["uri"] == URI
    assert rebuilt["skillMd"] == SKILL_MD


def test_replacing_the_bundle_overwrites_the_pointer():
    current = RegistryRecordDetail(
        record_id="rec-1",
        name="pdf-processing",
        descriptor_type=DESCRIPTOR_AGENT_SKILLS,
        descriptor_content={
            "skillMd": "old markdown",
            "skillDefinition": {
                "_meta": {
                    SKILL_SOURCE_META_KEY: source_dump(files=("SKILL.md", "old.py")),
                    # An unrelated key under `_meta` must not be collateral damage.
                    "com.example/other": {"keep": True},
                }
            },
        },
    )

    merged = _merge_descriptor_content(
        current,
        UpdateRecordRequest(
            skill_markdown=SKILL_MD,
            skill_source=source_dump(files=("SKILL.md", "new.py")),
        ),
    )
    rebuilt = _extract_descriptor_content(_build_descriptors(merged))

    assert skill_source_of(rebuilt)["files"] == ["SKILL.md", "new.py"]
    assert rebuilt["skillDefinition"]["_meta"]["com.example/other"] == {"keep": True}
    assert rebuilt["skillMd"] == SKILL_MD


# --- harness composition ----------------------------------------------------


class StubRegistry:
    def __init__(self, records):
        self.records = {record.record_id: record for record in records}

    def get_record(self, record_id):
        return self.records[record_id]


def skill_record(record_id, name, source=None, markdown=None):
    definition = {}
    if source:
        definition["_meta"] = {SKILL_SOURCE_META_KEY: source}
    return RegistryRecordDetail(
        record_id=record_id,
        name=name,
        descriptor_type=DESCRIPTOR_AGENT_SKILLS,
        status="APPROVED",
        descriptor_content={"skillMd": markdown, "skillDefinition": definition},
    )


def harness(records):
    svc = HarnessService(
        registry=StubRegistry(records),
        region="us-east-1",
        execution_role_arn="arn:aws:iam::1:role/harness",
        skills_bucket=BUCKET,
    )
    svc._s3 = FakeS3()
    return svc


def test_an_uploaded_bundle_is_attached_by_prefix_and_never_rewritten():
    """
    Writing here would replace the uploaded `references/` and `scripts/` with a
    single SKILL.md — the exact bug that made bundles worth storing in S3.
    """
    svc = harness([skill_record("rec-1", "pdf-processing", source=source_dump())])

    skills = svc._resolve_skills(["rec-1"], [])

    assert skills == [{"s3": {"uri": URI}}]
    assert svc._s3.put == []


def test_a_legacy_inline_record_still_publishes_its_markdown():
    """Records registered before bundle uploads existed must keep working."""
    svc = harness([skill_record("rec-2", "Company Style", markdown=SKILL_MD)])

    skills = svc._resolve_skills(["rec-2"], [])

    assert skills == [{"s3": {"uri": f"s3://{BUCKET}/skills/company-style/"}}]
    assert svc._s3.put == [("skills/company-style/SKILL.md", "text/markdown")]


def test_a_record_with_neither_a_bundle_nor_markdown_is_refused():
    svc = harness([skill_record("rec-3", "empty-skill")])

    with pytest.raises(ValueError, match="no uploaded bundle"):
        svc._resolve_skills(["rec-3"], [])


def test_both_kinds_compose_together_alongside_aws_skills():
    svc = harness(
        [
            skill_record("rec-1", "pdf-processing", source=source_dump()),
            skill_record("rec-2", "Company Style", markdown=SKILL_MD),
        ]
    )

    skills = svc._resolve_skills(["rec-1", "rec-2"], ["core-skills/*"])

    assert skills == [
        {"s3": {"uri": URI}},
        {"s3": {"uri": f"s3://{BUCKET}/skills/company-style/"}},
        {"awsSkills": {"paths": ["core-skills/*"]}},
    ]


def test_a_bundle_backed_record_is_composable_in_the_catalog():
    """Composability used to require inline markdown, which a bundle record lacks."""
    svc = harness([skill_record("rec-1", "pdf-processing", source=source_dump())])
    summary = svc.registry.get_record("rec-1")

    composable = svc._composable(DESCRIPTOR_AGENT_SKILLS, summary)

    assert composable.composable
    assert composable.reason is None


def test_a_record_with_nothing_to_fetch_is_not_composable():
    svc = harness([skill_record("rec-3", "empty-skill")])
    summary = svc.registry.get_record("rec-3")

    composable = svc._composable(DESCRIPTOR_AGENT_SKILLS, summary)

    assert not composable.composable
    assert "no uploaded bundle" in composable.reason


def test_composing_a_bundle_record_does_not_need_the_bucket_configured_for_writes():
    """A prefix-only attach reads nothing and writes nothing through this server."""
    svc = harness([skill_record("rec-1", "pdf-processing", source=source_dump())])
    svc.skills_bucket = BUCKET

    assert svc._resolve_skills(["rec-1"], []) == [{"s3": {"uri": URI}}]


def test_the_compose_request_carries_the_prefix_into_create_harness():
    """The end of the chain: what CreateHarness is actually given."""
    svc = harness([skill_record("rec-1", "pdf-processing", source=source_dump())])
    req = ComposeHarnessRequest(name="Composed", skill_record_ids=["rec-1"])

    assert svc._resolve_skills(req.skill_record_ids, req.aws_skill_paths) == [
        {"s3": {"uri": URI}}
    ]
