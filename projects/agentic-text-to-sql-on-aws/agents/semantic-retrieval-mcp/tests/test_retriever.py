"""쿼리 빌더·결과 정규화·retriever 유닛 테스트 — OpenSearch/bedrock mock."""

from __future__ import annotations

from unittest.mock import MagicMock

from semantic_retrieval_mcp.retriever import (
    CompositeRetriever,
    GraphTraverser,
    OpenSearchConfig,
    OpenSearchHybridRetriever,
    RetrievalHit,
    SemanticTermRetriever,
    _parse_join_path_response,
    build_hybrid_query,
    build_join_path_query,
    build_semantic_hybrid_query,
    normalize_hits,
    normalize_semantic_hits,
)

# ── 쿼리 빌더 ────────────────────────────────────────────────────────────


def test_build_hybrid_query_has_knn_and_lexical() -> None:
    body = build_hybrid_query([0.1, 0.2, 0.3], "최근 사용자", top_k=5)
    queries = body["query"]["hybrid"]["queries"]

    assert body["size"] == 5
    # 두 서브쿼리: lexical(multi_match) + knn.
    kinds = {list(q.keys())[0] for q in queries}
    assert kinds == {"multi_match", "knn"}

    knn = next(q["knn"] for q in queries if "knn" in q)
    assert knn["embedding"]["vector"] == [0.1, 0.2, 0.3]
    assert knn["embedding"]["k"] == 5

    lexical = next(q["multi_match"] for q in queries if "multi_match" in q)
    assert lexical["query"] == "최근 사용자"

    # 임베딩 필드는 응답에서 제외.
    assert "embedding" in body["_source"]["excludes"]


# ── 결과 정규화 ──────────────────────────────────────────────────────────


def test_normalize_hits_maps_fields() -> None:
    response = {
        "hits": {
            "hits": [
                {
                    "_score": 0.92,
                    "_source": {
                        "doc_type": "column",
                        "table": "customers",
                        "column": "last_login_at",
                        "description": "마지막 로그인 시각",
                        "ddl_snippet": "last_login_at TIMESTAMP",
                    },
                },
                {
                    "_score": 0.5,
                    "_source": {
                        "doc_type": "table",
                        "table": "orders",
                        "description": "주문 테이블",
                    },
                },
            ]
        }
    }
    hits = normalize_hits(response)

    assert len(hits) == 2
    assert hits[0].doc_type == "column"
    assert hits[0].table == "customers"
    assert hits[0].column == "last_login_at"
    assert hits[0].score == 0.92
    # 누락 필드는 None.
    assert hits[1].column is None
    assert hits[1].ddl_snippet is None


def test_normalize_hits_empty() -> None:
    assert normalize_hits({"hits": {"hits": []}}) == []
    assert normalize_hits({}) == []


def test_retrieval_hit_to_dict_shape() -> None:
    hits = normalize_hits(
        {"hits": {"hits": [{"_score": 1.0, "_source": {"doc_type": "table", "table": "t"}}]}}
    )
    d = hits[0].to_dict()
    # M2: additive 필드 포함(기존 필드는 유지 — 하위호환).
    assert {"doc_type", "table", "column", "description", "ddl_snippet", "score"} <= set(d.keys())
    assert set(d.keys()) == {
        "doc_type",
        "table",
        "column",
        "description",
        "ddl_snippet",
        "score",
        "term",
        "synonyms",
        "sql_fragment",
        "join_paths",
    }
    # 기존 doc_type=table 히트의 additive 필드는 기본 None.
    assert d["term"] is None
    assert d["synonyms"] is None
    assert d["sql_fragment"] is None
    assert d["join_paths"] is None


# ── retriever end-to-end (embedding + opensearch mock) ──────────────────


