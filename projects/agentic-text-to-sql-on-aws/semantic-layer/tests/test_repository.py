"""SemanticRepository 단위 테스트 (fake DynamoDB, AWS 호출 없음)."""

from __future__ import annotations

import pytest

from semantic_layer.repository import (
    VALID_ENTITY_TYPES,
    VALID_STATUSES,
    SemanticRepository,
    from_attribute_value,
    item_to_dict,
    to_attribute_value,
)

from .fakes import ConditionalCheckFailed, FakeDynamoClient, fake_embedder


def make_repo(**kwargs) -> tuple[SemanticRepository, FakeDynamoClient]:
    client = FakeDynamoClient()
    repo = SemanticRepository(
        "agentic-t2sql-semantic",
        client=client,
        embedder=fake_embedder,
        clock=lambda: "2026-07-26T00:00:00+00:00",
        **kwargs,
    )
    return repo, client


# --- AttributeValue 직렬화 ---------------------------------------------------


def test_attribute_value_roundtrip_scalars():
    for value in ["hi", 3, 1.5, True, None]:
        assert from_attribute_value(to_attribute_value(value)) == value


def test_attribute_value_roundtrip_nested():
    value = {"maps_to": [{"table": "orders", "column": "total_amount"}], "syn": ["a", "b"]}
    assert from_attribute_value(to_attribute_value(value)) == value


def test_bool_not_treated_as_number():
    # bool 은 int 하위타입이지만 BOOL 로 직렬화돼야 함.
    assert to_attribute_value(False) == {"BOOL": False}


# --- 최초 생성 / 조건부 쓰기 --------------------------------------------------


def test_put_creates_v0_version_1():
    repo, client = make_repo()
    result = repo.put_entity("table", "orders", {"table": "orders", "description": "주문"})
    assert result["version"] == 1
    assert result["sk"] == "v0"
    assert result["status"] == "candidate"
    stored = repo.get_entity("table", "orders")
    assert stored["description"] == "주문"


def test_put_first_write_uses_attribute_not_exists():
    repo, client = make_repo()
    repo.put_entity("table", "orders", {"table": "orders"})
    # 동일 키에 attribute_not_exists 조건이 남아있어 직접 재삽입은 막혀야 함.
    with pytest.raises(ConditionalCheckFailed):
        client.put_item(
            TableName="t",
            Item={"pk": {"S": "table#orders"}, "sk": {"S": "v0"}},
            ConditionExpression="attribute_not_exists(pk)",
        )


# --- 버전 증가 + 이력 복사 ----------------------------------------------------


def test_put_increments_version_and_copies_history():
    repo, client = make_repo()
    repo.put_entity("table", "orders", {"table": "orders", "description": "v1"})
    repo.put_entity("table", "orders", {"table": "orders", "description": "v2"})

    latest = repo.get_entity("table", "orders")
    assert latest["version"] == 2
    assert latest["description"] == "v2"

    # 직전 본이 v1 이력으로 보존됐는지.
    history = item_to_dict(client.store[("table#orders", "v1")])
    assert history["version"] == 1
    assert history["description"] == "v1"


def test_put_three_writes_keep_full_history():
    repo, client = make_repo()
    for i in range(1, 4):
        repo.put_entity("table", "orders", {"table": "orders", "description": f"v{i}"})
    assert repo.get_entity("table", "orders")["version"] == 3
    assert ("table#orders", "v1") in client.store
    assert ("table#orders", "v2") in client.store
    # v0 는 항상 최신본.
    assert client.store[("table#orders", "v0")]["version"]["N"] == "3"


def test_updated_at_and_by_recorded():
    repo, _ = make_repo()
    result = repo.put_entity(
        "table", "orders", {"table": "orders"}, actor="manager@example.com"
    )
    assert result["updated_by"] == "manager@example.com"
    assert result["updated_at"] == "2026-07-26T00:00:00+00:00"


# --- 임베딩 -----------------------------------------------------------------


def test_term_gets_embedding_computed():
    repo, _ = make_repo()
    result = repo.put_entity(
        "term",
        "revenue",
        {"term": "매출", "definition": "판매액", "synonyms": ["revenue"]},
        status="published",
    )
    assert len(result["embedding"]) == 1024


def test_table_entity_has_no_embedding():
    repo, _ = make_repo()
    result = repo.put_entity("table", "orders", {"table": "orders"})
    assert "embedding" not in result


def test_embedding_preserved_on_status_change_not_recomputed():
    calls: list[str] = []

    def counting_embedder(text: str) -> list[float]:
        calls.append(text)
        return [0.1] * 1024

    client = FakeDynamoClient()
    repo = SemanticRepository(
        "t", client=client, embedder=counting_embedder, clock=lambda: "t"
    )
    repo.put_entity("term", "revenue", {"term": "매출", "definition": "d"}, status="candidate")
    assert len(calls) == 1
    repo.publish("term", "revenue")
    # publish 는 기존 embedding 을 보존해 재계산하지 않아야 함.
    assert len(calls) == 1


