"""쿼리 빌더·결과 정규화·retriever 유닛 테스트 — OpenSearch/bedrock mock."""

from __future__ import annotations

from unittest.mock import MagicMock

from semantic_retrieval_mcp.retriever import (
    OpenSearchConfig,
    OpenSearchHybridRetriever,
    build_hybrid_query,
    normalize_hits,
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
    assert set(d.keys()) == {
        "doc_type",
        "table",
        "column",
        "description",
        "ddl_snippet",
        "score",
    }


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
