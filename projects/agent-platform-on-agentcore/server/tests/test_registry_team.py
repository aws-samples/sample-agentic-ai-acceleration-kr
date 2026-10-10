"""Every listed record carries the approved revision's custom metadata, so the
team filter has something to read without a per-record Get."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.registry import DESCRIPTOR_A2A, DESCRIPTOR_MCP, RegistryRecordDetail, RegistryRecordSummary  # noqa: E402
from services import registry_service as rs  # noqa: E402


def service(approved=None, details=None, fail_batch=False):
    svc = rs.RegistryService.__new__(rs.RegistryService)
    svc.registry_id = "r"
    svc.region = "us-east-1"
    svc._team_cache = {}
    approved = approved or {}
    details = details or {}

    def batch(ids):
        if fail_batch:
            raise RuntimeError("data plane down")
        return {i: approved[i] for i in ids if i in approved}

    svc.batch_get_discoverable = batch
    svc.get_record = lambda record_id: details[record_id]
    return svc


def summary(record_id, status="APPROVED", dtype=DESCRIPTOR_MCP, **kw):
    return RegistryRecordSummary(record_id=record_id, name=record_id, descriptor_type=dtype, status=status, **kw)


def approved_record(record_id, team):
    return {"recordId": record_id, "name": record_id, "recordType": "MCP", "status": "APPROVED",
            "customMetadata": {"team": team}}


def test_hydrate_all_attaches_metadata_to_every_approved_record():
    svc = service(approved={"m1": approved_record("m1", "finance"), "m2": approved_record("m2", "shared")})
    out = svc._hydrate_all([summary("m1"), summary("m2")])
    assert [r.team for r in out] == ["finance", None]
    assert all(r.discoverable for r in out)


def test_never_approved_record_is_read_individually_for_its_metadata():
    draft = RegistryRecordDetail(record_id="d1", name="d1", descriptor_type=DESCRIPTOR_MCP, status="DRAFT",
                                 custom_metadata={"team": "hr"})
    svc = service(details={"d1": draft})
    out = svc._hydrate_all([summary("d1", status="DRAFT")])
    assert out[0].team == "hr" and out[0].discoverable is False


def test_batch_failure_lists_records_with_unknown_visibility_not_500():
    svc = service(fail_batch=True)
    out = svc._hydrate_all([summary("m1")])
    assert out[0].team is None and out[0].discoverable is None
    # No metadata means no team to check: the filter must fail closed, not read "shared".
    assert out[0].visibility_known is False


def test_failed_get_of_a_never_approved_record_marks_visibility_unknown():
    svc = service()
    svc.get_record = lambda record_id: (_ for _ in ()).throw(RuntimeError("throttled"))
    out = svc._hydrate_all([summary("d1", status="DRAFT")])
    assert out[0].visibility_known is False


def test_hydrated_records_keep_visibility_known():
    svc = service(approved={"m1": approved_record("m1", "finance")})
    assert svc._hydrate_all([summary("m1")])[0].visibility_known is True


def test_team_of_record_uses_the_chattable_revision_and_caches(monkeypatch):
    svc = service()
    calls = []

    def chattable(record_id):
        calls.append(record_id)
        return RegistryRecordDetail(record_id=record_id, name="x", custom_metadata={"team": "finance"})

    svc.chattable_record = chattable
    assert svc.team_of_record("a1") == "finance"
    assert svc.team_of_record("a1") == "finance"
    assert calls == ["a1"]
