"""Registry-off skill upload: publish/list/delete straight to the bucket.

When the registry is off there is no AGENT_SKILLS record to create, but the
bundle still belongs in `s3://SKILLS_BUCKET/skills/<name>/` so the harness's
bucket-skill picker can offer it. These routes are the bucket-only half of the
existing `/skills` flow — publish without a record, list what is there, delete a
prefix — and must never touch the registry.
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routes.registry as reg  # noqa: E402
from models.skill import InvalidSkillBundle, SkillSource  # noqa: E402
from services.skill_bundle_service import DiscoveredSkill  # noqa: E402

BUCKET = "ap-skills"
URI = f"s3://{BUCKET}/skills/weather/"


class _Info:
    def __init__(self, valid=True):
        self.valid = valid
        self.name = "weather"
        self.description = "forecasts"
        self.skill_md = "---\nname: weather\n---\n"
        self.errors = [] if valid else ["bad"]


class _Staged:
    def __init__(self, valid=True):
        self.info = _Info(valid)


def _run(coro):
    return asyncio.run(coro)


def _no_registry(monkeypatch):
    """Any registry call from a bucket route is a bug — make it explode."""
    def boom(*a, **k):
        raise AssertionError("bucket skill route touched the registry")
    monkeypatch.setattr(reg._service, "create_record", boom)
    monkeypatch.setattr(reg._service, "get_record", boom)
    monkeypatch.setattr(reg._service, "list_records", boom)


def test_publish_writes_to_bucket_and_returns_the_skill_without_a_record(monkeypatch):
    _no_registry(monkeypatch)
    monkeypatch.setattr(reg, "_stage", lambda file: _async(_Staged(valid=True)))
    published = {}
    monkeypatch.setattr(
        reg._skills, "publish",
        lambda staged: published.setdefault("src", SkillSource(uri=URI, files=["SKILL.md"], total_bytes=1)),
    )
    out = _run(reg.publish_bucket_skill(file=object(), _=None))
    assert out.name == "weather"
    assert out.uri == URI
    assert "src" in published


def test_publish_rejects_an_invalid_bundle(monkeypatch):
    # _fail turns InvalidSkillBundle into a 400 (same as POST /skills), so an
    # invalid bundle never reaches publish.
    from fastapi import HTTPException
    _no_registry(monkeypatch)
    monkeypatch.setattr(reg, "_stage", lambda file: _async(_Staged(valid=False)))
    monkeypatch.setattr(reg._skills, "publish",
                        lambda staged: (_ for _ in ()).throw(AssertionError("published invalid")))
    with pytest.raises(HTTPException) as exc:
        _run(reg.publish_bucket_skill(file=object(), _=None))
    assert exc.value.status_code == 400


def test_list_bucket_skills(monkeypatch):
    _no_registry(monkeypatch)
    monkeypatch.setattr(
        reg._skills, "list_skills",
        lambda: [DiscoveredSkill(name="weather", description="forecasts", uri=URI)],
    )
    out = reg.list_bucket_skills(_=None)
    assert out["count"] == 1
    assert out["skills"][0].uri == URI


def test_delete_bucket_skill_removes_the_prefix(monkeypatch):
    _no_registry(monkeypatch)
    monkeypatch.setattr(reg._skills, "bucket", BUCKET)
    monkeypatch.setattr(reg._skills, "delete_prefix", lambda uri: 3)
    out = reg.delete_bucket_skill(uri=URI, _=None)
    assert out["deleted"] == URI
    assert out["removed"] == 3


def test_delete_bucket_skill_refuses_a_uri_outside_our_bucket(monkeypatch):
    from fastapi import HTTPException
    _no_registry(monkeypatch)
    monkeypatch.setattr(reg._skills, "bucket", BUCKET)
    monkeypatch.setattr(reg._skills, "delete_prefix",
                        lambda uri: (_ for _ in ()).throw(AssertionError("deleted foreign uri")))
    with pytest.raises(HTTPException) as exc:
        reg.delete_bucket_skill(uri="s3://someone-elses-bucket/skills/x/", _=None)
    assert exc.value.status_code == 400


async def _async(value):
    return value
