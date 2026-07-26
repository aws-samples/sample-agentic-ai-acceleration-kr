"""SemanticRepository 단위 테스트 (fake DynamoDB, AWS 호출 없음)."""

from __future__ import annotations

import pytest

from semantic_layer.repository import (
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
