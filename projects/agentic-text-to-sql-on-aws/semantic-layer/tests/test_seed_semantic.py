"""seed_semantic 단위 테스트 (fake repository, AWS 호출 없음)."""

from __future__ import annotations

from semantic_layer import seed_semantic
from semantic_layer.repository import SemanticRepository

from .fakes import FakeDynamoClient, fake_embedder


def make_repo() -> SemanticRepository:
    return SemanticRepository(
        "t", client=FakeDynamoClient(), embedder=fake_embedder, clock=lambda: "t"
    )


def test_seed_populates_all_entity_types():
    repo = make_repo()
    counts = seed_semantic.seed(table_name="t", repository=repo)
    assert counts["table"] == 5  # categories, customers, products, orders, order_items
    assert counts["term"] >= 5
    assert counts["fewshot"] >= 3
    assert counts["column"] > 0
    assert counts["join"] > 0


def test_seed_is_idempotent_only_bumps_version():
    repo = make_repo()
    seed_semantic.seed(table_name="t", repository=repo)
    seed_semantic.seed(table_name="t", repository=repo)
    orders = repo.get_entity("table", "orders")
    # 두 번 실행 → 버전만 2 로 증가(중복 항목 생성 없음).
    assert orders["version"] == 2


def test_seed_includes_candidate_example_for_split_validation():
    repo = make_repo()
    seed_semantic.seed(table_name="t", repository=repo)
    candidates = repo.list_entities(entity_type="term", status="candidate")
    assert len(candidates) >= 1
    published = repo.list_entities(entity_type="term", status="published")
    assert len(published) >= 4


def test_seed_recent_active_customer_term_matches_contract():
    repo = make_repo()
    seed_semantic.seed(table_name="t", repository=repo)
    term = repo.get_entity("term", "recent_active_customer")
    assert term["term"] == "최근 활성 고객"
    assert "요즘 들어온 유저" in term["synonyms"]
    assert term["sql_fragment"] == "last_login_at >= CURRENT_DATE - INTERVAL '3 months'"
    assert term["maps_to"] == [{"table": "customers", "column": "last_login_at"}]
    assert len(term["embedding"]) == 1024


def test_seed_revenue_term_maps_to_real_column():
    repo = make_repo()
    seed_semantic.seed(table_name="t", repository=repo)
    term = repo.get_entity("term", "revenue")
    assert term["term"] == "매출"
    # schema.py 실제 컬럼(orders.total_amount)으로 매핑돼야 함.
    assert term["maps_to"] == [{"table": "orders", "column": "total_amount"}]


def test_seed_join_entities_derived_from_fk():
    repo = make_repo()
    seed_semantic.seed(table_name="t", repository=repo)
    joins = repo.list_entities(entity_type="join")
    on_clauses = {j["join_on"] for j in joins}
    assert "orders.customer_id = customers.id" in on_clauses


def test_derive_schema_entities_order_table_before_column():
    from sample_data import schema

    entities = seed_semantic.derive_schema_entities(schema.TABLES)
    types = [e["entity_type"] for e in entities]
    last_table = max(i for i, t in enumerate(types) if t == "table")
    first_column = min(i for i, t in enumerate(types) if t == "column")
    # table 이 column 보다 먼저 나와야 함(그래프 MERGE 의존성 안전).
    assert last_table < first_column


def test_parse_reference():
    assert seed_semantic._parse_reference("categories(id)") == ("categories", "id")