def test_term_without_embedder_raises():
    client = FakeDynamoClient()
    repo = SemanticRepository("t", client=client, embedder=None, clock=lambda: "t")
    with pytest.raises(ValueError):
        repo.put_entity("term", "revenue", {"term": "매출", "definition": "d"})


# --- publish / unpublish -----------------------------------------------------


def test_publish_transitions_status_and_bumps_version():
    repo, _ = make_repo()
    repo.put_entity("term", "vip", {"term": "VIP", "definition": "d"}, status="candidate")
    published = repo.publish("term", "vip")
    assert published["status"] == "published"
    assert published["version"] == 2


def test_unpublish_transitions_back_to_candidate():
    repo, _ = make_repo()
    repo.put_entity("term", "vip", {"term": "VIP", "definition": "d"}, status="published")
    result = repo.unpublish("term", "vip")
    assert result["status"] == "candidate"
    assert result["version"] == 2


def test_publish_missing_entity_raises():
    repo, _ = make_repo()
    with pytest.raises(KeyError):
        repo.publish("term", "does-not-exist")


# --- list / 필터 -------------------------------------------------------------


def test_list_entities_filters_by_type_and_status():
    repo, _ = make_repo()
    repo.put_entity("table", "orders", {"table": "orders"}, status="published")
    repo.put_entity("term", "vip", {"term": "VIP", "definition": "d"}, status="published")
    repo.put_entity("term", "churn", {"term": "이탈", "definition": "d"}, status="candidate")

    terms = repo.list_entities(entity_type="term")
    assert {t["entity_id"] for t in terms} == {"vip", "churn"}

    published_terms = repo.list_entities(entity_type="term", status="published")
    assert {t["entity_id"] for t in published_terms} == {"vip"}


def test_list_entities_excludes_history_versions():
    repo, _ = make_repo()
    repo.put_entity("table", "orders", {"table": "orders", "description": "v1"})
    repo.put_entity("table", "orders", {"table": "orders", "description": "v2"})
    tables = repo.list_entities(entity_type="table")
    # v0(최신본) 하나만 반환, v1 이력은 제외.
    assert len(tables) == 1
    assert tables[0]["version"] == 2


# --- 검증 -------------------------------------------------------------------


def test_invalid_entity_type_rejected():
    repo, _ = make_repo()
    with pytest.raises(ValueError):
        repo.put_entity("bogus", "x", {})


def test_invalid_status_rejected():
    repo, _ = make_repo()
    with pytest.raises(ValueError):
        repo.put_entity("table", "orders", {"table": "orders"}, status="draft")


def test_get_missing_returns_none():
    repo, _ = make_repo()
    assert repo.get_entity("table", "nope") is None


# --- datasource entity_type --------------------------------------------------


def test_datasource_entity_type_is_valid():
    """admin panel 이 등록하는 데이터소스 연결 메타(자격증명 제외)를 저장할 수 있다."""
    repo, _ = make_repo()
    entity = repo.put_entity(
        "datasource",
        "warehouse",
        {"engine": "redshift-serverless", "host": "wh.example.com"},
        actor="admin-panel",
    )
    assert entity["entity_type"] == "datasource"
    assert entity["status"] == "candidate"
    assert entity["pk"] == "datasource#warehouse"
    assert repo.get_entity("datasource", "warehouse")["engine"] == "redshift-serverless"


def test_datasource_entity_needs_no_embedding():
    """datasource 는 EMBED_ENTITIES 가 아니므로 embedder 없이도 쓸 수 있다."""
    client = FakeDynamoClient()
    repo = SemanticRepository("agentic-t2sql-semantic", client=client, embedder=None)
    entity = repo.put_entity("datasource", "wh", {"engine": "aurora-postgresql"})
    assert "embedding" not in entity


def test_datasource_entity_publish_roundtrip():
    repo, _ = make_repo()
    repo.put_entity("datasource", "warehouse", {"engine": "aurora-postgresql"})
    assert repo.publish("datasource", "warehouse")["status"] == "published"
    assert repo.unpublish("datasource", "warehouse")["status"] == "candidate"


def test_datasource_addition_is_additive_only():
    """기존 5종 타입은 그대로 유효해야 한다(additive only 계약)."""
    assert VALID_ENTITY_TYPES == {"term", "fewshot", "table", "column", "join", "datasource"}


def test_graph_sync_ignores_datasource_entities():
    """graph_sync 는 미지원 타입에 빈 statement 를 반환 → 그래프 동기화에 영향 없음."""
    from semantic_layer.graph_sync import _delete_statements, _upsert_statements

    data = {"entity_type": "datasource", "entity_id": "warehouse", "engine": "redshift-serverless"}
    assert _upsert_statements("datasource", data) == []
    assert _delete_statements("datasource", data) == []