def test_retriever_search_embeds_and_queries() -> None:
    embedding_client = MagicMock()
    embedding_client.embed.return_value = [0.0] * 1024

    os_client = MagicMock()
    os_client.search.return_value = {
        "hits": {
            "hits": [
                {
                    "_score": 0.8,
                    "_source": {"doc_type": "table", "table": "customers"},
                }
            ]
        }
    }

    config = OpenSearchConfig(
        endpoint="https://example.us-west-2.aoss.amazonaws.com",
        index="t2sql-schema-docs",
        region="us-west-2",
        search_pipeline="t2sql-hybrid-pipeline",
    )
    retriever = OpenSearchHybridRetriever(
        config=config, embedding_client=embedding_client, client=os_client
    )

    hits = retriever.search("VIP 고객", top_k=3)

    embedding_client.embed.assert_called_once_with("VIP 고객")
    call = os_client.search.call_args
    assert call.kwargs["index"] == "t2sql-schema-docs"
    # search pipeline 경유 확인.
    assert call.kwargs["params"]["search_pipeline"] == "t2sql-hybrid-pipeline"
    assert call.kwargs["body"]["size"] == 3
    assert len(hits) == 1
    assert hits[0].table == "customers"


# ── M2: 용어/few-shot hybrid 쿼리 빌더 ───────────────────────────────────


def test_build_semantic_hybrid_query_fields_and_filter() -> None:
    body = build_semantic_hybrid_query([0.1, 0.2], "최근 사용자", top_k=4)
    assert body["size"] == 4
    # hybrid 는 compound 쿼리에 중첩 불가 → 최상위 query.hybrid + post_filter 구조.
    hybrid = body["query"]["hybrid"]
    kinds = {list(q.keys())[0] for q in hybrid["queries"]}
    assert kinds == {"multi_match", "knn"}

    lexical = next(q["multi_match"] for q in hybrid["queries"] if "multi_match" in q)
    # 용어 인덱스 전용 필드 목록.
    joined = " ".join(lexical["fields"])
    assert "term" in joined and "synonyms" in joined and "definition" in joined
    assert "question" in joined and "sql" in joined

    knn = next(q["knn"] for q in hybrid["queries"] if "knn" in q)
    assert knn["embedding"]["vector"] == [0.1, 0.2]

    # published 필터: status=published OR status 없음(방어) — post_filter 로 적용.
    filter_should = body["post_filter"]["bool"]["should"]
    assert {"term": {"status": "published"}} in filter_should
    assert "embedding" in body["_source"]["excludes"]


def test_build_semantic_hybrid_query_no_published_filter() -> None:
    body = build_semantic_hybrid_query([0.0], "q", top_k=2, published_only=False)
    # published_only=False 면 post_filter 없이 hybrid 직접.
    assert "hybrid" in body["query"]
    assert "post_filter" not in body


# ── M2: 용어/few-shot 결과 정규화 ────────────────────────────────────────


def test_normalize_semantic_hits_term() -> None:
    response = {
        "hits": {
            "hits": [
                {
                    "_score": 0.9,
                    "_source": {
                        "entity_type": "term",
                        "term": "최근 사용자",
                        "definition": "최근 3개월 활동 사용자",
                        "synonyms": ["액티브 유저", "요즘 유저"],
                        "sql_fragment": "last_login_at >= CURRENT_DATE - INTERVAL '3 months'",
                        "status": "published",
                    },
                }
            ]
        }
    }
    hits = normalize_semantic_hits(response)
    assert len(hits) == 1
    hit = hits[0]
    assert hit.doc_type == "term"
    assert hit.term == "최근 사용자"
    assert hit.description == "최근 3개월 활동 사용자"
    assert hit.synonyms == ["액티브 유저", "요즘 유저"]
    assert hit.sql_fragment.startswith("last_login_at")
    assert hit.score == 0.9


def test_normalize_semantic_hits_fewshot() -> None:
    response = {
        "hits": {
            "hits": [
                {
                    "_score": 0.7,
                    "_source": {
                        "entity_type": "fewshot",
                        "question": "지난달 매출은?",
                        "sql": "SELECT SUM(amount) FROM orders WHERE ...",
                    },
                }
            ]
        }
    }
    hits = normalize_semantic_hits(response)
    assert len(hits) == 1
    hit = hits[0]
    assert hit.doc_type == "fewshot"
    # fewshot 은 기존 필드 재활용: description=question, ddl_snippet=sql.
    assert hit.description == "지난달 매출은?"
    assert hit.ddl_snippet.startswith("SELECT")
    assert hit.term is None


