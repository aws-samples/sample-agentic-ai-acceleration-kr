# M2 / M3 / M4 인터페이스 기록 (Interface Record)

> 마일스톤이 산출·소비하는 인터페이스(리소스명·도구 시그니처·env var·CDK export)의 기록.
> 구현이 확정될 때마다 갱신한다.
> (원래 병렬 트랙용 접점 계약이었으나 순차 방식 전환으로 기록 문서로 축소 — 2026-07-26.
>  M4 는 모듈 병렬 구현을 위해 §8 을 착수 전 계약으로 먼저 고정한다 — 2026-07-26)

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
6. M4 admin: Manager 로그인 → 큐레이션 CRUD(candidate) → 승인(publish) → 전파 확인 →
   Cedar admin 도구 접근 제어(User 차단·Manager 허용) → admin panel ALB 헬스 (§8.7)

## 8. M4 접점 계약 (Admin Panel · datasource-admin-mcp) — 착수 전 고정

> M4 는 4개 모듈(admin-mcp / admin web / infra / e2e)을 병렬 구현하므로, 아래 계약을
> 구현 에이전트 프롬프트에 동일하게 박는다. **변경 시 이 문서를 먼저 갱신**한다.

### 8.0 아키텍처 결정 (확정)

- **semantic 쓰기 경로 단일화**: admin web API 는 DynamoDB 를 직접 쓰지 않는다.
  큐레이션·승인·데이터소스 작업은 전부 **사용자 JWT Bearer → Gateway MCP →
  `datasource-admin-mcp`**(SemanticRepository 재사용) 경로다. 이로써
  (1) DynamoDB 단일 쓰기 지점 유지, (2) Cedar 가 Manager/Admin 인가를 강제,
  (3) M3 이월 부채인 **사용자별 JWT On-Behalf-Of** 가 admin 경로에 실현된다.
- **admin panel = Next.js 단일 컨테이너**(web + API routes, 기존 ui/ 와 동형)를
  ECS Fargate + 전용 ALB 로 호스팅. API route 가 Cognito JWT 를 검증(aws-jwt-verify)
  하고 그룹 클레임으로 Admin/Manager 화면·기능을 분리한다.
- **Cognito 사용자·그룹 관리 / Cedar 조회 / CloudWatch·X-Ray 조회**는 MCP 도구가 아니라
  admin web task role 의 AWS SDK 직접 호출(읽기 위주 관리 평면 — 도구 평면과 무관).
- **Cedar 2-phase**: gateway 스택은 context `cedarActionScoping`(기본 false)을 받는다.
  false 면 M3 과 동일한 광역 permit + admin 도구 forbid-미보유그룹, true 면 action 목록
  스코프 정책으로 교체. admin target 생성 배포 → `-c cedarActionScoping=true` 재배포의
  2-phase (M3 학습: action 목록은 target 도구 동기화 후에만 검증 통과).

### 8.1 디렉토리·스택

```
agents/datasource-admin-mcp/   # Python uv + FastMCP (다른 MCP 서버와 동형 레이아웃)
admin/                         # Next.js admin web + API (ui/ 와 동형: Dockerfile, .env.example)
infra/lib/admin-stack.ts       # AgenticT2SqlAdminStack (ECS Fargate + ALB)
```

스택 배선: `AgenticT2SqlAdminStack` 은 base(VPC·ECR·Cognito·role)·gateway(gatewayUrl·
policyEngineId)·runtime 이후 배포. **배포 순서**: base(ECR·role 추가) → 이미지 push
(admin-mcp) → runtime(admin runtime 추가) → gateway(target 추가) →
gateway 2-phase(`-c cedarActionScoping=true`) → 이미지 push(admin-web) → admin →
`admin-outputs.json`.

### 8.2 신규 리소스 이름

| 리소스 | 이름 |
|---|---|
| ECR | `agentic-t2sql/datasource-admin-mcp` / `agentic-t2sql/admin-web` |
| Runtime | `agentic_t2sql_datasource_admin_mcp` (MCP protocol, PUBLIC) |
| Gateway MCP target | `datasource-admin-mcp` → 도구명 `datasource-admin-mcp___<tool>` |
| IAM role | `agentic-t2sql-admin-mcp-role` / `agentic-t2sql-admin-web-task-role` / `-admin-web-exec-role` |
| ALB | `agentic-t2sql-admin-alb` |
| 데이터소스 연결 시크릿 | `agentic-t2sql/datasource/<datasource_id>` |
| E2E Manager 테스트 사용자 | `e2e-manager@example.com` (Manager 그룹, 비밀번호는 기존 e2e 시크릿 공유) |

### 8.3 datasource-admin-mcp 도구 시그니처

모든 도구는 JSON dict 반환, 실패는 `{"status":"error","message":...}` 로 정규화.
`actor` 인자는 감사 기록용(admin web 이 JWT 의 username 을 전달).

```
list_entities(entity_type: str|None = None, status: str|None = None)
  -> {"status":"ok","entities":[{pk,sk,entity_type,entity_id,status,version,updated_at,updated_by,...payload}]}
     # embedding 필드는 응답에서 제거(payload 경량화)
get_entity(entity_type: str, entity_id: str) -> {"status":"ok","entity": {...}|None}
put_entity(entity_type: str, entity_id: str, payload: dict, status: str = "candidate",
           actor: str = "admin-panel") -> {"status":"ok","entity": {...}}
publish_entity(entity_type: str, entity_id: str, actor: str = "admin-panel")
  -> {"status":"ok","entity": {...}}   # SemanticRepository.publish()
unpublish_entity(entity_type: str, entity_id: str, actor: str = "admin-panel")
  -> {"status":"ok","entity": {...}}
register_datasource(datasource_id: str, engine: "aurora-postgresql"|"redshift-serverless",
                    config: dict, actor: str = "admin-panel")
  -> {"status":"ok","secret_arn": str}
  # config(호스트/DB/자격증명 등)를 Secrets Manager `agentic-t2sql/datasource/<id>` 에 저장,
  # 연결 메타(자격증명 제외)는 DynamoDB entity_type="datasource" 로 기록(candidate)
test_datasource(datasource_id: str) -> {"status":"ok","ok":bool,"detail":str}
  # 내장 소스(aurora/redshift)는 Data API SELECT 1, 등록 소스는 메타 검증
crawl_schema(datasource_id: str, actor: str = "admin-panel")
  -> {"status":"ok","tables":N,"columns":N,"joins":N}
  # information_schema 크롤 → table/column/join 엔티티 put(candidate) — 승인 후 published
```

