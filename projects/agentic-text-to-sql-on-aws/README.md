# Agentic Text-to-SQL on AWS

Amazon Bedrock AgentCore 기반의 agentic Text-to-SQL 솔루션입니다. 사용자가 자연어로 데이터를 질의하면, 에이전트가 semantic layer의 비즈니스 컨텍스트를 활용해 안전한 SQL을 생성·검증·실행하고 결과를 자연어와 표로 요약합니다.

오케스트레이션은 **Strands Graph**(결정적 노드 전이 골격)로, 도구는 **AgentCore Runtime에 호스팅된 MCP 서버**로, 프론트엔드는 **AG-UI 프로토콜 + CopilotKit**으로 구성됩니다. IaC는 CDK(TypeScript)입니다.

> ⚠️ **데모/샘플 목적 전용입니다.** 이 코드는 학습과 참조를 위한 것으로, 프로덕션 사용 전에는 인증 강제(현재 미강제), HTTPS/TLS, 최소 권한 재점검, 비용·용량 재산정이 필요합니다. 실제 데이터·자격증명으로 실행할 때 유의하세요.

## 구현 범위 (M5)

이 저장소의 현재 상태는 **M5 — 개선 파이프라인**입니다. 운영 트레이스가 평가(Track A)와 semantic 지식 채굴(Track B)의 공통 원천이 되고, 두 트랙 모두 Manager 승인을 거쳐야 운영에 반영됩니다.

| 마일스톤 | 범위 | 상태 |
|---|---|---|
| **M1** | 코어 파이프라인 E2E (CDK 인프라 · Aurora 샘플 데이터 · OpenSearch 최소형 · AG-UI 통합) | ✅ 구현 |
| **M2** | Semantic layer 완성(Neptune·DynamoDB·Streams 동기화) + clarification(interrupt 재요청) | ✅ 구현 |
| **M3** | Tool/보안 완성 (Gateway · Identity · Cedar policy · Redshift 소스 · 가드레일) | ✅ 구현 |
| **M4** | Admin panel (데이터소스 등록 · semantic 큐레이션·승인 큐 · 권한 · 디버깅 · 사용자 JWT OBO) | ✅ 구현 |
| **M5** | 개선 파이프라인 (Track A: 평가→추천→bundle 승격, Track B: semantic 지식 채굴→승인 큐) | ✅ 구현 |

**M5에서 추가된 것**
- **Track A — 평가 파이프라인** (`evaluation/`, `infra/lib/evaluation-stack.ts`): gold NL→SQL 데이터셋(`goldset-v1.jsonl`, 8문항) 기반 **Execution Accuracy(EX) custom code-based evaluator**. 스팬에서 (질문, 생성 SQL)을 복원해 goldset 과 매칭하고, gold SQL·생성 SQL 을 read-only(agent_ro)로 각각 실행해 정규화 결과셋을 비교합니다(LLM judge 비용 없음). ※ 이 evaluator 만 Lambda 로 구현 — AgentCore Evaluations 의 code-based evaluator 서비스 규격이며, "Tool layer Lambda 금지" 제약의 명시적 예외입니다.
- **Online evaluation**: orchestrator 트레이스를 상시 샘플링 평가하는 `OnlineEvaluationConfig`(builtin Correctness·ToolSelectionAccuracy + EX). admin panel 의 **평가·개선** 화면에서 배치 평가 실행(`StartBatchEvaluation`)·결과 리뷰·개선 추천(`StartRecommendation` — 시스템 프롬프트/도구 설명)을 수행합니다.
- **Configuration Bundle 승격 (A/B 폴백)**: 프롬프트·모델 설정을 불변 버전 스냅샷(bundle)으로 관리하고, **승격 = SSM `/agentic-t2sql/active-bundle` 포인터 전환**입니다. orchestrator 가 60초 TTL 캐시로 활성 bundle 을 읽어 시스템 프롬프트/모델을 오버라이드하며, 실패·빈 값이면 코드 기본값으로 폴백합니다(재배포 없는 전환·롤백). ⚠️ **A/B 트래픽 분할은 조사 시점(2026-07) 안정 API 표면이 확인되지 않아 미구현** — 수동 전량 전환 + online eval 전후 비교로 대체하는 폴백입니다(ARCHITECTURE §8 리스크 완화).
- **Track B — semantic 지식 채굴**: orchestrator 가 실행 종료마다 남기는 구조화 로그(`t2sql_query_record` — 질문·SQL·상태·**version vector**{bundle, agent})를 `mine_candidates` 도구가 CloudWatch Logs 에서 읽어, 성공 질의는 **few-shot 후보**, 실패 질의의 반복 표현은 **용어 후보**로 DynamoDB 에 candidate 적재합니다(단일 쓰기 유지, 자동 반영 없음). M4 승인 큐로 유입되어 Manager 승인 시 published → 기존 Streams 동기화로 전파됩니다.
- **승인 큐 반려(rejected) 상태** (M4 이월 부채 해소): `rejected` 상태와 반려 사유(`rejection_reason`)가 추가되어 반려 이력이 남고, 채굴기는 rejected 를 포함한 기존 entity_id 를 재적재하지 않습니다(반려한 후보가 배치 재실행으로 되살아나지 않음). 반려된 항목도 재승인(publish) 가능합니다.