def test_normalize_semantic_hits_synonyms_defensive() -> None:
    # synonyms 가 단일 문자열로 와도 리스트로 정규화(방어적 파싱).
    response = {
        "hits": {
            "hits": [
                {
                    "_score": 0.5,
                    "_source": {
                        "entity_type": "term",
                        "term": "VIP",
                        "definition": "우수 고객",
                        "synonyms": "우수고객",
                    },
                }
            ]
        }
    }
    hits = normalize_semantic_hits(response)
    assert hits[0].synonyms == ["우수고객"]


def test_normalize_semantic_hits_infers_fewshot_without_entity_type() -> None:
    # entity_type 누락 시 question/sql 존재로 fewshot 추정.
    response = {
        "hits": {"hits": [{"_score": 0.3, "_source": {"question": "q?", "sql": "SELECT 1"}}]}
    }
    hits = normalize_semantic_hits(response)
    assert hits[0].doc_type == "fewshot"


def test_semantic_term_retriever_search() -> None:
    embedding_client = MagicMock()
    embedding_client.embed.return_value = [0.0] * 1024
    os_client = MagicMock()
    os_client.search.return_value = {
        "hits": {
            "hits": [
                {
                    "_score": 0.8,
                    "_source": {"entity_type": "term", "term": "최근 사용자", "definition": "..."},
                }
            ]
        }
    }
    config = OpenSearchConfig(
        endpoint="https://example.us-west-2.es.amazonaws.com",
        index="t2sql-semantic",
        region="us-west-2",
    )
    retriever = SemanticTermRetriever(
        config=config, embedding_client=embedding_client, client=os_client
    )
    hits = retriever.search("요즘 유저", top_k=3)

    embedding_client.embed.assert_called_once_with("요즘 유저")
    call = os_client.search.call_args
    assert call.kwargs["index"] == "t2sql-semantic"
    assert call.kwargs["body"]["size"] == 3
    assert len(hits) == 1
    assert hits[0].doc_type == "term"
    assert hits[0].term == "최근 사용자"


# ── M2: Neptune join-path 순회 (GraphTraverser) ─────────────────────────


def test_build_join_path_query_shape() -> None:
    q = build_join_path_query()
    assert "JOINS*1..2" in q
    assert "$tables" in q
    assert "rel.on" in q


def test_parse_join_path_response_dedup_sorted() -> None:
    response = {
        "results": [
            {"join_on": "orders.customer_id = customers.id"},
            {"join_on": "orders.customer_id = customers.id"},  # 중복
            {"join_on": "orders.region_id = regions.id"},
            {"join_on": None},  # 무시
        ]
    }
    joins = _parse_join_path_response(response)
    assert joins == [
        "orders.customer_id = customers.id",
        "orders.region_id = regions.id",
    ]


def test_graph_traverser_find_join_paths() -> None:
    neptune = MagicMock()
    neptune.execute_open_cypher_query.return_value = {
        "results": [{"join_on": "orders.customer_id = customers.id"}]
    }
    traverser = GraphTraverser(endpoint="https://g:8182", client=neptune)
    joins = traverser.find_join_paths(["orders", "customers", "orders"])

    assert joins == ["orders.customer_id = customers.id"]
    call = neptune.execute_open_cypher_query.call_args
    # 중복 제거·정렬된 테이블이 파라미터로 전달.
    import json as _json

    params = _json.loads(call.kwargs["parameters"])
    assert params["tables"] == ["customers", "orders"]


def test_graph_traverser_single_table_skips_query() -> None:
    neptune = MagicMock()
    traverser = GraphTraverser(client=neptune)
    assert traverser.find_join_paths(["orders"]) == []
    neptune.execute_open_cypher_query.assert_not_called()


def test_graph_traverser_graceful_degrade_on_error() -> None:
    neptune = MagicMock()
    neptune.execute_open_cypher_query.side_effect = RuntimeError("neptune unavailable")
    traverser = GraphTraverser(client=neptune)
    # 예외 시 빈 리스트 (graceful degrade).
    assert traverser.find_join_paths(["orders", "customers"]) == []


# ── M2: CompositeRetriever 병합·join_paths 부착 ─────────────────────────


class _FakeRetriever:
    def __init__(self, hits: list[RetrievalHit]) -> None:
        self._hits = hits
        self.last = None

    def search(self, query: str, top_k: int = 5) -> list[RetrievalHit]:
        self.last = (query, top_k)
        return list(self._hits)


