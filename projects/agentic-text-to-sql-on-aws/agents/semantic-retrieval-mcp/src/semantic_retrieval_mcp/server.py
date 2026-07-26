"""FastMCP 엔트리포인트 — semantic-retrieval-mcp.

AgentCore Runtime MCP 호스팅 규격(2026-07 검증): Host 0.0.0.0, 포트 8000, POST /mcp,
stateless streamable-HTTP.

도구: search_schema(query, top_k) — 스키마/용어 컨텍스트를 OpenSearch hybrid 검색으로 반환.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from semantic_retrieval_mcp.retriever import OpenSearchHybridRetriever, SemanticRetriever

logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger("semantic_retrieval_mcp.server")

mcp = FastMCP(host="0.0.0.0", stateless_http=True)

_retriever: SemanticRetriever | None = None


def _get_retriever() -> SemanticRetriever:
    global _retriever
    if _retriever is None:
        _retriever = OpenSearchHybridRetriever()
    return _retriever


@mcp.tool()
def search_schema(query: str, top_k: int = 5) -> dict[str, Any]:
    """자연어 질의와 관련된 스키마/용어 컨텍스트를 검색한다.

    OpenSearch hybrid 검색(벡터 kNN + BM25)으로 테이블/컬럼 메타데이터·비즈니스 용어를
    찾아 schema linking 단계에 필요한 컨텍스트만 반환한다.

    Args:
        query: 자연어 질의(또는 그 일부).
        top_k: 반환할 최대 결과 수.

    Returns:
        {"results":[{"doc_type":"table"|"column","table":...,"column":...,
                     "description":...,"ddl_snippet":...,"score":...}]}
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