# --- rejected 상태 -------------------------------------------------------------


def test_rejected_status_is_valid_and_additive_only():
    """기존 2종 상태는 그대로 유효해야 한다(additive only 계약)."""
    assert VALID_STATUSES == {"candidate", "published", "rejected"}


def test_reject_transitions_status_and_bumps_version():
    repo, _ = make_repo()
    repo.put_entity("term", "vip", {"term": "VIP", "definition": "d"}, status="candidate")
    rejected = repo.reject("term", "vip", reason="정의가 모호함", actor="manager@example.com")
    assert rejected["status"] == "rejected"
    assert rejected["version"] == 2
    assert rejected["updated_by"] == "manager@example.com"
    assert rejected["rejection_reason"] == "정의가 모호함"
    assert repo.get_entity("term", "vip")["status"] == "rejected"


def test_reject_preserves_payload_and_embedding():
    calls: list[str] = []

    def counting_embedder(text: str) -> list[float]:
        calls.append(text)
        return [0.1] * 1024

    client = FakeDynamoClient()
    repo = SemanticRepository("t", client=client, embedder=counting_embedder, clock=lambda: "t")
    repo.put_entity("term", "vip", {"term": "VIP", "definition": "d"}, status="candidate")
    assert len(calls) == 1
    rejected = repo.reject("term", "vip", reason="중복")
    # status 전환은 내용 불변 → 임베딩 재계산 없음, payload 보존.
    assert len(calls) == 1
    assert rejected["term"] == "VIP"
    assert rejected["definition"] == "d"


def test_reject_without_reason_omits_rejection_reason_key():
    repo, _ = make_repo()
    repo.put_entity("table", "orders", {"table": "orders"})
    rejected = repo.reject("table", "orders")
    assert rejected["status"] == "rejected"
    assert "rejection_reason" not in rejected
    assert rejected["updated_by"] == "system"


def test_reject_missing_entity_raises():
    repo, _ = make_repo()
    with pytest.raises(KeyError):
        repo.reject("term", "does-not-exist")


def test_rejected_can_be_published_again():
    """반려 후 재승인 경로 — publish 는 status 무관하게 전환한다."""
    repo, _ = make_repo()
    repo.put_entity("term", "vip", {"term": "VIP", "definition": "d"})
    repo.reject("term", "vip", reason="보류")
    published = repo.publish("term", "vip", actor="manager@example.com")
    assert published["status"] == "published"
    assert published["version"] == 3
    # 반려 사유는 이력으로 payload 에 남는다(감사 추적).
    assert published["rejection_reason"] == "보류"


def test_rejected_can_be_unpublished_back_to_candidate():
    """반려 → 재검토 큐(candidate) 복귀 경로."""
    repo, _ = make_repo()
    repo.put_entity("term", "vip", {"term": "VIP", "definition": "d"})
    repo.reject("term", "vip", reason="보류")
    back = repo.unpublish("term", "vip")
    assert back["status"] == "candidate"
    assert back["version"] == 3


def test_list_entities_filters_rejected():
    repo, _ = make_repo()
    repo.put_entity("term", "keep", {"term": "keep", "definition": "d"})
    repo.put_entity("term", "drop", {"term": "drop", "definition": "d"})
    repo.reject("term", "drop", reason="부정확")

    assert [e["entity_id"] for e in repo.list_entities(status="rejected")] == ["drop"]
    # 반려된 항목은 승인 대기 큐에서 사라진다.
    assert [e["entity_id"] for e in repo.list_entities(status="candidate")] == ["keep"]


def test_put_entity_accepts_rejected_status_directly():
    repo, _ = make_repo()
    entity = repo.put_entity("table", "orders", {"table": "orders"}, status="rejected")
    assert entity["status"] == "rejected"


def test_graph_sync_deletes_rejected_entities():
    """rejected 는 published 가 아니므로 그래프에서 제거된다(코드 변경 불요 확인)."""
    from semantic_layer.graph_sync import record_to_cypher

    record = {
        "eventName": "MODIFY",
        "dynamodb": {
            "Keys": {"pk": {"S": "table#orders"}, "sk": {"S": "v0"}},
            "NewImage": {
                "pk": {"S": "table#orders"},
                "sk": {"S": "v0"},
                "entity_type": {"S": "table"},
                "entity_id": {"S": "orders"},
                "status": {"S": "rejected"},
                "table": {"S": "orders"},
            },
        },
    }
    statements = record_to_cypher(record)
    assert statements, "rejected 레코드는 삭제 statement 를 만들어야 한다"
    assert all("DELETE" in stmt.query.upper() for stmt in statements), [
        stmt.query for stmt in statements
    ]
