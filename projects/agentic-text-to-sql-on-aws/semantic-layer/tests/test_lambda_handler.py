"""lambda_handler 파샬 배치 응답 단위 테스트."""

from __future__ import annotations

from semantic_layer import lambda_handler
from semantic_layer.graph_sync import NeptuneGraphClient
from semantic_layer.repository import to_attribute_value

from .fakes import FakeNeptuneClient


def _record(seq: str, entity: dict, *, event: str = "INSERT", sk: str = "v0") -> dict:
    image = {k: to_attribute_value(v) for k, v in {**entity, "sk": sk}.items()}
    return {
        "eventName": event,
        "dynamodb": {"Keys": {"sk": {"S": sk}}, "NewImage": image, "SequenceNumber": seq},
    }


def test_all_success_returns_no_failures():
    fake = FakeNeptuneClient()
    client = NeptuneGraphClient(client=fake)
    event = {
        "Records": [
            _record("1", {"entity_type": "table", "table": "orders", "status": "published"}),
            _record("2", {"entity_type": "table", "table": "customers", "status": "published"}),
        ]
    }
    resp = lambda_handler.handler(event, graph_client=client)
    assert resp == {"batchItemFailures": []}
    assert len(fake.calls) == 2


def test_non_v0_records_skipped_without_execute():
    fake = FakeNeptuneClient()
    client = NeptuneGraphClient(client=fake)
    event = {
        "Records": [
            _record(
                "1",
                {"entity_type": "table", "table": "orders", "status": "published"},
                sk="v2",
            )
        ]
    }
    resp = lambda_handler.handler(event, graph_client=client)
    assert resp == {"batchItemFailures": []}
    assert fake.calls == []


def test_failed_record_reported_in_batch_item_failures():
    # 'customers' upsert 에서만 실패하도록 구성.
    fake = FakeNeptuneClient(fail_on="customers")
    client = NeptuneGraphClient(client=fake)
    event = {
        "Records": [
            _record("1", {"entity_type": "table", "table": "orders", "status": "published"}),
            _record("2", {"entity_type": "table", "table": "customers", "status": "published"}),
        ]
    }
    resp = lambda_handler.handler(event, graph_client=client)
    assert resp["batchItemFailures"] == [{"itemIdentifier": "2"}]


def test_empty_event_returns_empty_failures():
    fake = FakeNeptuneClient()
    client = NeptuneGraphClient(client=fake)
    resp = lambda_handler.handler({"Records": []}, graph_client=client)
    assert resp == {"batchItemFailures": []}
