# M2 / M3 인터페이스 기록 (Interface Record)

> M2 → M3 순차 구현에서 마일스톤이 산출·소비하는 인터페이스(리소스명·도구 시그니처·
> env var·CDK export)의 기록. 구현이 확정될 때마다 갱신한다.
> (원래 병렬 트랙용 접점 계약이었으나 순차 방식 전환으로 기록 문서로 축소 — 2026-07-26)

## 1. Orchestrator 도구 평면 (M3에서 Gateway 전환 예정)

현재 `orchestrator/mcp_client.py`는 Runtime MCP 서버에 직접(SigV4) 연결한다.
M3에서 Gateway 경유로 교체할 수 있도록 클라이언트 생성부를 설정 기반으로 유지한다.

- env: `SQL_MCP_ARN` / `SEMANTIC_MCP_ARN` (direct 모드), M3에서 `TOOL_PLANE_MODE`·`GATEWAY_URL` 추가 예정.
- M2는 도구 시그니처(§2)만 준수하면 클라이언트 경로에 영향 없음.

## 2. MCP 도구 시그니처

### 2.1 `search_schema` (semantic-retrieval-mcp — M2에서 확장)

```
search_schema(query: str, top_k: int = 5) ->
  {"results": [{
      "doc_type": "table"|"column"|"term"|"fewshot",   # M2: term/fewshot 추가
      "table": str|null, "column": str|null,
      "description": str|null, "ddl_snippet": str|null,
      "score": float,
      # M2 확장 필드 (additive — 기존 소비자는 무시해도 안전):
      "term": str|null, "synonyms": [str]|null, "sql_fragment": str|null,
      "join_paths": [str]|null      # Neptune 순회 결과
  }]}
```

- **기존 필드 제거/개명 금지** (orchestrator `mcp_parsing.py`와 UI가 소비). 신규 필드는 additive only.

### 2.2 `run_sql` (sql-execution-mcp — M3에서 확장)

```
run_sql(sql: str, datasource: str = "aurora") ->
  성공: {"status":"ok","columns":[...],"rows":[[...]],"row_count":N,"truncated":bool}
  거부: {"status":"rejected","reason":"...","rule":"..."}
  오류: {"status":"error","message":"..."}
```

- M3: `datasource`(`"aurora"` | `"redshift"`) 추가, 기본값 aurora로 하위호환 유지.
  validator dialect는 datasource별 분기.

## 3. SemanticRetriever 인터페이스 (M2)

`semantic_retrieval_mcp/retriever.py`의 추상 `SemanticRetriever`는 유지·확장:

- M2 추가: `GraphAugmentedRetriever` — OpenSearch hybrid 결과 + Neptune join-path 순회 결합.
- env `SEMANTIC_GRAPH_ENABLED`(기본 false)로 조합 retriever 선택 — Neptune 미배포
  환경에서도 OpenSearch 단독으로 동작 (graceful degrade).

## 4. Clarification(interrupt)의 AG-UI 표면화 (M2)

⚠️ Strands interrupt API 리서치 확정 후 갱신.

- 이벤트 value 스키마(프레임): `{"interrupt_id": str, "question": str,
  "fields": [{"name","label","type":"select"|"date_range"|"text","options":[...]}]}`
- 사용자 응답: 동일 threadId 재호출 + `forwardedProps.clarificationResponse =
  {"interrupt_id": str, "values": {...}}` → `request.py` `ParsedRequest`에
  `clarification_response` additive 추가.

## 5. CDK 리소스·export·env 기록

### 5.1 스택 구성

```
AgenticT2SqlBaseStack      (기존 — M3 에서 Redshift Serverless·M2M 클라이언트 추가)
AgenticT2SqlSemanticStack  (신규 M2 — DynamoDB·Neptune·동기화. Base 의존)
AgenticT2SqlRuntimeStack   (기존 — M3 env 추가: TOOL_PLANE_MODE/GATEWAY_URL, REDSHIFT_*)
AgenticT2SqlGatewayStack   (신규 M3 — Gateway·Cedar·Identity. Base+Runtime 의존, Runtime 이후 배포)
AgenticT2SqlUiStack        (기존)
```

**배포 순서**: Base → Semantic → (이미지 push) → Runtime → **Gateway** → UI.
Gateway 는 runtime MCP ARN 을 참조하므로 runtime 이후 배포된다(runtime→gateway 역참조 없음 → 사이클 없음).