class _FakeGraph:
    def __init__(self, joins: list[str]) -> None:
        self._joins = joins
        self.last_tables = None

    def find_join_paths(self, tables: list[str]) -> list[str]:
        self.last_tables = tables
        return list(self._joins)


def _table_hit(table: str, score: float) -> RetrievalHit:
    return RetrievalHit(
        doc_type="table",
        table=table,
        column=None,
        description=f"{table} 테이블",
        ddl_snippet=None,
        score=score,
    )


def _term_hit(term: str, score: float) -> RetrievalHit:
    return RetrievalHit(
        doc_type="term",
        table=None,
        column=None,
        description="정의",
        ddl_snippet=None,
        score=score,
        term=term,
    )


def test_composite_merges_and_attaches_join_paths() -> None:
    schema = _FakeRetriever([_table_hit("orders", 0.9), _table_hit("customers", 0.6)])
    term = _FakeRetriever([_term_hit("최근 사용자", 0.7)])
    graph = _FakeGraph(["orders.customer_id = customers.id"])

    composite = CompositeRetriever(schema, term, graph)
    hits = composite.search("최근 사용자 주문", top_k=5)

    # 두 하위 retriever 에 top_k 전달.
    assert schema.last == ("최근 사용자 주문", 5)
    assert term.last == ("최근 사용자 주문", 5)
    # 그래프에 두 결과의 table 집합(중복 제거·정렬) 전달.
    assert graph.last_tables == ["customers", "orders"]

    # score 내림차순 병합.
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)
    # table 히트에 join_paths 부착, term 히트에는 미부착.
    table_hits = [h for h in hits if h.doc_type == "table"]
    assert all(h.join_paths == ["orders.customer_id = customers.id"] for h in table_hits)
    term_hits = [h for h in hits if h.doc_type == "term"]
    assert term_hits and all(h.join_paths is None for h in term_hits)


def test_composite_adds_join_path_hit_when_no_table_hits() -> None:
    # table 히트가 없으면 join_paths 만 담은 별도 hit 추가.
    schema = _FakeRetriever([])
    term = _FakeRetriever([_term_hit("t", 0.5)])
    # term 에도 table 이 없으므로 graph 에는 빈 집합이 가지만, 여기선 graph 가 join 반환하도록 강제.
    graph = _FakeGraph(["a.x = b.y"])

    composite = CompositeRetriever(schema, term, graph)
    hits = composite.search("q", top_k=5)

    join_hits = [h for h in hits if h.join_paths]
    assert len(join_hits) == 1
    assert join_hits[0].doc_type == "table"
    assert join_hits[0].table is None
    assert join_hits[0].join_paths == ["a.x = b.y"]


def test_composite_without_graph_skips_join_paths() -> None:
    schema = _FakeRetriever([_table_hit("orders", 0.9)])
    term = _FakeRetriever([_term_hit("t", 0.5)])
    composite = CompositeRetriever(schema, term, graph_traverser=None)

    hits = composite.search("q", top_k=5)
    assert all(h.join_paths is None for h in hits)


def test_composite_caps_results_at_double_top_k() -> None:
    schema = _FakeRetriever([_table_hit(f"t{i}", 1.0 - i * 0.01) for i in range(10)])
    term = _FakeRetriever([_term_hit(f"term{i}", 0.5 - i * 0.01) for i in range(10)])
    composite = CompositeRetriever(schema, term, graph_traverser=None)

    hits = composite.search("q", top_k=3)
    # 최대 top_k*2 = 6.
    assert len(hits) == 6
    # 상위 점수부터.
    assert hits[0].score == 1.0


def test_composite_does_not_mutate_source_hits() -> None:
    # 부작용 방지: 원본 히트에 join_paths 를 덮어쓰지 않는다(replace 로 복제).
    src = _table_hit("orders", 0.9)
    schema = _FakeRetriever([src])
    term = _FakeRetriever([])
    graph = _FakeGraph(["orders.id = x.oid"])
    composite = CompositeRetriever(schema, term, graph)
    composite.search("q", top_k=5)
    assert src.join_paths is None
