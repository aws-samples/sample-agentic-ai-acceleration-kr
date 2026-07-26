"""Semantic 검색 — 저장소 중립 인터페이스(ARCHITECTURE.md §8).

추상 ``SemanticRetriever`` base + 구현체들:
- ``OpenSearchHybridRetriever``: 스키마 메타데이터(``t2sql-schema-docs``) hybrid 검색 (M1).
- ``SemanticTermRetriever``: 비즈니스 용어/few-shot(``t2sql-semantic``) hybrid 검색 (M2).
- ``GraphTraverser``: Neptune(neptunedata openCypher) join-path 순회 (M2).
- ``CompositeRetriever``: 위 셋을 결합해 스키마+용어+join path를 한 번에 반환 (M2).

인터페이스는 저장소 중립을 유지하여 Neptune 미배포 환경에서도 OpenSearch 단독으로
graceful degrade 한다.
"""

from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from typing import Any

from opensearchpy import AWSV4SignerAuth, OpenSearch, RequestsHttpConnection

from semantic_retrieval_mcp.embedding import EmbeddingClient

logger = logging.getLogger("semantic_retrieval_mcp.retriever")


@dataclass
class RetrievalHit:
    """정규화된 검색 결과 한 건. 도구 계약(search_schema)의 results 원소와 대응.

    M2에서 additive 필드(term/synonyms/sql_fragment/join_paths)를 추가했다. 기존
    소비자(orchestrator mcp_parsing, UI)는 필요한 필드만 읽으므로 하위호환이 유지된다.
    """

    doc_type: str  # "table" | "column" | "term" | "fewshot"
    table: str | None
    column: str | None
    description: str | None
    ddl_snippet: str | None
    score: float
    # ── M2 additive 필드 (기본 None — 기존 doc_type=table|column 히트는 그대로) ──
    term: str | None = None
    synonyms: list[str] | None = None
    sql_fragment: str | None = None
    join_paths: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_type": self.doc_type,
            "table": self.table,
            "column": self.column,
            "description": self.description,
            "ddl_snippet": self.ddl_snippet,
            "score": self.score,
            "term": self.term,
            "synonyms": self.synonyms,
            "sql_fragment": self.sql_fragment,
            "join_paths": self.join_paths,
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


