"""Harness composition keeps working with the registry off."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import services.harness_service as hs  # noqa: E402
from models.harness import ComposeHarnessRequest  # noqa: E402


def test_catalog_builds_without_registry(monkeypatch):
    monkeypatch.setattr(hs, "registry_enabled", lambda: False)
    svc = hs.HarnessService(execution_role_arn="arn:role", skills_bucket="")
    monkeypatch.setattr(svc.registry, "list_records",
                        lambda **k: (_ for _ in ()).throw(AssertionError("called registry")))
    cat = svc.catalog()
    assert cat.mcp_servers == []
    assert cat.skills == []
    assert cat.bucket_skills == []
    assert cat.aws_skill_categories
    assert cat.builtin_tools
    assert cat.configured is True


def test_catalog_degrades_when_registry_errors(monkeypatch):
    from botocore.exceptions import ClientError
    monkeypatch.setattr(hs, "registry_enabled", lambda: True)
    svc = hs.HarnessService(execution_role_arn="arn:role", skills_bucket="")

    def boom(**k):
        raise ClientError({"Error": {"Code": "AccessDeniedException"}}, "ListRegistryRecords")

    monkeypatch.setattr(svc.registry, "list_records", boom)
    cat = svc.catalog()
    assert cat.mcp_servers == [] and cat.skills == []


def test_compose_skips_registration_when_off(monkeypatch):
    monkeypatch.setattr(hs, "registry_enabled", lambda: False)
    svc = hs.HarnessService(execution_role_arn="arn:role")
    calls = {"spawn": 0}
    monkeypatch.setattr(svc, "create_harness",
                        lambda req: hs._to_harness_summary({"harnessId": "h1", "arn": "arn:h"}))
    monkeypatch.setattr(svc, "_spawn_registration",
                        lambda *a, **k: calls.__setitem__("spawn", calls["spawn"] + 1))
    svc.compose_and_register(ComposeHarnessRequest(name="hx"))
    assert calls["spawn"] == 0
