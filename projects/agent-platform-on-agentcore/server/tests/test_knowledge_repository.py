"""
Tests for knowledge-base persistence.

The DynamoDB table resource is faked: what matters here is the ownership read
path (a query per owner plus the shared set, deduplicated) and that `update`
never clobbers fields it was not given — the provisioner writes one id at a time
and a full put_item from a stale copy would erase the others.
"""
import json
import os
import sys
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.knowledge import (  # noqa: E402
    SOURCE_S3,
    SOURCE_UPLOAD,
    STATUS_GATEWAY,
    STATUS_READY,
    KnowledgeBaseRecord,
)
from repositories.knowledge_repository import (  # noqa: E402
    OWNER_INDEX,
    KnowledgeRepository,
)


class FakeTable:
    """The subset of the boto3 Table resource the repository uses."""

    def __init__(self):
        self.items = {}
        self.queries = []

    def load(self):
        return None

    def put_item(self, Item):
        self.items[Item["kb_key"]] = dict(Item)

    def get_item(self, Key):
        item = self.items.get(Key["kb_key"])
        return {"Item": dict(item)} if item else {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeNames,
                    ExpressionAttributeValues, ReturnValues):
        item = self.items.setdefault(Key["kb_key"], dict(Key))
        for placeholder, name in ExpressionAttributeNames.items():
            value_key = placeholder.replace("#", ":")
            if value_key in ExpressionAttributeValues:
                item[name] = ExpressionAttributeValues[value_key]
        return {"Attributes": dict(item)}

    def query(self, IndexName, KeyConditionExpression, ScanIndexForward):
        self.queries.append(IndexName)
        owner = KeyConditionExpression._values[1]
        items = [i for i in self.items.values() if i.get("owner_id") == owner]
        items.sort(key=lambda i: i.get("created_at", ""), reverse=not ScanIndexForward)
        return {"Items": [dict(i) for i in items]}

    def scan(self, **kwargs):
        items = list(self.items.values())
        # The only filter the repository uses is `shared == True`.
        if "FilterExpression" in kwargs:
            items = [i for i in items if i.get("shared")]
        return {"Items": [dict(i) for i in items]}

    def delete_item(self, Key):
        self.items.pop(Key["kb_key"], None)


def repository():
    repo = KnowledgeRepository.__new__(KnowledgeRepository)
    repo.table_name = "knowledge"
    repo.table = FakeTable()
    return repo


def record(kb_key, owner_id="user-1", shared=False, created_at="2026-08-05T00:00:00Z"):
    return KnowledgeBaseRecord(
        kb_key=kb_key,
        owner_id=owner_id,
        owner_name="alice",
        shared=shared,
        name=kb_key,
        created_at=created_at,
        updated_at=created_at,
    )


def test_a_record_round_trips():
    repo = repository()
    repo.put(record("kb_a_1"))

    stored = repo.get("kb_a_1")
    assert stored.kb_key == "kb_a_1"
    assert stored.owner_id == "user-1"
    assert stored.shared is False


def test_a_missing_record_is_none_rather_than_an_error():
    assert repository().get("kb_missing") is None


def test_the_source_type_and_its_settings_survive_a_round_trip():
    """`put` builds the item field by field, so a new field is easy to drop.

    Dropping this one is not a cosmetic loss: an S3 knowledge base whose source
    type does not come back reads as an upload one, which lets uploads through to a
    connector that rejects them and refuses the sync that would actually work.
    """
    repo = repository()
    stored = record("kb_s3_1")
    stored.source_type = SOURCE_S3
    stored.source_config = {"bucket_name": "corp-docs", "prefix": "exports/"}
    repo.put(stored)

    read = repo.get("kb_s3_1")

    assert read.source_type == SOURCE_S3
    assert read.source_config == {"bucket_name": "corp-docs", "prefix": "exports/"}


def test_a_record_written_before_source_types_reads_as_an_upload():
    repo = repository()
    repo.table.items["kb_old_1"] = {
        "kb_key": "kb_old_1",
        "owner_id": "user-1",
        "name": "Old",
        "status": "READY",
    }

    read = repo.get("kb_old_1")

    assert read.source_type == SOURCE_UPLOAD
    assert read.source_config == {}


def test_numbers_in_the_source_config_come_back_json_serialisable():
    """DynamoDB hands back Decimal, which boto3 cannot send as a parameter."""
    repo = repository()
    stored = record("kb_s3_2")
    stored.source_type = SOURCE_S3
    stored.source_config = {"bucket_name": "corp-docs", "max_files": 25}
    repo.put(stored)
    # What the real client returns for a stored number.
    repo.table.items["kb_s3_2"]["source_config"]["max_files"] = Decimal("25")

    read = repo.get("kb_s3_2")

    assert read.source_config["max_files"] == 25
    assert not isinstance(read.source_config["max_files"], Decimal)
    json.dumps(read.source_config)


def test_update_touches_only_the_named_fields():
    """The provisioner writes one id per step; the rest must survive."""
    repo = repository()
    repo.put(record("kb_a_1"))
    repo.update("kb_a_1", kb_id="ABCDEFGHIJ")

    updated = repo.update("kb_a_1", gateway_id="gw-1", status=STATUS_GATEWAY)
    assert updated.kb_id == "ABCDEFGHIJ"
    assert updated.gateway_id == "gw-1"
    assert updated.status == STATUS_GATEWAY
    assert updated.owner_name == "alice"


def test_update_stamps_updated_at_because_the_revive_check_reads_it():
    repo = repository()
    repo.put(record("kb_a_1"))

    updated = repo.update("kb_a_1", status=STATUS_READY)
    assert updated.updated_at != "2026-08-05T00:00:00Z"


def test_the_owner_listing_uses_the_index_not_a_scan():
    repo = repository()
    repo.put(record("kb_a_1", owner_id="user-1"))
    repo.put(record("kb_b_1", owner_id="user-2"))

    mine = repo.list_for_owner("user-1")
    assert [r.kb_key for r in mine] == ["kb_a_1"]
    assert repo.table.queries == [OWNER_INDEX]


def test_visible_to_merges_my_own_with_shared_ones():
    repo = repository()
    repo.put(record("kb_mine_1", owner_id="user-1", created_at="2026-08-01T00:00:00Z"))
    repo.put(record("kb_shared_1", owner_id="user-2", shared=True, created_at="2026-08-02T00:00:00Z"))
    repo.put(record("kb_theirs_1", owner_id="user-2", created_at="2026-08-03T00:00:00Z"))

    visible = repo.visible_to("user-1")
    assert [r.kb_key for r in visible] == ["kb_shared_1", "kb_mine_1"]


def test_my_own_shared_knowledge_base_is_listed_once():
    repo = repository()
    repo.put(record("kb_mine_1", owner_id="user-1", shared=True))

    assert [r.kb_key for r in repo.visible_to("user-1")] == ["kb_mine_1"]


def test_gateway_arns_are_collected_for_the_registry_sync_exclusion():
    repo = repository()
    repo.put(record("kb_a_1"))
    repo.update("kb_a_1", gateway_arn="arn:aws:bedrock-agentcore:us-east-1:1:gateway/a-1")
    repo.put(record("kb_b_1"))

    assert repo.all_gateway_arns() == {
        "arn:aws:bedrock-agentcore:us-east-1:1:gateway/a-1"
    }


def test_delete_removes_the_item():
    repo = repository()
    repo.put(record("kb_a_1"))
    repo.delete("kb_a_1")

    assert repo.get("kb_a_1") is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