- `entity_type` 유효값은 SemanticRepository 의 `term|fewshot|table|column|join` +
  M4 additive `datasource` (repository VALID_ENTITY_TYPES 확장 — additive only).
- semantic-layer 패키지는 admin-mcp 이미지에 로컬 경로 의존성으로 포함
  (빌드 컨텍스트는 레포 루트 기준 — Dockerfile 에서 `COPY semantic-layer ...`,
  build-and-push.sh 의 컨텍스트 경로 주의).

### 8.4 admin web API 경로 (Next.js API routes)

인증: `Authorization: Bearer <Cognito AccessToken>` — aws-jwt-verify 로 검증,
`cognito:groups` 클레임으로 인가. Manager|Admin 아니면 403. 미인증 401.

```
POST /api/auth/login                       {username,password} → tokens (USER_PASSWORD_AUTH, m2m 클라이언트)
GET  /api/health                           (인증 불필요)
GET  /api/semantic/entities?type=&status=  → MCP list_entities (사용자 토큰 OBO)
PUT  /api/semantic/entities/{type}/{id}    → MCP put_entity(candidate)
POST /api/semantic/entities/{type}/{id}/publish|unpublish → MCP publish/unpublish
GET  /api/approvals                        → MCP list_entities(status=candidate)
GET/POST /api/datasources                  → MCP list_entities(type=datasource) / register_datasource
POST /api/datasources/{id}/test|crawl      → MCP test_datasource / crawl_schema
GET  /api/iam/users, POST /api/iam/users   (Admin 전용 — cognito-idp Admin* 직접 호출)
POST /api/iam/users/{username}/groups      (Admin 전용)
GET  /api/cedar/policies                   read-only (ListPolicies/GetPolicy)
GET  /api/metrics/summary                  CloudWatch GetMetricData 요약
GET  /api/traces/sessions, /api/traces/{id} X-Ray/CloudWatch 로그 기반 세션·타임라인
```

### 8.5 env var

| env | 대상 | 값 |
|---|---|---|
| `SEMANTIC_TABLE_NAME`, `EMBEDDING_MODEL_ID` | admin-mcp | semantic 테이블·Titan embed |
| `AURORA_CLUSTER_ARN`, `AURORA_SECRET_ARN`, `DB_NAME` | admin-mcp | 스키마 크롤(agent_ro) |
| `REDSHIFT_WORKGROUP`, `REDSHIFT_DB`, `REDSHIFT_SECRET_ARN` | admin-mcp | redshift 크롤 |
| `DATASOURCE_SECRET_PREFIX` | admin-mcp | `agentic-t2sql/datasource/` |
| `AWS_REGION`, `COGNITO_USER_POOL_ID`, `COGNITO_CLIENT_ID` | admin-web | JWT 검증·로그인(m2m 클라이언트) |
| `GATEWAY_URL`, `POLICY_ENGINE_ID` | admin-web | MCP OBO 호출·Cedar 조회 |
| `ADMIN_MCP_TARGET` | admin-web | admin 도구명 프리픽스(`datasource-admin-mcp`) — target 이름 단일 원천 |
| `RUNTIME_LOG_GROUP_PREFIX` | admin-web | `/aws/bedrock-agentcore/runtimes/` (트레이스 탐색) |

orchestrator M4 additive: `forwardedProps.userAccessToken`(str) 이 오면 gateway 모드에서
M2M 서비스 토큰 대신 이 토큰을 Bearer 로 사용(사용자 위임). 없으면 기존 서비스 계정 유지.

### 8.6 CDK 출력 (admin-outputs.json 등)

| exportName / 키 | 값 | 스택 |
|---|---|---|
| `agentic-t2sql-admin-alb-url` (`AdminAlbUrl`) | admin panel ALB URL | Admin |
| `EcrAdminMcpUri` / `EcrAdminWebUri` | ECR URI 2종 | Base |
| `AdminMcpRuntimeArn` | admin MCP runtime ARN | Runtime |
| `PolicyEngineId` | (기존) Cedar 조회용 — admin 스택이 객체 참조로 소비 | Gateway |

### 8.7 E2E 레벨 6 체크 (scripts/e2e_verify.py --level 6)

1. admin ALB `/` 및 `/api/health` 200
2. `POST /api/auth/login` (e2e-manager) → 토큰 발급, 미인증 API 호출 401
3. Manager 토큰 Gateway MCP: `datasource-admin-mcp___put_entity`(term, candidate) →
   `list_entities(status=candidate)` 에 노출 → `publish_entity` → status=published
4. 수 초 대기 후 `search_schema` 에 신규 term 히트(OpenSearch 전파 확인)
5. Cedar: e2e-user(일반) 토큰으로 admin 도구 호출 → 거부(또는 tools/list 미노출),
   run_sql/search_schema 는 여전히 허용(action 스코프 회귀 확인)
6. admin API OBO: Manager 토큰으로 `GET /api/semantic/entities` 200, e2e-user 토큰 403
