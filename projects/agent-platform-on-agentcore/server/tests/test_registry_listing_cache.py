"""The registry listing is read once per window and shared, and the catalog reads
descriptors in bulk.

Covers the process-wide listing cache (single-flight loads, write invalidation,
the degraded-listing exemption), `record_details`, and the harness catalog path
that uses it. No real AWS: the clients are in-memory stubs.
"""
import json
import os
import sys
import threading
import time

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import services.harness_service as hs  # noqa: E402
from models.registry import DESCRIPTOR_MCP  # noqa: E402
from services.registry_service import ReadThroughCache, RegistryService  # noqa: E402

GATEWAY = "arn:aws:bedrock-agentcore:us-east-1:1:gateway/gw-1"
UPDATED = "2026-10-18T00:00:00+00:00"


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def full(record_id, record_type, status, descriptors=None):
    return {
        "recordId": record_id, "name": record_id, "recordType": record_type, "status": status,
        "recordVersion": "1", "updatedAt": UPDATED, "descriptors": descriptors or {},
    }


def mcp(record_id, status="APPROVED"):
    data = json.dumps({"name": record_id, "gatewayArn": GATEWAY})
    return full(record_id, "MCP", status, {"mcpServer": {"data": data}})


def skill(record_id, status="APPROVED"):
    descriptors = {"agentSkillsDefinition": {"data": "{}", "additionalData": {"skillMd": {"data": "# Skill"}}}}
    return full(record_id, "SKILL", status, descriptors)


class StubControl:
    """ListRegistryRecords / GetRegistryRecord / writes over an in-memory registry."""

    def __init__(self, records, gate=None):
        self.records = {r["recordId"]: dict(r) for r in records}
        self.list_calls = 0
        self.get_calls = []
        self.fail_gets = set()
        self.fail_writes = False
        self.gate = gate  # holds the first listing until the test releases it

    def list_registry_records(self, registryId, maxResults=100, filters=None, nextToken=None):
        self.list_calls += 1
        if self.gate is not None and self.list_calls == 1:
            self.gate.wait(5)
        wanted = {v for f in filters or [] if f["name"] == "recordType" for v in f["values"]}
        rows = [
            {k: v for k, v in r.items() if k != "descriptors"}
            for r in self.records.values()
            if not wanted or r["recordType"] in wanted
        ]
        return {"registryRecords": rows}

    def get_registry_record(self, registryId, recordId):
        self.get_calls.append(recordId)
        if recordId in self.fail_gets:
            raise ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
                              "GetRegistryRecord")
        return self.records[recordId]

    def update_registry_record_status(self, registryId, recordId, status, statusReason):
        if self.fail_writes:
            raise ClientError({"Error": {"Code": "ValidationException", "Message": "rejected"}},
                              "UpdateRegistryRecordStatus")
        self.records[recordId]["status"] = status

    def delete_registry_record(self, registryId, recordId):
        self.records.pop(recordId)


class StubData:
    """BatchGetDiscoverableRegistryRecord: only the records listed as discoverable."""

    def __init__(self, discoverable):
        self.discoverable = discoverable
        self.batch_calls = []
        self.fail = False

    def batch_get_discoverable_registry_record(self, entries):
        ids = entries[0]["recordIds"]
        self.batch_calls.append(list(ids))
        if self.fail:
            raise ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
                              "BatchGetDiscoverableRegistryRecord")
        return {"registryRecords": [self.discoverable[i] for i in ids if i in self.discoverable]}


def attach(service, control, data):
    service._registry_control = control
    service._registry_data = data
    return service


def make_service(records, discoverable=None, gate=None):
    if discoverable is None:
        discoverable = {r["recordId"]: r for r in records if r["status"] == "APPROVED"}
    control, data = StubControl(records, gate=gate), StubData(discoverable)
    return attach(RegistryService(registry_id="reg-1", region="us-east-1"), control, data), control, data


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert predicate(), "timed out waiting for the other thread"


# ── ReadThroughCache ───────────────────────────────────────────────────────


def test_a_cached_value_is_served_within_its_ttl_and_reloaded_after_it():
    clock = FakeClock()
    cache = ReadThroughCache(15, clock=clock)
    loads = []

    def load():
        loads.append(1)
        return len(loads)

    assert cache.get("k", load) == 1
    clock.now = 14
    assert cache.get("k", load) == 1
    clock.now = 15
    assert cache.get("k", load) == 2


def test_concurrent_misses_share_one_load():
    cache = ReadThroughCache(60)
    started, release = threading.Event(), threading.Event()
    loads = []

    def load():
        loads.append(1)
        started.set()
        release.wait(5)
        return "value"

    results = []
    threads = [threading.Thread(target=lambda: results.append(cache.get("k", load))) for _ in range(5)]
    threads[0].start()
    assert started.wait(5)
    for thread in threads[1:]:
        thread.start()
    time.sleep(0.05)
    release.set()
    for thread in threads:
        thread.join(5)
    assert loads == [1]
    assert results == ["value"] * 5