**M4에서 추가된 것**
- **Admin panel** (`admin/`, ECS Fargate + 전용 ALB): Cognito 로그인(USER_PASSWORD_AUTH) → `aws-jwt-verify` 검증 → `cognito:groups`로 화면·API 인가(Manager/Admin 분리, `iam/*`는 Admin 전용). 화면: semantic 큐레이션 · **승인 큐(candidate → publish)** · 데이터소스 등록/테스트/스키마 크롤 · Cognito 사용자·그룹 관리 + Cedar 정책 read-only 뷰 · 메트릭 요약/세션 트레이스 탐색기.
- **datasource-admin-mcp** (`agents/datasource-admin-mcp/`, Runtime 호스팅 → Gateway 3번째 MCP target): semantic CRUD·publish/unpublish(`SemanticRepository` 재사용 — DynamoDB 단일 쓰기 유지), 데이터소스 등록(Secrets Manager `agentic-t2sql/datasource/*`)·연결 테스트·`information_schema` 크롤(candidate 적재).
- **사용자별 JWT On-Behalf-Of (M3 이월 부채 해소)**: admin panel API가 사용자의 AccessToken을 **그대로** Gateway MCP에 전달해 Cedar가 실제 사용자 그룹으로 인가합니다. orchestrator도 `forwardedProps.userAccessToken`(additive)이 오면 gateway 모드에서 서비스 계정 대신 사용자 토큰으로 도구를 호출합니다(없으면 기존 서비스 계정 위임 — 현 UI는 로그인 미구현이라 이 경로가 기본).
- **Cedar action 스코프 2-phase**: 광역 permit으로 gateway를 먼저 배포(phase 1, target 도구 동기화 게이트 통과) 후 `scripts/deploy.sh gateway-scoped`(phase 2)로 일반 사용자를 `run_sql`/`search_schema`만 허용하도록 좁힙니다 — admin 도구는 default-deny로 차단되고(Cedar가 tools/list에서 제외) Manager/Admin만 사용할 수 있습니다.

**M3에서 추가된 것**
- **AgentCore Gateway 단일 도구 평면**: 두 MCP 서버(Runtime 호스팅)를 Gateway MCP target으로 등록(아웃바운드 SigV4). semantic tool search 기본 활성. orchestrator는 `TOOL_PLANE_MODE=gateway`로 Gateway를 경유합니다(직접 연결 `direct`는 폴백).
- **Identity(인바운드 JWT)**: Gateway가 Cognito user pool JWT(customJWTAuthorizer)를 검증. orchestrator는 Cognito M2M(USER_PASSWORD_AUTH) 서비스 계정으로 토큰을 받아 위임합니다. 사용자별 JWT On-Behalf-Of 전파는 M4+ 범위입니다.
- **Cedar Policy ENFORCE**: PolicyEngine(default-deny + forbid-wins)을 Gateway에 연결. Manager/Admin 그룹 전체 허용, 일반 인증 사용자 허용, `Denied` 그룹 forbid(거부 검증용) 정책 3개.
- **Redshift Serverless(4 RPU)**: 두 번째 데이터 소스. `run_sql(sql, datasource="aurora"|"redshift")` 라우팅, Redshift Data API 비동기 폴링 실행기, redshift dialect AST 검증(UNLOAD/COPY 차단), read-only `agent_ro` 사용자.
- **READ-ONLY 4중 방어 완성**: ① Cedar default-deny ② SQLGlot AST allow-list(LLM 밖, 소스별 dialect) ③ 최소 권한 IAM ④ DB SELECT-only grant (Aurora·Redshift 동형).

