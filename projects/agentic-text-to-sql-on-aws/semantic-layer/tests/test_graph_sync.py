"""record_to_cypher / NeptuneGraphClient 단위 테스트."""

from __future__ import annotations

import json

from semantic_layer.graph_sync import (
    CypherStatement,
    NeptuneGraphClient,
    record_to_cypher,
)
from semantic_layer.repository import to_attribute_value

from .fakes import FakeNeptuneClient


def _image(data: dict) -> dict:
    """평범한 dict → Streams NewImage/OldImage(AttributeValue map)."""
    return {k: to_attribute_value(v) for k, v in data.items()}


def _record(event: str, *, new: dict | None = None, old: dict | None = None, sk: str = "v0"):
    ddb: dict = {"Keys": {"sk": {"S": sk}}, "SequenceNumber": "100"}
    if new is not None:
        ddb["NewImage"] = _image({**new, "sk": sk})
    if old is not None:
        ddb["OldImage"] = _image({**old, "sk": sk})
    return {"eventName": event, "dynamodb": ddb}


# --- 비-v0 무시 --------------------------------------------------------------


def test_non_v0_record_ignored():
    rec = _record(
        "INSERT",
        new={"entity_type": "table", "table": "orders", "status": "published"},
        sk="v3",
    )
    assert record_to_cypher(rec) == []


# --- published upsert --------------------------------------------------------


def test_published_table_merges():
    rec = _record(
        "INSERT", new={"entity_type": "table", "table": "orders", "status": "published"}
    )
    stmts = record_to_cypher(rec)
    assert len(stmts) == 1
    assert "MERGE (t:Table" in stmts[0].query
    assert stmts[0].parameters == {"name": "orders"}


def test_published_column_merges_with_has_column_edge():
    rec = _record(
        "INSERT",
        new={
            "entity_type": "column",
            "table": "orders",
            "column": "total_amount",
            "status": "published",
        },
    )
    stmts = record_to_cypher(rec)
    assert len(stmts) == 1
    q = stmts[0].query
    assert "MERGE (c:Column {key: $key})" in q
    assert "HAS_COLUMN" in q
    assert stmts[0].parameters["key"] == "orders.total_amount"


def test_published_join_merges_edge_with_on():
    rec = _record(
        "MODIFY",
        new={
            "entity_type": "join",
            "left_table": "orders",
            "right_table": "customers",
            "join_on": "orders.customer_id = customers.id",
            "status": "published",
        },
    )
    stmts = record_to_cypher(rec)
    assert len(stmts) == 1
    assert "JOINS" in stmts[0].query
    assert stmts[0].parameters["on"] == "orders.customer_id = customers.id"


def test_published_term_merges_node_and_maps_to():
    rec = _record(
        "INSERT",
        new={
            "entity_type": "term",
            "term": "매출",
            "status": "published",
            "maps_to": [
                {"table": "orders", "column": "total_amount"},
                {"table": "orders", "column": None},
            ],
        },
    )
    stmts = record_to_cypher(rec)
    # Term MERGE + column MAPS_TO + table MAPS_TO = 3.
    assert len(stmts) == 3
    assert "MERGE (t:Term" in stmts[0].query
    assert any("MAPS_TO" in s.query and "Column" in s.query for s in stmts)
    assert any("MAPS_TO" in s.query and "Table" in s.query for s in stmts)


# --- candidate / REMOVE 삭제 -------------------------------------------------


def test_candidate_status_deletes_node():
    rec = _record(
        "MODIFY", new={"entity_type": "table", "table": "orders", "status": "candidate"}
    )
    stmts = record_to_cypher(rec)
    assert len(stmts) == 1
    assert "DETACH DELETE" in stmts[0].query


def test_remove_event_deletes_node():
    rec = _record("REMOVE", old={"entity_type": "term", "term": "매출", "status": "published"})
    stmts = record_to_cypher(rec)
    assert len(stmts) == 1
    assert "DETACH DELETE" in stmts[0].query
    assert stmts[0].parameters == {"name": "매출"}


def test_remove_join_deletes_edge_only():
    rec = _record(
        "REMOVE",
        old={
            "entity_type": "join",
            "left_table": "orders",
            "right_table": "customers",
            "status": "published",
        },
    )
    stmts = record_to_cypher(rec)
    assert len(stmts) == 1
    assert "DELETE j" in stmts[0].query
    assert "DETACH" not in stmts[0].query


def test_published_to_candidate_demotion_deletes():
    rec = _record(
        "MODIFY",
        new={
            "entity_type": "column",
            "table": "orders",
            "column": "x",
            "status": "candidate",
        },
    )
    stmts = record_to_cypher(rec)
    assert "DETACH DELETE" in stmts[0].query


def test_record_without_entity_type_ignored():
    rec = _record("INSERT", new={"status": "published"})
    assert record_to_cypher(rec) == []


# --- NeptuneGraphClient ------------------------------------------------------


def test_neptune_client_executes_all_statements():
    fake = FakeNeptuneClient()
    client = NeptuneGraphClient(client=fake)
    stmts = [
        CypherStatement("MERGE (t:Table {name: $n})", {"n": "orders"}),
        CypherStatement("MERGE (t:Table {name: $n})", {"n": "customers"}),
    ]
    count = client.execute(stmts)
    assert count == 2
    assert len(fake.calls) == 2
    # parameters 는 json 문자열로 전달돼야 함.
    assert json.loads(fake.calls[0]["parameters"]) == {"n": "orders"}


def test_neptune_client_empty_statements_noop():
    fake = FakeNeptuneClient()
    client = NeptuneGraphClient(client=fake)
    assert client.execute([]) == 0
    assert fake.calls == []
