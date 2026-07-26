# semantic-retrieval-mcp

AgentCore Runtime에 호스팅되는 MCP 서버. schema linking 단계에서 자연어 질의와 관련된
**스키마 메타데이터 + 비즈니스 용어/few-shot 예시 + 테이블 join path**를 OpenSearch
hybrid(vector+BM25) 검색과 Neptune 그래프 순회로 반환한다.

## 도구

### `search_schema(query: str, top_k: int = 5) -> dict`

```
{"results":[{
  "doc_type": "table"|"column"|"term"|"fewshot",
  "table": str|null, "column": str|null,
  "description": str|null, "ddl_snippet": str|null, "score": float,
  # M2 additive 필드 (용어/few-shot·join path 시에만 채워짐, 그 외 null):
  "term": str|null, "synonyms": [str]|null, "sql_fragment": str|null,
  "join_paths": [str]|null
}]}
```

- 기존 필드(`doc_type`/`table`/`column`/`description`/`ddl_snippet`/`score`)는 **제거/개명
  없이 유지** — 신규 필드는 additive only(기존 소비자는 무시해도 안전).
- 용어/few-shot 결합 시 최대 `top_k*2`까지 반환한다.
- 검색 실패 시 graceful: `{"results":[], "error":"..."}`.

## 동작

1. 질의 → `amazon.titan-embed-text-v2:0` 임베딩(1024차원, bedrock-runtime InvokeModel)
2. **스키마 검색**: `t2sql-schema-docs` 인덱스 hybrid(kNN + `multi_match` BM25) → `doc_type=table|column`
3. **용어/few-shot 검색**: `t2sql-semantic` 인덱스 hybrid(term/synonyms/definition/question/sql +
   kNN) → `doc_type=term|fewshot`. `status=published`만 반영(status 없는 문서도 방어적으로 통과)
4. **join path 순회**(Neptune 활성 시): 2·3의 table 집합으로 openCypher `JOINS*1..2` 순회 →
   join 조건 문자열을 `doc_type=table` 히트의 `join_paths`에 부착(table 히트가 없으면 별도 hit 1건 추가)
5. score 내림차순 병합 후 정규화

임베딩 인덱스 매핑(knn_vector 1024차원 + text 필드), hybrid search pipeline, `t2sql-semantic`
동기화(DynamoDB→OSIS)와 Neptune 그래프는 **다른 컴포넌트가 생성·동기화**한다고 가정한다.
이 서버는 **검색만** 담당한다.

## 저장소 중립 설계 (ARCHITECTURE.md §8)

추상 `SemanticRetriever` base + 구현체:

| 클래스 | 역할 |
|---|---|
| `OpenSearchHybridRetriever` | 스키마 메타데이터(`t2sql-schema-docs`) hybrid 검색 (M1) |
| `SemanticTermRetriever` | 비즈니스 용어/few-shot(`t2sql-semantic`) hybrid 검색 (M2) |
| `GraphTraverser` | Neptune(neptunedata openCypher) join-path 순회 (M2) |
| `CompositeRetriever` | 위 셋을 결합 — 스키마+용어+join path를 한 번에 (M2) |

- **graceful degrade**: Neptune 미배포/오류 시 `GraphTraverser`는 빈 리스트+warning 로그만
  남기고 검색 전체는 정상 동작. `SEMANTIC_INDEX`·graph env가 모두 없으면 M1과 동일하게
  `OpenSearchHybridRetriever` 단독으로 동작.

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
| `OPENSEARCH_INDEX` | 스키마 인덱스명 (기본 `t2sql-schema-docs`) |
| `SEMANTIC_INDEX` | 용어/few-shot 인덱스명 (기본 `t2sql-semantic`). 설정 시 Composite 조립 |
| `OPENSEARCH_SEARCH_PIPELINE` | hybrid 점수 정규화 search pipeline (기본 `t2sql-hybrid-pipeline`) |
| `OPENSEARCH_SERVICE` | SigV4 서비스명: Serverless=`aoss`, 관리형 도메인=`es` |
| `SEMANTIC_GRAPH_ENABLED` | Neptune join-path 순회 활성화 (`true`/`false`, 기본 `false`) |
| `GRAPH_ENDPOINT` | Neptune 엔드포인트 `https://<host>:8182` (graph 활성 시 필수) |
| `EMBEDDING_MODEL_ID` | 임베딩 모델 (기본 `amazon.titan-embed-text-v2:0`) |
| `AWS_REGION` | 리전 (기본 `us-west-2`) |

> `SEMANTIC_INDEX`·`SEMANTIC_GRAPH_ENABLED`/`GRAPH_ENDPOINT`가 모두 없으면 스키마 검색
> 단독(M1 호환)으로 동작한다. `SEMANTIC_INDEX`가 있으면 용어/few-shot을 결합하고,
> 추가로 graph가 켜지면 join path까지 채운다.