**M2에서 추가된 것**
- **DynamoDB system-of-record** (`semantic-layer/`): 비즈니스 용어·few-shot·스키마 메타의 CRUD와 항목 단위 버전 관리(`v0` 최신본 + 이력), `candidate`/`published` 분리(미승인 지식은 에이전트에 노출되지 않음 — 지식 오염 방어선).
- **파생 저장소 동기화** (dual-write 금지): DynamoDB Streams → ① OSIS zero-ETL 파이프라인 → OpenSearch `t2sql-semantic` 인덱스(published만), ② Streams consumer Lambda → Neptune openCypher upsert(+DLQ).
- **Neptune Serverless 그래프**: `(Table)-[:JOINS]->(Table)`, `(Term)-[:MAPS_TO]->(Column)` — schema linking 시 join-path 순회(GraphRAG). semantic MCP 는 VPC 모드로 전환해 Neptune에 접근합니다.
- **clarification(재요청) E2E**: 모호한 질의 → Strands interrupt → AG-UI `CUSTOM(clarification_request)` 이벤트 → CopilotKit 폼(select/date-range/text) → 같은 세션 재호출로 중단 지점부터 재개.

설계 전체는 [`ARCHITECTURE.md`](./ARCHITECTURE.md), 구현 체크리스트는 [`docs/well-architected-checklist.md`](./docs/well-architected-checklist.md)를 참고하세요.

> **Preview 기능 상태 (M5 착수 시점 2026-07-27 재확인, us-west-2 라이브 검증)**:
> - **Evaluations**(custom evaluator·code-based Lambda·배치/온라인 평가): API 실존·동작 확인 → **네이티브 사용**.
> - **Configuration Bundle · Recommendations**: API 실존·응답 확인, 단 서비스 발표 기준 **Preview**(AgentCore Optimization) → 사용하되 admin 화면에 Preview 임을 명시하고 호출 실패는 안내로 graceful degrade.
> - **A/B Testing(Gateway 트래픽 분할)**: 안정 API 표면 미확인 → **미구현, 수동 bundle 전환 폴백**(SSM 포인터 + online eval 전후 비교).
> - **Agent Registry**: 범위 밖(미사용). 상세 판정 근거는 `docs/m2-m3-interface-contract.md` §9.0.

## 아키텍처 (5계층)

```
┌──────────────────────────────────────────────────────────────┐
│ [1] UI Layer — Next.js + CopilotKit (ECS Fargate + ALB)      │
│     브라우저 ↔ 서버사이드 프록시(SigV4) ↔ AgentCore Runtime   │
│     AG-UI SSE 이벤트(TEXT_MESSAGE_*/TOOL_CALL_*/STEP_*)        │
└───────────────┬──────────────────────────────────────────────┘
                │ POST /invocations (AG-UI, SigV4)
┌───────────────▼──────────────────────────────────────────────┐
│ [2] Orchestration — Strands Graph (AgentCore Runtime + Memory)│
│     intent → schema_linking → sql_generation → execution      │
│              → synthesis   (execution 실패 시 self-correction) │
└───────────────┬───────────────────────────┬──────────────────┘
                │ MCP (SigV4 streamable-http)│
┌───────────────▼───────────┐   ┌───────────▼──────────────────┐
│ [3] Tool — MCP servers     │   │ [4] Semantic layer            │
│  (AgentCore Runtime 호스팅)│   │  DynamoDB(원본, 버전·상태)     │
│  · sql-execution-mcp       │   │   └Streams→ OpenSearch(hybrid)│
│  · semantic-retrieval-mcp  │   │   └Streams→ Neptune(join path)│
│    (VPC 모드 — Neptune 접근)│   │  candidate/published 분리     │
└───────────────┬───────────┘   └───────────────────────────────┘
                │ RDS Data API (read-only 자격증명)
┌───────────────▼──────────────────────────────────────────────┐
│ [5] Data — Aurora PostgreSQL Serverless v2 (Data API)         │
│     (M3에서 Redshift Serverless 추가)                          │
└──────────────────────────────────────────────────────────────┘
```

