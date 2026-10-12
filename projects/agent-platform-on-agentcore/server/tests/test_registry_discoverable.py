"""
The discovery data plane: BatchGetDiscoverableRegistryRecord and what it buys.

Two things ride on it. Listing used to fan out one GetRegistryRecord per agent
record to recover the invoke ARN (ListRegistryRecords omits descriptors), which
is one call per record against a 10 TPS quota; BatchGet returns up to 100
approved records with descriptors in one call. And the chat gate: editing an
APPROVED record opens a DRAFT revision while the approved one stays
discoverable (AWS's dual-revision model), so a record whose *latest* revision
is DRAFT may still have an approved revision to chat with — the data plane is
the only API that returns it.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.registry_service import RegistryService  # noqa: E402

RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:1:runtime/r-1"
OLD_RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:1:runtime/r-old"


def _card(arn):
    import json

    return json.dumps({"name": "a", "url": arn, "agentRuntimeArn": arn})


def _summary(rid, status="APPROVED", record_type="AGENT"):
    return {"recordId": rid, "name": rid, "recordType": record_type, "status": status}


def _full(rid, arn=RUNTIME_ARN, status="APPROVED"):
    return {
        "recordId": rid,
        "name": rid,
        "recordType": "AGENT",
        "status": status,
        "descriptors": {"a2aAgentCard": {"data": _card(arn), "dataSchemaVersion": "0.3.0"}},
    }


class StubControl:
    def __init__(self, summaries, details=None):
        self.summaries = summaries
        self.details = details or {}
        self.list_calls = []
        self.get_calls = []

    def list_registry_records(self, **params):
        self.list_calls.append(params)
        return {"registryRecords": self.summaries}

    def get_registry_record(self, **params):
        self.get_calls.append(params["recordId"])
        return self.details[params["recordId"]]


class StubData:
    def __init__(self, discoverable):
        self.discoverable = discoverable  # recordId -> full record
        self.batch_calls = []

    def batch_get_discoverable_registry_record(self, **params):
        self.batch_calls.append(params)
        ids = params["entries"][0]["recordIds"]
        found = [self.discoverable[i] for i in ids if i in self.discoverable]
        errors = [
            {"recordId": i, "errorCode": "RESOURCE_NOT_FOUND"}
            for i in ids
            if i not in self.discoverable
        ]
        return {"registryRecords": found, "errors": errors}


def _service(summaries, details=None, discoverable=None):
    service = RegistryService(registry_id="reg-1", region="us-east-1")
    control = StubControl(summaries, details)
    data = StubData(discoverable or {})
    service._registry_control = control
    service._registry_data = data
    return service, control, data


def test_batch_get_chunks_by_100_and_indexes_by_id():
    ids = [f"rec-{i}" for i in range(150)]
    service, _, data = _service([], discoverable={i: _full(i) for i in ids})
    result = service.batch_get_discoverable(ids)
    assert [len(c["entries"][0]["recordIds"]) for c in data.batch_calls] == [100, 50]
    assert data.batch_calls[0]["entries"][0]["registryId"] == "reg-1"
    assert set(result) == set(ids)


def test_batch_get_of_nothing_makes_no_call():
    service, _, data = _service([])
    assert service.batch_get_discoverable([]) == {}
    assert data.batch_calls == []


def test_list_hydrates_approved_summaries_with_one_batch_get():
    ids = ["a", "b", "c"]
    service, control, data = _service(
        [_summary(i) for i in ids], discoverable={i: _full(i) for i in ids}
    )
    records = service.list_records(descriptor_type="A2A")
    assert len(data.batch_calls) == 1
    assert control.get_calls == []
    assert all(r.agent_runtime_arn == RUNTIME_ARN for r in records)
    assert all(r.discoverable is True for r in records)


def test_list_falls_back_to_get_for_records_the_data_plane_lacks():
    # A DRAFT with no approved revision is invisible to the data plane; its
    # binding still has to come from somewhere, so it is fetched individually.
    service, control, data = _service(
        [_summary("draft", status="DRAFT")],
        details={"draft": _full("draft", status="DRAFT")},
    )
    records = service.list_records(descriptor_type="A2A")
    assert control.get_calls == ["draft"]
    assert records[0].agent_runtime_arn == RUNTIME_ARN
    assert records[0].discoverable is False


def test_list_marks_a_draft_with_an_approved_revision_discoverable():
    service, _, _ = _service(
        [_summary("edited", status="DRAFT")],
        details={"edited": _full("edited", status="DRAFT")},
        discoverable={"edited": _full("edited", arn=OLD_RUNTIME_ARN)},
    )
    record = service.list_records(descriptor_type="A2A")[0]
    assert record.status == "DRAFT"
    assert record.discoverable is True


def test_list_reads_metadata_in_one_batch_but_never_gets_records_that_need_no_descriptor():
    """One BatchGet always runs now: the team filter reads custom metadata off the
    approved revision, so even a list of skills needs it. It is still one call and
    no per-record Get for an approved record (a never-approved one still costs a
    Get, because only a Get knows its metadata)."""
    full = {**_full("s"), "recordType": "SKILL"}
    del full["descriptors"]
    service, control, data = _service([_summary("s", record_type="SKILL")], discoverable={"s": full})
    service.list_records()
    assert len(data.batch_calls) == 1 and control.get_calls == []


def test_chattable_record_prefers_the_latest_when_it_is_approved():
    service, _, data = _service([], details={"a": _full("a")})
    record = service.chattable_record("a")
    assert record.status == "APPROVED" and record.revision is None
    assert data.batch_calls == []


def test_chattable_record_falls_back_to_the_approved_revision():
    service, _, _ = _service(
        [],
        details={"a": _full("a", status="DRAFT")},
        discoverable={"a": _full("a", arn=OLD_RUNTIME_ARN)},
    )
    record = service.chattable_record("a")
    assert record.status == "APPROVED"
    assert record.revision == "approved"
    # The binding is the approved revision's, not the draft's: that is the
    # endpoint curation signed off on.
    assert record.agent_runtime_arn == OLD_RUNTIME_ARN


def test_chattable_record_returns_the_latest_when_nothing_is_discoverable():
    service, _, _ = _service([], details={"a": _full("a", status="REJECTED")})
    record = service.chattable_record("a")
    assert record.status == "REJECTED" and record.revision is None


def test_list_copies_custom_metadata_from_the_approved_revision():
    # ListRegistryRecords omits customMetadata (verified live 2026-10-10); the
    # BatchGet record has it, and the card chips read it off the summary.
    full = {**_full("s"), "recordType": "SKILL", "customMetadata": {"team": "platform"},
            "customMetadataSchemaComplianceStatus": "COMPLIANT"}
    del full["descriptors"]
    service, control, data = _service([_summary("s", record_type="SKILL"), _summary("a")],
                                       discoverable={"s": full, "a": _full("a")})
    records = {r.record_id: r for r in service.list_records()}
    assert records["s"].custom_metadata == {"team": "platform"}
    assert records["s"].compliance_status == "COMPLIANT"
    assert len(data.batch_calls) == 1
