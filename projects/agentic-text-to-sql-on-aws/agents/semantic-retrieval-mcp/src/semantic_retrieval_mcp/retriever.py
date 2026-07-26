"""Semantic 검색 — 저장소 중립 인터페이스(ARCHITECTURE.md §8).

추상 ``SemanticRetriever`` base + ``OpenSearchHybridRetriever`` 구현체.
M2에서 Neptune 순회 retriever가 추가될 예정이므로 인터페이스를 저장소 중립으로 유지한다.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from opensearchpy import AWSV4SignerAuth, OpenSearch, RequestsHttpConnection

from semantic_retrieval_mcp.embedding import EmbeddingClient


@dataclass
class RetrievalHit:
    """정규화된 검색 결과 한 건. 도구 계약(search_schema)의 results 원소와 대응."""

    doc_type: str  # "table" | "column"
    table: str | None
    column: str | None
    description: str | None
    ddl_snippet: str | None
    score: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_type": self.doc_type,
            "table": self.table,
            "column": self.column,
            "description": self.description,
            "ddl_snippet": self.ddl_snippet,
            "score": self.score,
        }


class SemanticRetriever(ABC):
    """스키마/용어 컨텍스트 검색 추상 인터페이스(저장소 중립)."""

    @abstractmethod
    def search(self, query: str, top_k: int = 5) -> list[RetrievalHit]:
        raise NotImplementedError


def build_hybrid_query(embedding: list[float], query_text: str, top_k: int) -> dict[str, Any]:
    """hybrid(knn + BM25 match) 쿼리 본문을 구성한다.

    벡터는 클라이언트에서 계산해 주입한다(임베딩 처리를 서버 사이드 ML processor에
    의존하지 않는 이식성 있는 방식). search pipeline이 점수 정규화·결합을 담당.
    """
    return {
        "size": top_k,
        "query": {
            "hybrid": {
                "queries": [
                    {
                        "multi_match": {
                            "query": query_text,
                            "fields": [
                                "description^2",
                                "table",
                                "column",
                                "synonyms",
                                "ddl_snippet",
                            ],
                        }
                    },
                    {
                        "knn": {
                            "embedding": {
                                "vector": embedding,
                                "k": top_k,
                            }
                        }
                    },
                ]
            }
        },
        # 임베딩 벡터는 응답에서 제외(페이로드 절감).
        "_source": {"excludes": ["embedding"]},
    }


def normalize_hits(response: dict[str, Any]) -> list[RetrievalHit]:
    """OpenSearch 응답을 RetrievalHit 리스트로 정규화한다."""
    hits = response.get("hits", {}).get("hits", [])
    results: list[RetrievalHit] = []
    for hit in hits:
        source = hit.get("_source", {})
        results.append(
            RetrievalHit(
                doc_type=source.get("doc_type", "table"),
                table=source.get("table"),
                column=source.get("column"),
                description=source.get("description"),
                ddl_snippet=source.get("ddl_snippet"),
                score=hit.get("_score", 0.0),
            )
        )
    return results


@dataclass
class OpenSearchConfig:
    endpoint: str
    index: str
    region: str = "us-west-2"
    # hybrid 점수 정규화·결합을 수행하는 search pipeline 이름(sample-data 컴포넌트가 생성).
    search_pipeline: str = "t2sql-hybrid-pipeline"
    # SigV4 서명 서비스명. 이 솔루션은 관리형 OpenSearch 도메인(opensearch.Domain)을
    # 배포하므로 "es" 가 기본. OpenSearch Serverless 로 바꾸면 OPENSEARCH_SERVICE=aoss.
    # (seed 인덱서 index_schema_docs.py 도 "es" 로 서명 — 정합 유지)
    service: str = "es"

    @classmethod
    def from_env(cls) -> OpenSearchConfig:
        endpoint = os.environ["OPENSEARCH_ENDPOINT"]
        return cls(
            endpoint=endpoint,
            index=os.environ.get("OPENSEARCH_INDEX", "t2sql-schema-docs"),
            region=os.environ.get("AWS_REGION", "us-west-2"),
            search_pipeline=os.environ.get("OPENSEARCH_SEARCH_PIPELINE", "t2sql-hybrid-pipeline"),
            service=os.environ.get("OPENSEARCH_SERVICE", "es"),
        )


class OpenSearchHybridRetriever(SemanticRetriever):
    """OpenSearch hybrid(vector+BM25) 검색 구현체.

    질의 → titan-embed-text-v2 임베딩 → hybrid 쿼리(knn + match, search pipeline 경유) → 정규화.
    """

    def __init__(
        self,
        config: OpenSearchConfig | None = None,
        embedding_client: EmbeddingClient | None = None,
        client: Any | None = None,
    ) -> None:
        self.config = config or OpenSearchConfig.from_env()
        self.embedding_client = embedding_client or EmbeddingClient(region=self.config.region)
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = _build_opensearch_client(self.config)
        return self._client

    def search(self, query: str, top_k: int = 5) -> list[RetrievalHit]:
        embedding = self.embedding_client.embed(query)
        body = build_hybrid_query(embedding, query, top_k)
        response = self.client.search(
            index=self.config.index,
            body=body,
            params={"search_pipeline": self.config.search_pipeline},
        )
        return normalize_hits(response)


def _build_opensearch_client(config: OpenSearchConfig) -> OpenSearch:
    import boto3

    credentials = boto3.Session().get_credentials()
    auth = AWSV4SignerAuth(credentials, config.region, config.service)
    host = config.endpoint.replace("https://", "").replace("http://", "")
    return OpenSearch(
        hosts=[{"host": host, "port": 443}],
        http_auth=auth,
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection,
        pool_maxsize=20,
    )
