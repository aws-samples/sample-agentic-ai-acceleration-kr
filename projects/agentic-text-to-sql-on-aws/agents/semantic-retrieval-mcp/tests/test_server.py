"""search_schema 도구 유닛 테스트 — retriever mock."""

from __future__ import annotations

from semantic_retrieval_mcp import server
from semantic_retrieval_mcp.retriever import RetrievalHit


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