- **UI**는 AgentCore를 직접 호출하지 않습니다. Fargate의 서버사이드 프록시(`/api/copilotkit`)가 SigV4 서명을 붙여 Runtime의 `/invocations`(SSE)로 전달합니다.
- **도구 노출 모델**: 모든 도구는 AgentCore Runtime에 MCP 서버로 호스팅됩니다(Lambda 미사용). M3에서 이들을 AgentCore Gateway의 MCP target으로 등록합니다.
- **오케스트레이터**는 결정적 Strands Graph로, 각 노드가 코드로 인코딩되어 테스트·버전 관리가 가능합니다.

## 디렉토리 구성

```
agentic-text-to-sql-on-aws/
├── infra/                         # CDK (TypeScript) — 6개 스택
│   ├── bin/agentic-t2sql.ts       # 앱 진입점 (base → semantic → runtime → gateway → ui/admin)
│   └── lib/
│       ├── base-stack.ts          # VPC/Aurora/Redshift Serverless/OpenSearch/ECR/Cognito/Memory/IAM
│       ├── semantic-stack.ts      # DynamoDB/Neptune Serverless/OSIS/graph-sync Lambda (M2)
│       ├── runtime-stack.ts       # AgentCore Runtime × 4 (orchestrator + MCP 3)
│       ├── gateway-stack.ts       # AgentCore Gateway + MCP target 3 + Cedar PolicyEngine (M3·M4)
│       ├── ui-stack.ts            # ECS Fargate + 퍼블릭 ALB
│       ├── admin-stack.ts         # admin panel — ECS Fargate + 전용 ALB (M4)
│       ├── evaluation-stack.ts    # EX evaluator Lambda + Evaluator + OnlineEvalConfig + SSM bundle 포인터 (M5)
│       └── config.ts              # cdk.json context 기반 공통 설정
├── agents/
│   ├── orchestrator/              # Strands Graph 오케스트레이터 (AG-UI, 포트 8080, clarification 포함)
│   ├── sql-execution-mcp/         # SQL 검증(SQLGlot AST)·실행(Data API) MCP (포트 8000)
│   ├── semantic-retrieval-mcp/    # 스키마+용어+few-shot hybrid 검색 · Neptune join-path MCP (포트 8000)
│   └── datasource-admin-mcp/      # semantic 큐레이션·승인 + 데이터소스 관리 MCP (M4, Manager/Admin 전용)
├── semantic-layer/                # DynamoDB CRUD·버전 관리(candidate/published/rejected) · seed · Streams→Neptune Lambda (M2·M5, uv)
├── evaluation/                    # Execution Accuracy(EX) code-based evaluator Lambda + goldset (M5, uv)
├── ui/                            # Next.js + CopilotKit (AG-UI 프록시 · clarification 폼)
├── admin/                         # admin panel — Next.js web + API (M4, Cognito 인증·JWT OBO)
├── sample-data/                   # 결정적 샘플 데이터 생성기 + seed/인덱싱 스크립트 (uv)
├── scripts/                       # 배포·seed·E2E·cleanup 스크립트
├── docs/                          # 아키텍처 리뷰 다이어그램 · WA 체크리스트 · 인터페이스 기록
├── ARCHITECTURE.md                # 설계 확정본 (단일 진실 원천)
└── README.md
```

## 사전 준비

- **AWS 계정**과 배포 권한 (VPC/RDS/OpenSearch/ECS/ECR/IAM/Cognito/Secrets Manager/Bedrock AgentCore)
- 리전 **us-west-2** (오레곤). Aurora 엔진 버전은 리전별 가용 버전을 반드시 확인하세요(아래 주의 참고)
- **Amazon Bedrock 모델 액세스** 활성화 (us-west-2):
  - Anthropic Claude Sonnet (inference profile, `cdk.json`의 `modelId`)
  - Amazon Titan Text Embeddings V2 (`amazon.titan-embed-text-v2:0`)
