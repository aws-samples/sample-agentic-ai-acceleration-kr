"""GET /records and GET /records/{id} degrade to deployed resources."""
import os
import sys

import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routes.registry as reg  # noqa: E402
from models.registry import RegistryRecordSummary  # noqa: E402


def _deployed(record_id, name):
    return RegistryRecordSummary(record_id=record_id, name=name, status="APPROVED",
                                 agent_runtime_arn="arn:x", source="deployed")


def test_list_records_falls_back_on_client_error(monkeypatch):
    monkeypatch.setattr(reg, "registry_enabled", lambda: True)

    def boom(**kw):
        raise ClientError({"Error": {"Code": "AccessDeniedException"}}, "ListRegistryRecords")

    monkeypatch.setattr(reg._service, "list_records", boom)
    monkeypatch.setattr(reg._sync_service, "deployed_agent_records",
                        lambda: [_deployed("deployed:arn:x", "x")])
    out = reg.list_records(type=None, status=None, name=None, user=None)
    assert out["count"] == 1
    assert out["records"][0].source == "deployed"


def test_list_records_falls_back_when_disabled(monkeypatch):
    monkeypatch.setattr(reg, "registry_enabled", lambda: False)
    monkeypatch.setattr(reg._service, "list_records",
                        lambda **kw: (_ for _ in ()).throw(AssertionError("registry called")))
    monkeypatch.setattr(reg._sync_service, "deployed_agent_records",
                        lambda: [_deployed("deployed:arn:y", "y")])
    out = reg.list_records(type=None, status=None, name=None, user=None)
    assert out["records"][0].name == "y"


def test_get_record_synthesises_deployed_detail_without_calling_aws(monkeypatch):
    # A deployed:<arn> id names no real registry record, so it must be answered
    # from the same fallback the listing uses — never handed to GetRegistryRecord,
    # which rejects it as a malformed recordId.
    def must_not_call(*a, **kw):
        raise AssertionError("get_record must not hit the registry for a deployed id")

    monkeypatch.setattr(reg._service, "get_record", must_not_call)
    monkeypatch.setattr(reg._sync_service, "deployed_agent_records",
                        lambda: [_deployed("deployed:arn:z", "z")])
    detail = reg.get_record(record_id="deployed:arn:z", user=None)
    assert detail.record_id == "deployed:arn:z"
    assert detail.source == "deployed"
    assert detail.status == "APPROVED"


def test_get_record_404s_for_an_unknown_deployed_id(monkeypatch):
    monkeypatch.setattr(reg._sync_service, "deployed_agent_records", lambda: [])
    with pytest.raises(HTTPException) as exc:
        reg.get_record(record_id="deployed:arn:gone", user=None)
    assert exc.value.status_code == 404
