# semantic-retrieval-mcp

AgentCore Runtime에 호스팅되는 MCP 서버. schema linking 단계에서 자연어 질의와 관련된
**스키마/용어 컨텍스트**를 OpenSearch hybrid(vector+BM25) 검색으로 반환한다.

## 도구

### `search_schema(query: str, top_k: int = 5) -> dict`

- 반환: `{"results":[{"doc_type":"table"|"column","table":...,"column":...,"description":...,"ddl_snippet":...,"score":...}]}`
- 검색 실패 시 graceful: `{"results":[], "error":"..."}`

## 동작

1. 질의 → `amazon.titan-embed-text-v2:0` 임베딩(1024차원, bedrock-runtime InvokeModel)
2. OpenSearch `hybrid` 쿼리(kNN 벡터 + `multi_match` BM25) — search pipeline이 점수 정규화·결합
3. 결과를 `RetrievalHit`로 정규화

임베딩 인덱스 매핑(knn_vector 1024차원 + text 필드)과 hybrid search pipeline은 **sample-data
컴포넌트가 생성**한다고 가정한다.

## 저장소 중립 설계 (ARCHITECTURE.md §8)

추상 `SemanticRetriever` base + `OpenSearchHybridRetriever` 구현체. M2에서 Neptune 순회
retriever를 같은 인터페이스로 추가할 수 있도록 저장소 중립으로 설계.

## AgentCore Runtime MCP 호스팅 규격 (2026-07 검증)

`FastMCP(host="0.0.0.0", stateless_http=True)` + `mcp.run(transport="streamable-http")`.
컨테이너는 `0.0.0.0:8000`에서 `POST /mcp`를 노출한다(stateless streamable-HTTP).

## 로컬 실행

```bash
uv sync
uv run pytest
uv run ruff check .

export OPENSEARCH_ENDPOINT=... OPENSEARCH_INDEX=t2sql-schema-docs AWS_REGION=us-west-2
uv run python -m semantic_retrieval_mcp.server
# → http://0.0.0.0:8000/mcp
```

## 환경 변수

`.env.example` 참고.

| 변수 | 설명 |
|---|---|
| `OPENSEARCH_ENDPOINT` | OpenSearch 엔드포인트 |
| `OPENSEARCH_INDEX` | 인덱스명 (기본 `t2sql-schema-docs`) |
| `OPENSEARCH_SEARCH_PIPELINE` | hybrid 점수 정규화 search pipeline (기본 `t2sql-hybrid-pipeline`) |
| `OPENSEARCH_SERVICE` | SigV4 서비스명: Serverless=`aoss`, 관리형 도메인=`es` |
| `EMBEDDING_MODEL_ID` | 임베딩 모델 (기본 `amazon.titan-embed-text-v2:0`) |
| `AWS_REGION` | 리전 (기본 `us-west-2`) |