- **Node.js 20 이상**, **[uv](https://docs.astral.sh/uv/)**, **AWS CLI v2**, **jq**
- **컨테이너 빌더**: Docker(데몬 실행) 또는 [Finch](https://runfinch.com/) — `scripts/build-and-push.sh`가 Docker 데몬을 감지해 없으면 Finch로 자동 폴백합니다. ARM64(Graviton) 이미지를 빌드합니다.
- **CDK Bootstrap** 완료: `cd infra && npm install && npx cdk bootstrap aws://<ACCOUNT_ID>/us-west-2`

## 실행 순서

스택은 base → semantic → (이미지 push · seed) → runtime → gateway → ui 순으로 의존합니다. 아래 스크립트가 순서를 캡슐화합니다.

```bash
# 0) 인프라 의존성 설치
cd infra && npm install && cd ..

# 1) Base 스택 배포 (VPC/Aurora/OpenSearch/ECR/Cognito/Memory/IAM) — 20~30분
#    ⚠️ IAM·NAT 생성 승인 프롬프트가 뜹니다. 내용 확인 후 진행하세요.
scripts/deploy.sh base
#    → infra/base-outputs.json 생성

# 2) Semantic 스택 배포 (DynamoDB/Neptune Serverless/OSIS/graph-sync Lambda) — 15~20분
scripts/deploy.sh semantic
#    → infra/semantic-outputs.json 생성

# 3) 컨테이너 이미지 3개 빌드·푸시 (ARM64, docker→finch 자동 폴백)
scripts/build-and-push.sh orchestrator sql-execution-mcp semantic-retrieval-mcp

# 4) 샘플 데이터 + semantic layer seed (멱등, outputs 기반)
scripts/seed.sh
#    Aurora 데이터 적재 → OpenSearch 스키마 인덱싱 → DynamoDB semantic seed
#    (semantic 인덱스를 kNN 매핑으로 선생성 후 Streams 로 OpenSearch·Neptune 전파)

# 5) Runtime 스택 배포 (AgentCore Runtime × 3 — semantic MCP 는 VPC 모드)
scripts/deploy.sh runtime
#    → infra/runtime-outputs.json 생성

# 6) Gateway 스택 배포 (Gateway + MCP target 2 + Cedar PolicyEngine ENFORCE)
scripts/deploy.sh gateway
#    → infra/gateway-outputs.json 생성 (GatewayUrl 출력)
#    이후 orchestrator 를 gateway 모드로 전환하려면 update-agent-runtime 으로
#    TOOL_PLANE_MODE=gateway + GATEWAY_URL + COGNITO_* env 를 주입합니다 (README 아래 참고).

# 7) UI 이미지 빌드·푸시 후 UI 스택 배포
scripts/build-and-push.sh ui
scripts/deploy.sh ui
#    → infra/ui-outputs.json 생성 (AlbUrl 출력)

# 8) [M4] datasource-admin-mcp 이미지 push → runtime 재배포(admin runtime 추가) →
#    gateway 재배포(admin target 추가) → Cedar phase 2(action 스코프) → admin panel 배포
scripts/build-and-push.sh datasource-admin-mcp
scripts/deploy.sh runtime
scripts/deploy.sh gateway          # phase 1: admin target 등록 (광역 permit 유지)
scripts/deploy.sh gateway-scoped   # phase 2: 일반 사용자 run_sql/search_schema 만 허용
scripts/build-and-push.sh admin-web
scripts/deploy.sh admin
#    → infra/admin-outputs.json 생성 (AdminAlbUrl 출력)

# 9) [M5] Evaluation 스택 배포 (EX evaluator Lambda + Evaluator + OnlineEvalConfig
#    + SSM active-bundle 포인터) — admin 스택이 이 출력을 env 로 소비하므로 admin 전에
#    배포하는 것이 정순입니다(이미 admin 을 배포했다면 admin 재배포로 env 반영).
scripts/deploy.sh evaluation
#    → infra/evaluation-outputs.json 생성

# 10) E2E 스모크 테스트 (레벨1~7: MCP · orchestrator SSE · UI · clarification ·
#    Gateway/Cedar/Redshift · admin panel 큐레이션·승인·OBO · M5 개선 파이프라인)
#    레벨5~7은 Cognito 테스트 사용자가 필요합니다 — 아래 스크립트가 멱등 생성합니다
#    (e2e-user / e2e-denied(Denied) / e2e-manager(Manager), 비밀번호는
#     agentic-t2sql/e2e/user-password 시크릿).
scripts/create-e2e-users.sh
scripts/e2e-smoke.sh
```

> ⚠️ **Aurora 엔진 버전 주의**: us-west-2는 특정 시점에 `aurora-postgresql` 16.6을 제공하지 않을 수 있습니다(가용 버전 예: 16.8/16.9/16.10/16.11/16.13/17.x). 배포 실패 시 `aws rds describe-db-engine-versions --engine aurora-postgresql --region us-west-2`로 가용 버전을 확인하고 `infra/lib/base-stack.ts`의 `AuroraPostgresEngineVersion`을 조정하세요.

각 컴포넌트의 로컬 개발·테스트 방법은 해당 디렉토리 README를 참고하세요
([orchestrator](./agents/orchestrator/README.md) · [sql-execution-mcp](./agents/sql-execution-mcp/README.md) · [semantic-retrieval-mcp](./agents/semantic-retrieval-mcp/README.md) · [datasource-admin-mcp](./agents/datasource-admin-mcp/README.md) · [sample-data](./sample-data/README.md) · [ui](./ui/README.md) · [admin](./admin/README.md)).

**admin panel 사용**: `infra/admin-outputs.json`의 `AdminAlbUrl`로 접속해 Manager 또는 Admin 그룹 사용자로 로그인합니다(데모용 계정은 `scripts/create-e2e-users.sh`가 만드는 `e2e-manager@example.com`). 용어를 candidate로 등록 → 승인 큐에서 publish → 수 초 내 OpenSearch/Neptune에 전파되어 에이전트 질의에 반영됩니다. 승인 큐에서 **반려**하면 rejected 상태와 사유가 이력으로 남고, 반려 목록에서 재승인할 수 있습니다.

**개선 파이프라인 사용 (M5)**: admin panel 의 **평가·개선** 화면에서 ① "후보 채굴 실행"으로 최근 운영 질의에서 few-shot/용어 후보를 채굴(→ 승인 큐 유입), ② 배치 평가를 실행해 EX·Correctness 스코어를 리뷰, ③ 개선 추천(시스템 프롬프트/도구 설명)을 생성해 bundle 새 버전으로 반영, ④ 원하는 bundle 버전을 **승격**하면 orchestrator 가 약 60초 내(캐시 TTL) 새 프롬프트·모델로 전환됩니다. 롤백은 이전 버전을 다시 승격하면 됩니다.

## 보안 참고 사항

- **READ-ONLY 다층 방어**: Data API는 DML/DDL도 실행 가능하므로 애플리케이션 레벨 강제가 필수입니다. M1은 3중 방어를 구현합니다.
  1. **SQL AST validator** (SQLGlot allow-list) — LLM 밖 결정적 검증. SELECT/WITH만, 단일 statement, 시스템 카탈로그·위험 함수 차단, 자동 LIMIT 주입. (`sql-execution-mcp/validation.py`)
  2. **read-only IAM 역할** — MCP 실행 역할은 특정 클러스터 ARN에 대한 `rds-data` 실행 권한과 `agent_ro` 시크릿 읽기만 부여.
  3. **DB SELECT-only grant** — `agent_ro` DB 역할은 SELECT 권한만 보유(seed가 부여). 쓰기 권한 없음.
  - Cedar default-deny policy는 **M3**에서 추가되어 4중 방어가 완성됩니다.
- **컴포넌트별 최소 권한 IAM 역할 분리**: orchestrator / sql-mcp / semantic-mcp / ui가 각각 별도 역할을 사용하며, 각 역할은 자신에게 필요한 액션만 특정 리소스 ARN에 한정해 보유합니다.
- **시크릿 관리**: DB 자격증명은 AWS Secrets Manager에만 저장하고 LLM 컨텍스트에 노출하지 않습니다. 저장소에는 `.env.example`(placeholder)만 커밋하며 실물 `.env`는 `.gitignore`로 제외됩니다.
- **거부 쿼리 감사 로깅**: AST validator가 거부한 SQL은 전수 로깅됩니다.
- **M1의 한계 (프로덕션 전 보완 필요)**:
  - ALB가 **HTTP**로 노출됩니다(UI·admin panel 모두). 프로덕션은 ACM 인증서 + HTTPS(TLS) 리스너로 전환하세요.
  - **채팅 UI는 인증이 강제되지 않습니다.** UI 프록시(`/api/copilotkit`)에 Cognito JWT 검증 훅 자리만 마련돼 있습니다. admin panel(M4)은 Cognito 로그인 + JWT 검증 + 그룹 인가가 강제되며, 사용자 토큰이 Gateway MCP까지 OBO로 전파됩니다. orchestrator 경로의 사용자 토큰 전파는 additive로 구현돼 있으나(`forwardedProps.userAccessToken`) UI 로그인이 없어 기본은 서비스 계정 위임입니다.
  - Online evaluation 샘플링이 데모 편의상 100%입니다 — 운영에서는 judge 모델 비용에 맞춰 낮추세요(`infra/lib/config.ts` `onlineEvalSamplingPercentage`).

## 리소스 정리 (cleanup)

배포된 리소스(Aurora·OpenSearch·NAT GW·ALB·ECR·Runtime 등)는 시간당 과금됩니다. 사용 후 정리하세요.

```bash
# 전체 스택 삭제 (역순: Admin → UI → Gateway → Runtime → Semantic → Base). 확인 프롬프트 후 진행.
scripts/cleanup.sh
#    CI 등 비대화형에서는: scripts/cleanup.sh --yes
```

- ECR 리포지토리는 `emptyOnDelete`로 이미지째 삭제됩니다.
- 스택 밖에서 생성된 시크릿(E2E 비밀번호, admin panel이 등록한 `agentic-t2sql/datasource/*`)도 cleanup 스크립트가 함께 삭제합니다.
- **CloudWatch 로그 그룹은 수동 삭제가 필요합니다**: `/aws/bedrock-agentcore/runtimes/agentic_t2sql_*`, ECS 태스크 로그 그룹(`AgenticT2SqlUiStack-TaskDef*`, `AgenticT2SqlAdminStack-TaskDef*`). 보존 정책에 따라 남을 수 있으니 필요 시 콘솔/CLI로 삭제하세요.
- 삭제 후 `aws cloudformation list-stacks --region us-west-2`로 `AgenticT2Sql*` 스택이 모두 `DELETE_COMPLETE`인지 확인하세요.

## 월 예상 비용

us-west-2 기준, 상시 가동 가정의 대략적 추정입니다(실제는 사용량·데이터 전송에 따라 달라집니다).

| 리소스 | 사양 | 월 추정 |
|---|---|---|
| NAT Gateway | 1개 | ~$32 + 데이터 처리 |
| OpenSearch (관리형) | t3.small.search × 1 + 10GB gp3 | ~$27 |
| Aurora Serverless v2 | 0.5~2 ACU (유휴 시 스케일다운) | ~$43 (최소 상시) |
| Redshift Serverless | 4 RPU (사용량 기반) | 유휴 시 거의 $0 |
| Neptune Serverless | 1~2.5 NCU | ~$90 (최소 상시) |
| OSIS 파이프라인 | 1 OCU | ~$175 |
| ECS Fargate (UI + admin) | 0.25/0.5 vCPU × 2 서비스 | ~$27 |
| Application Load Balancer | 2개 (UI·admin) | ~$32 + LCU |
| AgentCore Runtime × 4 | consumption 기반 (호출량 비례) | 유휴 시 거의 $0 |
| **합계** | | **~$430–470/월** (유휴 최적화 시 더 낮음) |

> 비용을 최소화하려면 사용하지 않을 때 `scripts/cleanup.sh`로 정리하세요. Aurora Serverless v2와 AgentCore Runtime은 유휴 시 비용이 크게 낮아집니다.

## 라이선스

이 프로젝트는 저장소 루트의 [LICENSE](../../LICENSE)(MIT-0)를 따릅니다.
