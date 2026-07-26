# Agentic Text-to-SQL on AWS

Amazon Bedrock AgentCore 기반의 agentic Text-to-SQL 솔루션입니다. 사용자가 자연어로 데이터를 질의하면, 에이전트가 semantic layer의 비즈니스 컨텍스트를 활용해 안전한 SQL을 생성·검증·실행하고 결과를 자연어와 표로 요약합니다.

오케스트레이션은 **Strands Graph**(결정적 노드 전이 골격)로, 도구는 **AgentCore Runtime에 호스팅된 MCP 서버**로, 프론트엔드는 **AG-UI 프로토콜 + CopilotKit**으로 구성됩니다. IaC는 CDK(TypeScript)입니다.

> ⚠️ **데모/샘플 목적 전용입니다.** 이 코드는 학습과 참조를 위한 것으로, 프로덕션 사용 전에는 인증 강제(현재 미강제), HTTPS/TLS, 최소 권한 재점검, 비용·용량 재산정이 필요합니다. 실제 데이터·자격증명으로 실행할 때 유의하세요.

## 구현 범위 (M2)

이 저장소의 현재 상태는 **M2 — Semantic layer 완성 + clarification**입니다. 자연어 질의 → 스키마 링킹(비즈니스 용어·few-shot·join-path 포함) → SQL 생성 → AST 검증 → 실행 → 결과 스트리밍이 동작하며, 질의가 모호하면 에이전트가 인터랙티브 폼으로 되묻고 같은 세션에서 재개합니다. M3~M5는 로드맵입니다.

| 마일스톤 | 범위 | 상태 |
|---|---|---|
| **M1** | 코어 파이프라인 E2E (CDK 인프라 · Aurora 샘플 데이터 · OpenSearch 최소형 · AG-UI 통합) | ✅ 구현 |
| **M2** | Semantic layer 완성(Neptune·DynamoDB·Streams 동기화) + clarification(interrupt 재요청) | ✅ 구현 |
| M3 | Tool/보안 완성 (Gateway · Identity · Cedar policy · Redshift 소스 · 가드레일) | 🗺️ 로드맵 |
| M4 | Admin panel (데이터소스 등록 · semantic 큐레이션 · 권한 · 디버깅) | 🗺️ 로드맵 |
| M5 | 개선 파이프라인 (Track A: 평가→insight→bundle→A/B, Track B: semantic 지식 채굴) | 🗺️ 로드맵 |

**M2에서 추가된 것**
- **DynamoDB system-of-record** (`semantic-layer/`): 비즈니스 용어·few-shot·스키마 메타의 CRUD와 항목 단위 버전 관리(`v0` 최신본 + 이력), `candidate`/`published` 분리(미승인 지식은 에이전트에 노출되지 않음 — 지식 오염 방어선).
- **파생 저장소 동기화** (dual-write 금지): DynamoDB Streams → ① OSIS zero-ETL 파이프라인 → OpenSearch `t2sql-semantic` 인덱스(published만), ② Streams consumer Lambda → Neptune openCypher upsert(+DLQ).
- **Neptune Serverless 그래프**: `(Table)-[:JOINS]->(Table)`, `(Term)-[:MAPS_TO]->(Column)` — schema linking 시 join-path 순회(GraphRAG). semantic MCP 는 VPC 모드로 전환해 Neptune에 접근합니다.
- **clarification(재요청) E2E**: 모호한 질의 → Strands interrupt → AG-UI `CUSTOM(clarification_request)` 이벤트 → CopilotKit 폼(select/date-range/text) → 같은 세션 재호출로 중단 지점부터 재개.

설계 전체는 [`ARCHITECTURE.md`](./ARCHITECTURE.md), 구현 체크리스트는 [`docs/well-architected-checklist.md`](./docs/well-architected-checklist.md)를 참고하세요.

> **Preview 기능 주의**: AgentCore Optimization(Insights/Recommendations/Experiments)·Registry 등 일부 기능은 조사 시점(2026-07) 기준 Preview이며 M5 로 후치되어 있습니다. Policy·Evaluations는 출처 간 Preview/GA 표기가 상충하므로 구현 착수 시 최신 문서로 재확인이 필요합니다. M1 범위에는 Preview 의존 기능이 포함되지 않습니다.

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
├── infra/                         # CDK (TypeScript) — 4개 스택
│   ├── bin/agentic-t2sql.ts       # 앱 진입점 (base → semantic → runtime → ui)
│   └── lib/
│       ├── base-stack.ts          # VPC/Aurora/OpenSearch/ECR/Cognito/Memory/IAM
│       ├── semantic-stack.ts      # DynamoDB/Neptune Serverless/OSIS/graph-sync Lambda (M2)
│       ├── runtime-stack.ts       # AgentCore Runtime × 3 (orchestrator + MCP 2)
│       ├── ui-stack.ts            # ECS Fargate + 퍼블릭 ALB
│       └── config.ts              # cdk.json context 기반 공통 설정
├── agents/
│   ├── orchestrator/              # Strands Graph 오케스트레이터 (AG-UI, 포트 8080, clarification 포함)
│   ├── sql-execution-mcp/         # SQL 검증(SQLGlot AST)·실행(Data API) MCP (포트 8000)
│   └── semantic-retrieval-mcp/    # 스키마+용어+few-shot hybrid 검색 · Neptune join-path MCP (포트 8000)
├── semantic-layer/                # DynamoDB CRUD·버전 관리 · seed · Streams→Neptune Lambda (M2, uv)
├── ui/                            # Next.js + CopilotKit (AG-UI 프록시 · clarification 폼)
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