**M3 배치 결정 (확정)**:
- **Redshift 는 Gateway 가 아닌 Base 스택**에 둔다. 이유: (1) runtime 이 Gateway 보다 먼저 배포되므로
  sql-mcp 의 `REDSHIFT_SECRET_ARN` 을 Gateway 산출물로 주입하려면 배포 후 update-agent-runtime 이 필요
  → 대신 Base 소유로 두면 object 참조(결정적 cross-stack export)로 runtime 에 직접 주입된다. (2) sql-mcp
  실행 role 이 Base 소유라 Redshift 권한도 Base 에서 부여하는 게 자연스럽다.
- **Redshift 전용 3-AZ VPC(`agentic-t2sql-rs-vpc`)를 Base 에 별도 생성.** Redshift Serverless workgroup 은
  3개 AZ 서브넷을 요구하는데 기존 base VPC 는 maxAzs:2 이고, 배포된 VPC 의 AZ 수 변경은 파괴적 교체라
  회피. Data API 만 쓰므로 NAT 없는 PRIVATE_ISOLATED 로 비용 최소화.
- **Gateway↔PolicyEngine 연결은 L2 escape hatch** (`(gateway.node.defaultChild as CfnGateway)
  .policyEngineConfiguration = {arn, mode:'ENFORCE'}`). L2 Gateway 에 policyEngine prop 없음(확인).
- **Cedar action 이름 규칙 `<TargetName>___<toolName>`** (트리플 언더스코어, AWS 공식 문서 gateway-tool-naming).
  target 이름: `sql-execution-mcp` / `semantic-retrieval-mcp` → action `sql-execution-mcp___run_sql`,
  `semantic-retrieval-mcp___search_schema`.

### 5.2 신규 리소스 이름

| 리소스 | 이름 | 마일스톤 |
|---|---|---|
| DynamoDB 테이블 (semantic system-of-record) | `agentic-t2sql-semantic` | M2 |
| Neptune 클러스터 (또는 Analytics 그래프) | `agentic-t2sql-graph` | M2 |
| Gateway | `agentic-t2sql-gateway` | M3 |
| PolicyEngine | `agentic_t2sql_policy_engine` (⚠️ 언더스코어만, 하이픈 불가) | M3 |
| Gateway MCP target | `sql-execution-mcp` / `semantic-retrieval-mcp` | M3 |
| Redshift Serverless namespace / workgroup | `agentic-t2sql-rs-ns` / `agentic-t2sql-rs-wg` | M3 |
| Redshift read-only 시크릿 | `agentic-t2sql/redshift/agent_ro` | M3 |
| M2M(USER_PASSWORD_AUTH) Cognito 클라이언트 | `agentic-t2sql-m2m-client` | M3 |

### 5.3 신규 CfnOutput exportName

| exportName | 값 | 마일스톤 |
|---|---|---|
| `agentic-t2sql-semantic-table-name` / `-arn` | DynamoDB 테이블명/ARN | M2 |
| `agentic-t2sql-graph-endpoint` | Neptune 엔드포인트 | M2 |
| `agentic-t2sql-gateway-url` / `-id` | Gateway MCP URL / ID (Gateway 스택) | M3 |
| `agentic-t2sql-redshift-workgroup` / `-secret-arn` | workgroup / RS 시크릿 (Base 스택) | M3 |
| `agentic-t2sql-m2m-client-id` | M2M Cognito 클라이언트 ID (Base 스택) | M3 |

### 5.4 Runtime env var 추가분

| env | 대상 런타임 | 마일스톤 |
|---|---|---|
| `SEMANTIC_TABLE_NAME`, `GRAPH_ENDPOINT`, `SEMANTIC_GRAPH_ENABLED` | semantic-retrieval-mcp | M2 |
| `TOOL_PLANE_MODE`, `GATEWAY_URL` | orchestrator | M3 |
| `REDSHIFT_WORKGROUP`, `REDSHIFT_DB`, `REDSHIFT_SECRET_ARN` | sql-execution-mcp | M3 |

## 6. 이미지 태그

- `build-and-push.sh`가 `TAG` env(기본 `latest`)를 지원한다. 배포 전 검증·롤백용
  마일스톤 태그(`m2-<sha>` 등)를 쓸 수 있고, Runtime 스택은 `latest`를 참조한다.

## 7. E2E 검증 시나리오 (e2e-smoke.sh 확장 계획)

1. M1 회귀: 기존 3레벨 10체크 전부 PASS
2. M2 clarification: 모호 질의 → clarification 이벤트 수신 → 응답 재호출 → 정상 완료
3. M2 semantic: 비즈니스 용어 질의가 DynamoDB published 정의·Neptune join path를 반영
4. M3 Cedar 거부: 권한 없는 principal의 도구 호출 → 거부 확인
5. M3 Redshift: `datasource="redshift"` 질의 정상 실행 + DELETE 거부