def test_a_failed_load_reaches_its_waiters_and_is_not_kept():
    cache = ReadThroughCache(60)
    started, release = threading.Event(), threading.Event()
    attempts = []

    def load():
        attempts.append(1)
        if len(attempts) == 1:
            started.set()
            release.wait(5)
            raise RuntimeError("registry down")
        return "ok"

    errors = []

    def call():
        try:
            cache.get("k", load)
        except RuntimeError as exc:
            errors.append(exc)

    leader = threading.Thread(target=call)
    leader.start()
    assert started.wait(5)
    waiter = threading.Thread(target=call)
    waiter.start()
    time.sleep(0.05)
    release.set()
    leader.join(5)
    waiter.join(5)
    assert len(errors) == 2
    assert cache.get("k", load) == "ok"  # the failure was not remembered
    assert len(attempts) == 2


def test_a_load_that_straddles_an_invalidation_is_not_stored():
    cache = ReadThroughCache(60)
    started, release = threading.Event(), threading.Event()
    values = iter(["read before the write", "read after the write"])

    def load():
        value = next(values)
        if value.startswith("read before"):
            started.set()
            release.wait(5)
        return value

    out = []
    thread = threading.Thread(target=lambda: out.append(cache.get("k", load)))
    thread.start()
    assert started.wait(5)
    cache.invalidate()
    release.set()
    thread.join(5)
    assert out == ["read before the write"]
    assert cache.get("k", load) == "read after the write"


def test_a_caller_after_an_invalidation_does_not_wait_for_an_older_load():
    cache = ReadThroughCache(60)
    started, release = threading.Event(), threading.Event()

    def slow():
        started.set()
        release.wait(5)
        return "old"

    old = threading.Thread(target=lambda: cache.get("k", slow))
    old.start()
    assert started.wait(5)
    cache.invalidate()

    result = []
    fresh = threading.Thread(target=lambda: result.append(cache.get("k", lambda: "new")))
    fresh.start()
    fresh.join(2)
    release.set()
    old.join(5)
    assert result == ["new"]


def test_a_value_the_predicate_refuses_is_returned_but_not_kept():
    cache = ReadThroughCache(60)
    loads = []

    def load():
        loads.append(1)
        return len(loads)

    assert cache.get("k", load, cacheable=lambda value: False) == 1
    assert cache.get("k", load, cacheable=lambda value: False) == 2


# ── RegistryService listings ───────────────────────────────────────────────


def test_a_listing_is_read_once_and_shared_by_every_service_instance():
    records = [mcp("rec-1"), mcp("rec-2")]
    control, data = StubControl(records), StubData({r["recordId"]: r for r in records})
    first = attach(RegistryService(registry_id="reg-1", region="us-east-1"), control, data)
    second = attach(RegistryService(registry_id="reg-1", region="us-east-1"), control, data)

    assert [r.record_id for r in first.list_records(descriptor_type=DESCRIPTOR_MCP)] == ["rec-1", "rec-2"]
    assert [r.record_id for r in second.list_records(descriptor_type=DESCRIPTOR_MCP)] == ["rec-1", "rec-2"]
    assert control.list_calls == 1


def test_each_caller_gets_its_own_copy_of_the_listing():
    service, control, _ = make_service([mcp("rec-1")])
    first = service.list_records()
    first[0].name = "changed by a caller"
    assert service.list_records()[0].name == "rec-1"
    assert control.list_calls == 1


def test_a_status_write_drops_the_listing():
    service, control, _ = make_service([mcp("rec-1", status="DRAFT")])
    assert service.list_records()[0].status == "DRAFT"

    service.update_status("rec-1", "approve")

    assert service.list_records()[0].status == "APPROVED"
    assert control.list_calls == 2


def test_a_failed_write_also_drops_the_listing():
    service, control, _ = make_service([mcp("rec-1", status="DRAFT")])
    service.list_records()
    control.fail_writes = True

    with pytest.raises(ClientError):
        service.update_status("rec-1", "approve")

    service.list_records()
    assert control.list_calls == 2


def test_a_delete_drops_the_listing():
    service, control, _ = make_service([mcp("rec-1"), mcp("rec-2")])
    assert len(service.list_records()) == 2

    service.delete_record("rec-2")

    assert [r.record_id for r in service.list_records()] == ["rec-1"]
    assert control.list_calls == 2


def test_a_listing_with_unknown_visibility_is_answered_but_read_again_next_time():
    service, control, data = make_service([mcp("rec-1")])
    data.fail = True

    first = service.list_records()
    assert first[0].visibility_known is False  # fail closed for the team filter

    data.fail = False
    second = service.list_records()
    assert second[0].visibility_known is True
    assert control.list_calls == 2


