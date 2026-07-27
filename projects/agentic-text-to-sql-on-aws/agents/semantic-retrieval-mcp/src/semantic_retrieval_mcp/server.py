"""FastMCP 엔트리포인트 — semantic-retrieval-mcp.

AgentCore Runtime MCP 호스팅 규격(2026-07 검증): Host 0.0.0.0, 포트 8000, POST /mcp,
stateless streamable-HTTP.

도구: search_schema(query, top_k) — 스키마/용어/few-shot 컨텍스트 + Neptune join path 를
OpenSearch hybrid 검색 + 그래프 순회로 반환.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from semantic_retrieval_mcp.retriever import (
    CompositeRetriever,
    GraphTraverser,
    OpenSearchHybridRetriever,
    SemanticRetriever,
    SemanticTermRetriever,
)

logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger("semantic_retrieval_mcp.server")

mcp = FastMCP(host="0.0.0.0", stateless_http=True)

_retriever: SemanticRetriever | None = None


def _env_flag(name: str, default: bool = False) -> bool:
    """"true"/"false" env 플래그를 방어적으로 해석한다."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def build_retriever() -> SemanticRetriever:
    """env 에 따라 retriever 를 조립한다.

    - semantic 확장이 꺼진 환경(``SEMANTIC_INDEX`` 미설정 & graph 비활성) → 기존
      ``OpenSearchHybridRetriever`` 단독(스키마 검색만)으로 동작한다(하위호환 보장).
    - ``SEMANTIC_INDEX`` 가 있거나 graph 가 켜지면 → 스키마 hybrid + 용어/few-shot
      (SemanticTermRetriever) 를 CompositeRetriever 로 결합.
    - 추가로 ``SEMANTIC_GRAPH_ENABLED`` 가 true 이고 ``GRAPH_ENDPOINT`` 가 있으면
      GraphTraverser 를 조립해 join path 까지 채운다(없으면 join path 단계 생략).
    """
    schema_retriever = OpenSearchHybridRetriever()

    graph_enabled = _env_flag("SEMANTIC_GRAPH_ENABLED") and bool(os.environ.get("GRAPH_ENDPOINT"))
    semantic_enabled = bool(os.environ.get("SEMANTIC_INDEX")) or graph_enabled

    if not semantic_enabled:
        logger.info("semantic_extension_disabled — schema-only retriever")
        return schema_retriever

    term_retriever = SemanticTermRetriever()

    graph_traverser: GraphTraverser | None = None
    if graph_enabled:
        graph_traverser = GraphTraverser()
        logger.info("graph_traversal_enabled endpoint=%s", os.environ.get("GRAPH_ENDPOINT"))
    else:
        logger.info("graph_traversal_disabled")

    return CompositeRetriever(
        schema_retriever=schema_retriever,
        term_retriever=term_retriever,
        graph_traverser=graph_traverser,
    )


def _get_retriever() -> SemanticRetriever:
    global _retriever
    if _retriever is None:
        _retriever = build_retriever()
    return _retriever


@mcp.tool()
def search_schema(query: str, top_k: int = 5) -> dict[str, Any]:
    """자연어 질의와 관련된 스키마/용어/few-shot 컨텍스트를 검색한다.

    OpenSearch hybrid 검색(벡터 kNN + BM25)으로 테이블/컬럼 메타데이터·비즈니스 용어·
    few-shot NLQ↔SQL 예시를 찾고, Neptune 순회(활성 시)로 관련 테이블 간 join path 를
    도출해 schema linking 단계에 필요한 컨텍스트만 반환한다.

    Args:
        query: 자연어 질의(또는 그 일부).
        top_k: 반환할 최대 결과 수(용어/few-shot 결합 시 최대 top_k*2 까지 반환).

    Returns:
        {"results":[{
            "doc_type":"table"|"column"|"term"|"fewshot",
            "table":...,"column":...,"description":...,"ddl_snippet":...,"score":...,
            # additive(용어/few-shot·join path 시에만 채워짐, 그 외 null):
            "term":...,"synonyms":[...],"sql_fragment":...,"join_paths":[...]
        }]}
    """
    top_k = max(1, min(int(top_k), 50))
    try:
        hits = _get_retriever().search(query, top_k=top_k)
    except Exception as exc:  # noqa: BLE001 — 검색 실패를 정규화(에이전트가 graceful 처리)
        logger.warning("search_schema_error: %s: %s", type(exc).__name__, exc)
        return {"results": [], "error": f"{type(exc).__name__}: {exc}"}

    return {"results": [hit.to_dict() for hit in hits]}


def main() -> None:
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