def build_semantic_hybrid_query(
    embedding: list[float], query_text: str, top_k: int, *, published_only: bool = True
) -> dict[str, Any]:
    """t2sql-semantic 인덱스용 hybrid(knn + BM25 multi_match) 쿼리 본문을 구성한다.

    필드 목록이 스키마 인덱스와 다르므로(term/synonyms/definition/question/sql) 별도
    빌더로 분리한다. ``published_only`` 이면 status=published 필터를 함께 건다(방어적 —
    published 만 동기화된다고 가정하되 status 필드가 있으면 걸러낸다).

    ⚠️ OpenSearch 의 ``hybrid`` 쿼리는 compound 쿼리(bool 등) 안에 중첩할 수 없다.
    따라서 status 필터는 ``post_filter`` 로 건다(쿼리 단계 이후 적용 — hybrid 점수
    정규화에는 영향 없음).
    """
    body: dict[str, Any] = {
        "size": top_k,
        "query": {
            "hybrid": {
                "queries": [
                    {
                        "multi_match": {
                            "query": query_text,
                            "fields": [
                                "term^2",
                                "synonyms",
                                "definition",
                                "question",
                                "sql",
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
        "_source": {"excludes": ["embedding"]},
    }
    if published_only:
        # published 문서만 반환하되, status 필드가 없는 문서도 통과시킨다(방어).
        body["post_filter"] = {
            "bool": {
                "should": [
                    {"term": {"status": "published"}},
                    {"bool": {"must_not": {"exists": {"field": "status"}}}},
                ],
                "minimum_should_match": 1,
            }
        }
    return body


def _coerce_str_list(value: Any) -> list[str] | None:
    """synonyms 같은 필드를 문자열 리스트로 방어적 정규화(문자열·리스트·None 모두 허용)."""
    if value is None:
        return None
    if isinstance(value, str):
        return [value] if value else None
    if isinstance(value, (list, tuple)):
        items = [str(v) for v in value if v is not None and str(v) != ""]
        return items or None
    return [str(value)]


def normalize_semantic_hits(response: dict[str, Any]) -> list[RetrievalHit]:
    """t2sql-semantic 응답을 RetrievalHit(doc_type=term|fewshot)로 정규화한다.

    - term 히트: description=definition, term/synonyms/sql_fragment 채움.
    - fewshot 히트: description=question, ddl_snippet=sql (기존 필드 재활용).
    entity_type 이 없으면 question/sql 존재 여부로 fewshot 을 추정한다(방어).
    """
    hits = response.get("hits", {}).get("hits", [])
    results: list[RetrievalHit] = []
    for hit in hits:
        source = hit.get("_source", {})
        score = hit.get("_score", 0.0)
        entity_type = source.get("entity_type")
        is_fewshot = entity_type == "fewshot" or (
            entity_type is None and (source.get("question") or source.get("sql"))
        )
        if is_fewshot:
            results.append(
                RetrievalHit(
                    doc_type="fewshot",
                    table=None,
                    column=None,
                    description=source.get("question"),
                    ddl_snippet=source.get("sql"),
                    score=score,
                )
            )
        else:
            results.append(
                RetrievalHit(
                    doc_type="term",
                    table=None,
                    column=None,
                    description=source.get("definition"),
                    ddl_snippet=None,
                    score=score,
                    term=source.get("term"),
                    synonyms=_coerce_str_list(source.get("synonyms")),
                    sql_fragment=source.get("sql_fragment"),
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

    @classmethod
    def semantic_from_env(cls) -> OpenSearchConfig:
        """용어/few-shot 인덱스(``t2sql-semantic``)용 설정. 엔드포인트·리전·서비스·search
        pipeline 은 스키마 인덱스와 공유하고 index 만 ``SEMANTIC_INDEX`` 로 분리한다."""
        endpoint = os.environ["OPENSEARCH_ENDPOINT"]
        return cls(
            endpoint=endpoint,
            index=os.environ.get("SEMANTIC_INDEX", "t2sql-semantic"),
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


class SemanticTermRetriever(SemanticRetriever):
    """용어/few-shot 인덱스(``t2sql-semantic``) hybrid 검색 구현체.

    질의 → 임베딩 → build_semantic_hybrid_query(term/synonyms/definition/question/sql +
    knn) → normalize_semantic_hits(doc_type=term|fewshot). OpenSearchHybridRetriever 와
    같은 클라이언트/임베딩 패턴을 재사용하되 인덱스·쿼리 빌더·정규화만 교체한다.
    """

    def __init__(
        self,
        config: OpenSearchConfig | None = None,
        embedding_client: EmbeddingClient | None = None,
        client: Any | None = None,
        published_only: bool = True,
    ) -> None:
        self.config = config or OpenSearchConfig.semantic_from_env()
        self.embedding_client = embedding_client or EmbeddingClient(region=self.config.region)
        self._client = client
        self.published_only = published_only

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = _build_opensearch_client(self.config)
        return self._client

    def search(self, query: str, top_k: int = 5) -> list[RetrievalHit]:
        embedding = self.embedding_client.embed(query)
        body = build_semantic_hybrid_query(
            embedding, query, top_k, published_only=self.published_only
        )
        response = self.client.search(
            index=self.config.index,
            body=body,
            params={"search_pipeline": self.config.search_pipeline},
        )
        return normalize_semantic_hits(response)


def build_join_path_query() -> str:
    """테이블 집합 사이의 join 조건(rel.on)을 조회하는 openCypher 질의.

    파라미터 ``$tables`` 로 대상 테이블명 리스트를 받아 1~2 hop JOINS 경로의 join 조건
    문자열을 중복 없이 반환한다. neptunedata execute_open_cypher_query 로 실행한다.
    """
    return (
        "MATCH (t1:Table)-[j:JOINS*1..2]-(t2:Table) "
        "WHERE t1.name IN $tables AND t2.name IN $tables AND t1.name < t2.name "
        "UNWIND j AS rel "
        "RETURN DISTINCT rel.on AS join_on"
    )


class GraphTraverser:
    """Neptune(neptunedata) openCypher join-path 순회.

    boto3 ``neptunedata`` 클라이언트를 lazy 생성한다(엔드포인트/리전은 env 기반). 순회 중
    어떤 예외가 발생해도 빈 리스트를 반환하고 warning 로그만 남겨 graceful degrade 한다
    (Neptune 미배포·네트워크 오류 환경에서도 검색 전체가 실패하지 않도록).
    """

    def __init__(
        self,
        endpoint: str | None = None,
        region: str | None = None,
        client: Any | None = None,
    ) -> None:
        self.endpoint = endpoint or os.environ.get("GRAPH_ENDPOINT")
        self.region = region or os.environ.get("AWS_REGION", "us-west-2")
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client(
                "neptunedata", endpoint_url=self.endpoint, region_name=self.region
            )
        return self._client

    def find_join_paths(self, tables: list[str]) -> list[str]:
        """대상 테이블들 사이의 join 조건 문자열 리스트를 반환한다(중복 제거·정렬)."""
        unique_tables = sorted({t for t in tables if t})
        if len(unique_tables) < 2:
            # join 은 최소 2개 테이블이 필요.
            return []
        try:
            response = self.client.execute_open_cypher_query(
                openCypherQuery=build_join_path_query(),
                parameters=json.dumps({"tables": unique_tables}),
            )
            return _parse_join_path_response(response)
        except Exception as exc:  # noqa: BLE001 — 그래프 순회 실패는 graceful degrade
            logger.warning("graph_traversal_error: %s: %s", type(exc).__name__, exc)
            return []


def _parse_join_path_response(response: dict[str, Any]) -> list[str]:
    """neptunedata openCypher 응답에서 join_on 값을 방어적으로 추출(중복 제거·정렬)."""
    rows = response.get("results") or response.get("result") or []
    joins: list[str] = []
    for row in rows:
        if isinstance(row, dict):
            value = row.get("join_on")
        else:
            value = row
        if value is None or str(value) == "":
            continue
        joins.append(str(value))
    return sorted(set(joins))


class CompositeRetriever(SemanticRetriever):
    """스키마 + 용어/few-shot + Neptune join-path 를 결합하는 M2 조합 retriever.

    ① schema 검색(top_k) ② term/fewshot 검색(top_k) ③ 두 결과의 table 집합으로 join path
    조회 → table 히트들에 join_paths 부착. table 히트가 없으면 join_paths 만 담은 별도
    hit 1건을 추가한다. 결과는 score 내림차순 병합 후 최대 top_k*2 로 자른다.
    graph_traverser 가 None 이면(Neptune 비활성) join path 단계를 건너뛴다.
    """

    def __init__(
        self,
        schema_retriever: SemanticRetriever,
        term_retriever: SemanticRetriever,
        graph_traverser: GraphTraverser | None = None,
    ) -> None:
        self.schema_retriever = schema_retriever
        self.term_retriever = term_retriever
        self.graph_traverser = graph_traverser

    def search(self, query: str, top_k: int = 5) -> list[RetrievalHit]:
        schema_hits = self.schema_retriever.search(query, top_k=top_k)
        # 용어/few-shot 검색 실패(인덱스 미생성·매핑 오류 등)가 스키마 검색까지
        # 무너뜨리지 않도록 graceful degrade 한다.
        try:
            term_hits = self.term_retriever.search(query, top_k=top_k)
        except Exception as exc:  # noqa: BLE001 — 보조 검색 실패는 경고 후 계속
            logger.warning("term_retrieval_error: %s: %s", type(exc).__name__, exc)
            term_hits = []

        join_paths: list[str] = []
        if self.graph_traverser is not None:
            tables = _collect_tables(schema_hits, term_hits)
            join_paths = self.graph_traverser.find_join_paths(tables)

        merged = _merge_hits(schema_hits, term_hits, join_paths)
        merged.sort(key=lambda h: h.score, reverse=True)
        return merged[: top_k * 2]


def _collect_tables(*hit_lists: list[RetrievalHit]) -> list[str]:
    """여러 히트 리스트에서 table 값을 모아 중복 제거·정렬한다."""
    tables: set[str] = set()
    for hits in hit_lists:
        for hit in hits:
            if hit.table:
                tables.add(hit.table)
    return sorted(tables)


def _merge_hits(
    schema_hits: list[RetrievalHit],
    term_hits: list[RetrievalHit],
    join_paths: list[str],
) -> list[RetrievalHit]:
    """schema/term 히트를 병합하고 join_paths 를 부착한다.

    join_paths 가 있으면 doc_type=table 히트들에 부착하고, table 히트가 하나도 없으면
    join_paths 만 담은 별도 hit(doc_type=table, score=0.0) 1건을 추가한다.
    """
    table_hits = [replace(h) for h in schema_hits if h.doc_type == "table"]
    other_hits = [replace(h) for h in schema_hits if h.doc_type != "table"]
    term_hits = [replace(h) for h in term_hits]

    if join_paths:
        if table_hits:
            for hit in table_hits:
                hit.join_paths = list(join_paths)
        else:
            other_hits.append(
                RetrievalHit(
                    doc_type="table",
                    table=None,
                    column=None,
                    description="join paths",
                    ddl_snippet=None,
                    score=0.0,
                    join_paths=list(join_paths),
                )
            )
    return [*table_hits, *other_hits, *term_hits]


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
