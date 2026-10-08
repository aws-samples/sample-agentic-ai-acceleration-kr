"""SkillBundleService discovery: enumerate bucket prefixes as skills."""
import pytest

from services.skill_bundle_service import SkillBundleService, DiscoveredSkill

SKILL_MD = b"""---
name: sales-analyst
description: Answers revenue questions.
---
# Sales Analyst
Body.
"""


class _FakeS3:
    def __init__(self, prefixes, objects):
        # prefixes: list of "skills/<name>/"; objects: {key: bytes}
        self._prefixes = prefixes
        self._objects = objects

    def list_objects_v2(self, **kw):
        if kw.get("Delimiter") == "/":
            return {"CommonPrefixes": [{"Prefix": p} for p in self._prefixes]}
        # _list_prefix path (no delimiter): return keys under Prefix
        prefix = kw["Prefix"]
        contents = [
            {"Key": k, "Size": len(v)}
            for k, v in self._objects.items()
            if k.startswith(prefix)
        ]
        return {"Contents": contents}

    def get_object(self, Bucket, Key):
        if Key not in self._objects:
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": _Body(self._objects[Key])}


class _Body:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


def _svc(monkeypatch, prefixes, objects, bucket="bap-skills-us-east-1"):
    svc = SkillBundleService(bucket=bucket)
    monkeypatch.setattr(svc, "_s3", _FakeS3(prefixes, objects), raising=False)
    return svc


def test_list_skills_enumerates_prefixes(monkeypatch):
    svc = _svc(
        monkeypatch,
        prefixes=["skills/sales-analyst/"],
        objects={"skills/sales-analyst/SKILL.md": SKILL_MD},
    )
    skills = svc.list_skills()
    assert len(skills) == 1
    assert isinstance(skills[0], DiscoveredSkill)
    assert skills[0].name == "sales-analyst"
    assert skills[0].description == "Answers revenue questions."
    assert skills[0].uri == "s3://bap-skills-us-east-1/skills/sales-analyst/"


def test_list_skills_skips_prefix_without_skill_md(monkeypatch):
    svc = _svc(
        monkeypatch,
        prefixes=["skills/broken/"],
        objects={"skills/broken/notes.txt": b"x"},
    )
    assert svc.list_skills() == []


def test_list_skills_empty_when_bucket_unconfigured():
    svc = SkillBundleService(bucket="")
    assert svc.list_skills() == []


def test_read_published_skill_returns_md_and_source(monkeypatch):
    svc = _svc(
        monkeypatch,
        prefixes=["skills/sales-analyst/"],
        objects={"skills/sales-analyst/SKILL.md": SKILL_MD},
    )
    skill_md, source = svc.read_published_skill("sales-analyst")
    assert "# Sales Analyst" in skill_md
    assert source.uri == "s3://bap-skills-us-east-1/skills/sales-analyst/"
    assert "SKILL.md" in source.files


def test_read_published_skill_missing_raises(monkeypatch):
    svc = _svc(monkeypatch, prefixes=[], objects={})
    with pytest.raises(ValueError):
        svc.read_published_skill("ghost")