스택은 base → semantic → (이미지 push · seed) → runtime → ui 순으로 의존합니다. 아래 스크립트가 순서를 캡슐화합니다.

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

# 6) UI 이미지 빌드·푸시 후 UI 스택 배포
scripts/build-and-push.sh ui
scripts/deploy.sh ui
#    → infra/ui-outputs.json 생성 (AlbUrl 출력)

# 7) E2E 스모크 테스트 (MCP · orchestrator SSE · UI ALB · clarification/semantic 확장)
scripts/e2e-smoke.sh
```

> ⚠️ **Aurora 엔진 버전 주의**: us-west-2는 특정 시점에 `aurora-postgresql` 16.6을 제공하지 않을 수 있습니다(가용 버전 예: 16.8/16.9/16.10/16.11/16.13/17.x). 배포 실패 시 `aws rds describe-db-engine-versions --engine aurora-postgresql --region us-west-2`로 가용 버전을 확인하고 `infra/lib/base-stack.ts`의 `AuroraPostgresEngineVersion`을 조정하세요.

각 컴포넌트의 로컬 개발·테스트 방법은 해당 디렉토리 README를 참고하세요
([orchestrator](./agents/orchestrator/README.md) · [sql-execution-mcp](./agents/sql-execution-mcp/README.md) · [semantic-retrieval-mcp](./agents/semantic-retrieval-mcp/README.md) · [sample-data](./sample-data/README.md) · [ui](./ui/README.md)).

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
  - ALB가 **HTTP**로 노출됩니다. 프로덕션은 ACM 인증서 + HTTPS(TLS) 리스너로 전환하세요.
  - **인증이 강제되지 않습니다.** UI 프록시(`/api/copilotkit`)에 Cognito JWT 검증 훅 자리만 마련돼 있으며, 사용자 신원 전파·row-level 정책은 M3 범위입니다.
  - Cognito user pool·그룹(Admin/Manager)은 프로비저닝만 되어 있고 admin panel(M4)에서 활용됩니다.

## 리소스 정리 (cleanup)

배포된 리소스(Aurora·OpenSearch·NAT GW·ALB·ECR·Runtime 등)는 시간당 과금됩니다. 사용 후 정리하세요.

```bash
# 전체 스택 삭제 (역순: UI → Runtime → Base). 확인 프롬프트 후 진행.
scripts/cleanup.sh
#    CI 등 비대화형에서는: scripts/cleanup.sh --yes
```

- ECR 리포지토리는 `emptyOnDelete`로 이미지째 삭제됩니다.
- **CloudWatch 로그 그룹은 수동 삭제가 필요합니다**: `/aws/bedrock-agentcore/runtimes/agentic_t2sql_*`, ECS 태스크 로그 그룹(`AgenticT2SqlUiStack-TaskDef*`). 보존 정책에 따라 남을 수 있으니 필요 시 콘솔/CLI로 삭제하세요.
- 삭제 후 `aws cloudformation list-stacks --region us-west-2`로 `AgenticT2Sql*` 스택이 모두 `DELETE_COMPLETE`인지 확인하세요.

## 월 예상 비용

us-west-2 기준, 상시 가동 가정의 대략적 추정입니다(실제는 사용량·데이터 전송에 따라 달라집니다).

| 리소스 | 사양 | 월 추정 |
|---|---|---|
| NAT Gateway | 1개 | ~$32 + 데이터 처리 |
| OpenSearch (관리형) | t3.small.search × 1 + 10GB gp3 | ~$27 |
| Aurora Serverless v2 | 0.5~2 ACU (유휴 시 스케일다운) | ~$43 (최소 상시) |
| ECS Fargate (UI) | 0.25 vCPU / 0.5GB × 1 | ~$9 |
| Application Load Balancer | 1개 | ~$16 + LCU |
| AgentCore Runtime × 3 | consumption 기반 (호출량 비례) | 유휴 시 거의 $0 |
| **합계** | | **~$130–150/월** (유휴 최적화 시 더 낮음) |

> 비용을 최소화하려면 사용하지 않을 때 `scripts/cleanup.sh`로 정리하세요. Aurora Serverless v2와 AgentCore Runtime은 유휴 시 비용이 크게 낮아집니다.

## 라이선스

이 프로젝트는 저장소 루트의 [LICENSE](../../LICENSE)(MIT-0)를 따릅니다.