def test_concurrent_callers_share_one_registry_read():
    gate = threading.Event()
    service, control, _ = make_service([mcp("rec-1"), mcp("rec-2")], gate=gate)
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(service.list_records(descriptor_type=DESCRIPTOR_MCP)))
        for _ in range(5)
    ]
    threads[0].start()
    wait_until(lambda: control.list_calls == 1)
    for thread in threads[1:]:
        thread.start()
    time.sleep(0.05)
    gate.set()
    for thread in threads:
        thread.join(5)
    assert control.list_calls == 1
    assert len(results) == 5 and all(len(r) == 2 for r in results)


def test_batch_get_chunks_run_side_by_side_and_merge():
    ids = [f"rec-{i}" for i in range(60)]
    service, _, data = make_service([], discoverable={i: mcp(i) for i in ids})
    found = service.batch_get_discoverable(ids)
    assert sorted(len(call) for call in data.batch_calls) == [10, 25, 25]
    assert set(found) == set(ids)


# ── record_details ─────────────────────────────────────────────────────────


def test_approved_records_come_from_one_batch_and_only_the_rest_from_gets():
    service, control, data = make_service([mcp("rec-a"), mcp("rec-b"), mcp("rec-draft", status="DRAFT")])
    summaries = service.list_records()
    # The listing already read the draft (it is not discoverable), and that read
    # is memoised by revision, so the details below need no Get of their own.
    control.get_calls.clear()
    data.batch_calls.clear()

    details = service.record_details(summaries)

    assert data.batch_calls == [["rec-a", "rec-b"]]
    assert control.get_calls == []
    assert set(details) == {"rec-a", "rec-b", "rec-draft"}
    assert details["rec-a"].descriptor_content["server"]["gatewayArn"] == GATEWAY


def test_a_failed_bulk_read_falls_back_to_one_get_per_record():
    service, control, data = make_service([mcp("rec-a")])
    summaries = service.list_records()
    data.fail = True
    control.get_calls.clear()

    details = service.record_details(summaries)

    assert "rec-a" in details
    assert control.get_calls == ["rec-a"]


def test_a_record_that_cannot_be_read_is_left_out():
    service, control, _ = make_service([mcp("rec-a", status="DRAFT")], discoverable={})
    control.fail_gets = {"rec-a"}
    summaries = service.list_records()

    assert service.record_details(summaries) == {}


# ── harness catalog ────────────────────────────────────────────────────────


def test_catalog_reads_every_composable_descriptor_in_bulk(monkeypatch):
    monkeypatch.setattr(hs, "registry_enabled", lambda: True)
    records = [mcp("rec-mcp-1"), mcp("rec-mcp-2"), skill("rec-skill")]
    service, control, _ = make_service(records)
    harness = hs.HarnessService(
        registry=service,
        region="us-east-1",
        execution_role_arn="arn:aws:iam::1:role/harness",
        skills_bucket="",
    )

    catalog = harness.catalog()

    assert control.get_calls == []  # one GetRegistryRecord per record was 102 calls per page
    assert [m.record_id for m in catalog.mcp_servers] == ["rec-mcp-1", "rec-mcp-2"]
    assert all(m.composable for m in catalog.mcp_servers)
    assert [s.record_id for s in catalog.skills] == ["rec-skill"]
    assert catalog.skills[0].composable


# ── chattable_record ───────────────────────────────────────────────────────


def agent(record_id, status="APPROVED"):
    data = json.dumps({"name": record_id, "url": "https://x"})
    return full(record_id, "A2A", status, {"a2aAgentCard": {"data": data}})


def test_a_chat_turn_reuses_the_bound_record_instead_of_a_get_each():
    """The approval gate and the target resolution each asked for it: two
    GetRegistryRecord per new thread, throttled at 50 concurrent new chats."""
    service, control, _ = make_service([agent("rec-1")])
    for _ in range(5):
        assert service.chattable_record("rec-1").status == "APPROVED"
    assert control.get_calls == ["rec-1"]


def test_a_status_change_through_this_server_is_seen_by_the_next_turn():
    service, control, _ = make_service([agent("rec-1")])
    service.chattable_record("rec-1")
    service.update_status("rec-1", "deprecate")
    reads_before = len(control.get_calls)
    service.chattable_record("rec-1")
    # Read again rather than served from before the write. (Which revision it
    # binds to is chattable_record's rule — the approved one while AWS serves it.)
    assert len(control.get_calls) == reads_before + 1


def test_a_throttled_read_is_not_cached_and_callers_get_copies():
    service, control, _ = make_service([agent("rec-1")])
    control.fail_gets.add("rec-1")
    with pytest.raises(ClientError):
        service.chattable_record("rec-1")
    control.fail_gets.clear()
    first = service.chattable_record("rec-1")
    first.name = "mutated by a caller"
    assert service.chattable_record("rec-1").name == "rec-1"
