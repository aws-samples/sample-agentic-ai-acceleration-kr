"""search_schema 도구 + retriever 조립 유닛 테스트 — retriever mock."""

from __future__ import annotations

from semantic_retrieval_mcp import server
from semantic_retrieval_mcp.retriever import (
    CompositeRetriever,
    OpenSearchHybridRetriever,
    RetrievalHit,
)


def _hit() -> RetrievalHit:
    return RetrievalHit(
        doc_type="table",
        table="customers",
        column=None,
        description="고객",
        ddl_snippet="CREATE TABLE customers (...)",
        score=0.7,
    )


def test_search_schema_returns_results(monkeypatch) -> None:
    class FakeRetriever:
        def __init__(self) -> None:
            self.last = None

        def search(self, query: str, top_k: int = 5):
            self.last = (query, top_k)
            return [_hit()]

    fake = FakeRetriever()
    monkeypatch.setattr(server, "_get_retriever", lambda: fake)

    result = server.search_schema("고객 목록", top_k=3)
    assert "results" in result
    assert result["results"][0]["table"] == "customers"
    assert fake.last == ("고객 목록", 3)


def test_search_schema_clamps_top_k(monkeypatch) -> None:
    captured = {}

    class FakeRetriever:
        def search(self, query: str, top_k: int = 5):
            captured["top_k"] = top_k
            return []

    monkeypatch.setattr(server, "_get_retriever", lambda: FakeRetriever())

    server.search_schema("q", top_k=999)
    assert captured["top_k"] == 50  # 상한 50으로 캡


def test_search_schema_handles_error(monkeypatch) -> None:
    class FailingRetriever:
        def search(self, query: str, top_k: int = 5):
            raise RuntimeError("opensearch unavailable")

    monkeypatch.setattr(server, "_get_retriever", lambda: FailingRetriever())

    result = server.search_schema("q")
    assert result["results"] == []
    assert "opensearch unavailable" in result["error"]


def test_search_schema_exposes_additive_fields(monkeypatch) -> None:
    # term 히트의 additive 필드가 응답에 그대로 실린다(계약 확인).
    term_hit = RetrievalHit(
        doc_type="term",
        table=None,
        column=None,
        description="최근 3개월 활동 사용자",
        ddl_snippet=None,
        score=0.9,
        term="최근 사용자",
        synonyms=["액티브 유저"],
        sql_fragment="last_login_at >= ...",
        join_paths=None,
    )

    class FakeRetriever:
        def search(self, query: str, top_k: int = 5):
            return [term_hit]

    monkeypatch.setattr(server, "_get_retriever", lambda: FakeRetriever())
    result = server.search_schema("요즘 유저")
    item = result["results"][0]
    assert item["doc_type"] == "term"
    assert item["term"] == "최근 사용자"
    assert item["synonyms"] == ["액티브 유저"]
    assert item["sql_fragment"] == "last_login_at >= ..."
    assert item["join_paths"] is None


# ── retriever 조립 분기 (build_retriever) ────────────────────────────────


def _set_base_env(monkeypatch) -> None:
    monkeypatch.setenv("OPENSEARCH_ENDPOINT", "https://example.us-west-2.es.amazonaws.com")
    monkeypatch.setenv("AWS_REGION", "us-west-2")


def test_build_retriever_schema_only_when_no_semantic_env(monkeypatch) -> None:
    # SEMANTIC_INDEX·graph 없음 → M1 동일: OpenSearchHybridRetriever 단독.
    _set_base_env(monkeypatch)
    monkeypatch.delenv("SEMANTIC_INDEX", raising=False)
    monkeypatch.delenv("SEMANTIC_GRAPH_ENABLED", raising=False)
    monkeypatch.delenv("GRAPH_ENDPOINT", raising=False)

    retriever = server.build_retriever()
    assert isinstance(retriever, OpenSearchHybridRetriever)


def test_build_retriever_composite_when_semantic_index_set(monkeypatch) -> None:
    _set_base_env(monkeypatch)
    monkeypatch.setenv("SEMANTIC_INDEX", "t2sql-semantic")
    monkeypatch.delenv("SEMANTIC_GRAPH_ENABLED", raising=False)
    monkeypatch.delenv("GRAPH_ENDPOINT", raising=False)

    retriever = server.build_retriever()
    assert isinstance(retriever, CompositeRetriever)
    # graph 비활성.
    assert retriever.graph_traverser is None


def test_build_retriever_composite_with_graph(monkeypatch) -> None:
    _set_base_env(monkeypatch)
    monkeypatch.setenv("SEMANTIC_INDEX", "t2sql-semantic")
    monkeypatch.setenv("SEMANTIC_GRAPH_ENABLED", "true")
    monkeypatch.setenv("GRAPH_ENDPOINT", "https://g.us-west-2.neptune.amazonaws.com:8182")

    retriever = server.build_retriever()
    assert isinstance(retriever, CompositeRetriever)
    assert retriever.graph_traverser is not None


def test_build_retriever_graph_enabled_alone_forces_composite(monkeypatch) -> None:
    # SEMANTIC_INDEX 없어도 graph 켜지면 composite.
    _set_base_env(monkeypatch)
    monkeypatch.delenv("SEMANTIC_INDEX", raising=False)
    monkeypatch.setenv("SEMANTIC_GRAPH_ENABLED", "true")
    monkeypatch.setenv("GRAPH_ENDPOINT", "https://g:8182")

    retriever = server.build_retriever()
    assert isinstance(retriever, CompositeRetriever)
    assert retriever.graph_traverser is not None


def test_build_retriever_graph_flag_without_endpoint_no_graph(monkeypatch) -> None:
    # 플래그만 있고 GRAPH_ENDPOINT 없으면 graph 미조립(SEMANTIC_INDEX 로 composite 은 됨).
    _set_base_env(monkeypatch)
    monkeypatch.setenv("SEMANTIC_INDEX", "t2sql-semantic")
    monkeypatch.setenv("SEMANTIC_GRAPH_ENABLED", "true")
    monkeypatch.delenv("GRAPH_ENDPOINT", raising=False)

    retriever = server.build_retriever()
    assert isinstance(retriever, CompositeRetriever)
    assert retriever.graph_traverser is None


def test_env_flag_parsing(monkeypatch) -> None:
    monkeypatch.setenv("F", "TRUE")
    assert server._env_flag("F") is True
    monkeypatch.setenv("F", "false")
    assert server._env_flag("F") is False
    monkeypatch.delenv("F", raising=False)
    assert server._env_flag("F", default=True) is True
