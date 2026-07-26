"""semantic 큐레이션 도구 유닛 테스트 (list/get/put/publish/unpublish). AWS 호출 없음."""

from __future__ import annotations

import pytest

from datasource_admin_mcp import repository_factory, server

from .fakes import FakeRepository


@pytest.fixture()
def repo() -> FakeRepository:
    fake = FakeRepository()
    repository_factory.reset(repository=fake)
    yield fake
    repository_factory.reset()


# --- put / get ---------------------------------------------------------------


def test_put_entity_defaults_to_candidate(repo: FakeRepository) -> None:
    result = server.put_entity("term", "revenue", {"term": "매출", "definition": "주문 총액 합계"})
    assert result["status"] == "ok"
    assert result["entity"]["status"] == "candidate"
    assert result["entity"]["entity_type"] == "term"
    assert result["entity"]["version"] == 1
    assert repo.puts == [{"entity_type": "term", "entity_id": "revenue", "status": "candidate"}]


def test_put_entity_passes_actor_for_audit(repo: FakeRepository) -> None:
    result = server.put_entity("term", "vip", {"term": "VIP"}, "candidate", "manager@example.com")
    assert result["entity"]["updated_by"] == "manager@example.com"


def test_get_entity_returns_none_when_absent(repo: FakeRepository) -> None:
    result = server.get_entity("term", "nope")
    assert result == {"status": "ok", "entity": None}


def test_embedding_stripped_from_responses(repo: FakeRepository) -> None:
    # payload 경량화 계약(§8.3): 응답에서 embedding 제거.
    server.put_entity("term", "revenue", {"term": "매출", "embedding": [0.1] * 1024})
    assert "embedding" in repo.store[("term", "revenue")]  # 저장은 유지

    assert "embedding" not in server.get_entity("term", "revenue")["entity"]
    assert "embedding" not in server.list_entities()["entities"][0]
    assert "embedding" not in server.put_entity("term", "revenue", {"term": "매출2"})["entity"]
    assert "embedding" not in server.publish_entity("term", "revenue")["entity"]


# --- list --------------------------------------------------------------------


def test_list_entities_filters_by_type_and_status(repo: FakeRepository) -> None:
    server.put_entity("term", "a", {"term": "a"})
    server.put_entity("term", "b", {"term": "b"}, "published")
    server.put_entity("table", "orders", {"table": "orders"}, "published")

    all_entities = server.list_entities()
    assert len(all_entities["entities"]) == 3

    candidates = server.list_entities(status="candidate")
    assert [e["entity_id"] for e in candidates["entities"]] == ["a"]

    terms = server.list_entities(entity_type="term")
    assert {e["entity_id"] for e in terms["entities"]} == {"a", "b"}


def test_list_entities_error_is_normalized() -> None:
    class Boom:
        def list_entities(self, entity_type=None, status=None):
            raise RuntimeError("dynamodb unavailable")

    repository_factory.reset(repository=Boom())
    try:
        result = server.list_entities()
    finally:
        repository_factory.reset()
    assert result["status"] == "error"
    assert "RuntimeError: dynamodb unavailable" == result["message"]


# --- publish / unpublish ------------------------------------------------------


def test_publish_then_unpublish_roundtrip(repo: FakeRepository) -> None:
    server.put_entity("term", "revenue", {"term": "매출"})

    published = server.publish_entity("term", "revenue", "manager@example.com")
    assert published["entity"]["status"] == "published"
    assert published["entity"]["version"] == 2
    assert published["entity"]["term"] == "매출"  # payload 보존

    unpublished = server.unpublish_entity("term", "revenue")
    assert unpublished["entity"]["status"] == "candidate"
    assert unpublished["entity"]["version"] == 3


def test_publish_missing_entity_returns_error(repo: FakeRepository) -> None:
    result = server.publish_entity("term", "ghost")
    assert result["status"] == "error"
    assert "KeyError" in result["message"]


def test_put_entity_rejects_unknown_type() -> None:
    # 실 SemanticRepository 의 검증을 통과하지 않는 타입은 error 로 정규화된다.
    from semantic_layer.repository import SemanticRepository

    from .fakes import fake_embedder

    class _Client:
        def get_item(self, **kwargs):
            return {}

        def put_item(self, **kwargs):
            return {}

    repository_factory.reset(
        repository=SemanticRepository("t", client=_Client(), embedder=fake_embedder)
    )
    try:
        result = server.put_entity("unknown_type", "x", {})
    finally:
        repository_factory.reset()
    assert result["status"] == "error"
    assert "ValueError" in result["message"]


def test_datasource_entity_type_accepted_by_real_repository() -> None:
    # M4 additive 검증: VALID_ENTITY_TYPES 에 datasource 가 포함돼야 put 이 성공한다.
    from semantic_layer.repository import SemanticRepository

    from .fakes import fake_embedder

    stored: list[dict] = []

    class _Client:
        def get_item(self, **kwargs):
            return {}

        def put_item(self, **kwargs):
            stored.append(kwargs["Item"])
            return {}

    repository_factory.reset(
        repository=SemanticRepository("t", client=_Client(), embedder=fake_embedder)
    )
    try:
        result = server.put_entity("datasource", "warehouse", {"engine": "redshift-serverless"})
    finally:
        repository_factory.reset()
    assert result["status"] == "ok"
    assert result["entity"]["entity_type"] == "datasource"
    assert stored[0]["entity_type"] == {"S": "datasource"}
