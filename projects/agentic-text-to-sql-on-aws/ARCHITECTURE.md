# agentic-text-to-sql-on-aws — 아키텍처 설계 문서

> 2026-07-26 기준 설계 확정본. 리서치(AgentCore 서비스 카탈로그 / Text-to-SQL 패턴 / UI 스트리밍 표준 / 레포 컨벤션)와
> 동료 검토(Codex) 결과를 반영했다. 구현 시점에 Preview → GA 상태 변화를 반드시 재확인할 것.

## 1. 개요

Amazon Bedrock AgentCore 기반의 프로덕션급 agentic Text-to-SQL 솔루션.
사용자는 자연어로 데이터를 질의하고, 에이전트는 semantic layer의 비즈니스 컨텍스트를 활용해
정확하고 안전한 SQL을 생성·실행·분석한다. 운영자는 admin panel에서 데이터 소스, semantic 메타데이터,
권한, 평가/개선 파이프라인을 관리한다.

### 페르소나 (3명)

| 페르소나 | 한국어 명칭 | 역할 |
|---|---|---|
| **Admin** | 관리자 | 솔루션 배포, 인프라 운영, 데이터 소스 연결, 사용자/권한 관리, 상태 모니터링·디버깅 |
| **Manager** | 매니저 | semantic 메타데이터 큐레이션(비즈니스 용어, 동의어, 관계, few-shot 예시), 평가 결과 리뷰·프롬프트 개선 승인 |
| **User** | 사용자 | 자연어 질의, 결과·시각화 확인, 재요청(clarification) 응답 |

Admin과 Manager는 admin panel을 공유하되 Cognito group + 화면 권한으로 분리한다.

## 2. 확정된 핵심 결정

| # | 결정 사항 | 선택 | 근거 |
|---|---|---|---|
| D1 | IaC | **CDK (TypeScript)** | aws-samples 표준, AgentCore alpha L2 constructs(Runtime/Memory/Gateway/Evaluator/Policy) 존재, ECS·OpenSearch·Neptune·Cognito를 단일 스택 체계로 관리 |
| D2 | Neptune 도입 시점 | **Day-1 포함** | AWS 공식 text-to-SQL 레퍼런스(2026-04 ML Blog)의 GraphRAG(Neptune 그래프 순회 + OpenSearch 벡터) 패턴 채택. 용어 관계·join path의 multi-hop 해석 담당 |
| D3 | 프로젝트 성격 | **코어 우선 풀 구현** | 5계층 + admin panel + 평가 파이프라인 전체를 배포 가능한 형태로 구현. 마일스톤은 내부 단계로만 사용 |
| D4 | Phase 1 데이터 소스 | **Aurora PostgreSQL + Redshift Serverless** | 둘 다 Data API 기반(드라이버·커넥션 풀 불필요). 셀프 매니지드 MySQL/PostgreSQL 직접 연결은 후속 확장 |
| D5 | 프론트엔드 | **AG-UI protocol + CopilotKit** | AgentCore Runtime의 AG-UI 네이티브 지원(SSE `/invocations`), interrupt 이벤트로 clarification 폼 렌더링이 프로토콜 레벨에서 해결 |
| D6 | Long-term memory 위치 | **Orchestration layer 소속 (개인화 레이어)** | long-term memory = 사용자별 동적·경험적 지식(선호, 과거 쿼리 패턴). semantic layer = 조직 공유·정적 도메인 지식. 상호보완이며 병합하지 않음 |
| D7 | 도구 노출 모델 | **Runtime 호스팅 MCP 서버만 (Lambda 미사용)** | 모든 도구는 AgentCore Runtime에 MCP 서버로 호스팅 후 Gateway MCP target 등록. 예외: Evaluations code-based evaluator는 서비스 규격상 Lambda |
| D8 | 리전/브랜치 | **us-west-2** / `feature/agentic-text-to-sql` | 사용자 지정 |
| D9 | Runtime 배포 방식 | **컨테이너(ECR) 방식** | 에이전트·MCP 서버 모두 Dockerfile 기반 ARM64 이미지를 ECR에 푸시해 배포. 로컬 빌드는 docker 기본, 문제 시 **finch** 폴백 |

## 3. 전체 아키텍처

```
┌─────────────────────────────────────────────────────────────────────┐
│ Part 3. Admin Panel (ECS Fargate: Web + API)                        │
│  데이터소스 등록 │ semantic 큐레이션 │ 권한 관리 │ 대시보드 │ 디버깅   │
└──────────┬──────────────────────────────────────────────────────────┘
           │ 관리                                    Part 2. 개선 파이프라인
┌──────────▼──────────────────────────────┐   ┌──────────────────────────┐
│ Part 1. Core (5 Layers)                 │   │ OTEL → CloudWatch GenAI  │
│                                         │   │ Evaluations (custom EX·  │
│ [1] UI: ECS Fargate + CopilotKit        │──▶│  trajectory·LLM-judge)   │
│     AG-UI(SSE) ↔ AgentCore Runtime      │   │ Online eval → Insights   │
│ [2] Orchestration: Strands Graph        │   │ → Recommendations        │
│     on AgentCore Runtime + Memory       │◀──│ → Configuration Bundle   │
│ [3] Tool: AgentCore Gateway(MCP)        │   │ → A/B (Gateway 분할)     │
│     + Identity + Policy(Cedar)          │   └──────────────────────────┘
│ [4] Semantic: OpenSearch + Neptune      │
│     + DynamoDB                          │
│ [5] Data: Aurora PG + Redshift          │
│     (Data API, read-only)               │
└─────────────────────────────────────────┘
```

## 4. 레이어별 설계

### 4.1 UI Layer

- **호스팅**: Next.js 웹앱, ECS Fargate + ALB.
- **프로토콜**: AG-UI. 브라우저는 AgentCore를 직접 호출하지 않는다(SigV4/OAuth 불가).
  Fargate 내 서버 사이드(CopilotKit Runtime)가 Cognito 토큰을 검증·전달하는 프록시 역할.
- **스트리밍**: AgentCore Runtime의 AG-UI 네이티브 지원 사용
  (`bedrock-agentcore[ag-ui]`의 `AGUIApp` 엔트리포인트, SSE `/invocations`).
  `TEXT_MESSAGE_*`(텍스트 델타), `TOOL_CALL_*`(도구 진행 표시), `STATE_SNAPSHOT/DELTA`(멀티 에이전트 상태) 이벤트 렌더링.
- **재요청(clarification) 흐름** — 핵심 요구사항:
  1. 에이전트가 정보 부족 인지 → Strands `context.interrupt({name, reason: {폼 스키마}})` 호출로 일시 정지
  2. AG-UI interrupt 이벤트(ui 스펙 + fields)가 SSE로 전달
  3. CopilotKit이 date-range picker 등 인터랙티브 폼 렌더링
  4. 사용자 응답 → 동일 `runtimeSessionId`로 재호출, interrupt id에 keyed된 응답 전달
  5. session manager가 상태 복원 → 중단 지점부터 재개
- ⚠️ **Codex 검토 반영**: `runtimeSessionId`는 동일 microVM으로의 라우팅 affinity만 보장하며,
  중단 지점의 워크플로 상태를 자동 복원하지 않는다. **`AgentCoreMemorySessionManager`(또는 S3SessionManager)로
  대화·interrupt 상태를 명시적으로 영속화**하는 것이 재개 정확성의 필수 조건이다.
- ⚠️ Strands AG-UI 통합 패키지는 community-maintained. 구현 초기에 통합 테스트로 검증하고,
  실패 시 폴백은 raw SSE + 자체 이벤트 매핑.

### 4.2 Orchestration Layer

- **프레임워크**: Strands Agents SDK, AgentCore Runtime 배포.
- **배포 방식 (D9)**: **컨테이너 방식** — Dockerfile로 ARM64 이미지 빌드 → ECR 푸시 → Runtime이 ECR 이미지 참조.
  direct code upload는 사용하지 않는다. 로컬 빌드는 docker, 실패 시 finch(`finch build`) 폴백.
  MCP 서버들(§4.3)도 동일한 컨테이너 파이프라인 공유.
- **패턴**: Strands **Graph** — 결정적·감사 가능한 골격. 노드 전이가 코드로 인코딩되어 테스트·버전 관리 가능.

```
intent 분석/명확화 ──(모호)──▶ interrupt(사용자 재요청)
        │
        ▼
schema linking (semantic layer 검색: OpenSearch hybrid + Neptune 순회)
        ▼
SQL 생성 (few-shot 예시 + 비즈니스 용어 컨텍스트 주입)
        ▼
AST 검증 (SQLGlot allow-list) ──(실패)──▶ self-correction 루프 (최대 N회)
        ▼
실행 (Gateway 경유 SQL 실행 도구)  ──(오류)──▶ self-correction 루프
        ▼
결과 분석/시각화 (AgentCore Code Interpreter: pandas 집계·차트)
        ▼
자연어 내러티브 합성
```

- **Memory**:
  - Short-term: 대화 히스토리(멀티턴, "그럼 작년은?" 류 후속 질문)
  - Long-term: User Preferences 전략(선호 차트·집계 방식) + custom 전략(사용자별 용어 사용 패턴, 자주 쓰는 테이블).
    namespace는 `/strategy/{memoryStrategyId}/actor/{actorId}/...` 계층으로 사용자 격리.

### 4.3 Tool Layer

- **도구 노출 모델 (확정)**: **Lambda를 사용하지 않는다.** 모든 도구는
  **AgentCore Runtime에 MCP 서버로 호스팅**(MCP protocol, port 8000, `/mcp`)하고,
  각 MCP 서버를 **AgentCore Gateway의 MCP target**으로 등록한다.
  Gateway는 인증/인가·도구 집약(aggregation)·semantic tool search를 담당하는 단일 도구 평면(tool plane)이다.
  - MCP 서버 구성 (Runtime 호스팅):
    - `sql-execution-mcp`: SQL 검증(AST allow-list)·실행(Data API)·결과 반환
    - `semantic-retrieval-mcp`: 스키마/용어/동의어/few-shot 검색 (OpenSearch hybrid + Neptune 순회)
    - `datasource-admin-mcp`: 데이터 소스 메타 조회 등 관리용 도구 (Manager/Admin 전용)
  - Runtime의 MCP 호스팅 모드는 에이전트 호스팅과 동일한 배포 체계(microVM 세션 격리, consumption 과금)를 공유.
- **Identity**: 인바운드 = Cognito JWT (사용자 신원 전파 → row-level 정책의 근거),
  아웃바운드 = token vault + Secrets Manager (DB 자격증명이 LLM 컨텍스트에 노출되지 않도록).
- **Policy (Cedar)**: default-deny. Cognito group(페르소나) 기반으로
  `principal(role) → action(tool) → resource(gateway)` 접근 제한. 예: User는 SELECT 실행 도구만,
  Manager는 메타데이터 갱신 도구까지.
- **Registry** (Preview): 도구·에이전트를 조직 카탈로그에 등록. Phase 후반 선택 적용.

### 4.4 Semantic Layer

AWS 공식 GraphRAG 레퍼런스 패턴 + WrenAI MDL의 "코드로 버전 관리되는 계약" 사상을 결합.

| 저장소 | 역할 |
|---|---|
| **DynamoDB** | system-of-record. 비즈니스 용어, 동의어, 메트릭 정의, few-shot NLQ↔SQL 쌍의 CRUD·버전 관리 (admin panel이 쓰기) |
| **OpenSearch** | hybrid(vector+BM25) 검색 인덱스. 스키마 메타데이터·동의어·예시를 임베딩, 질의 시 관련 컨텍스트만 프롬프트 주입 (DynamoDB → 동기화) |
| **Neptune** | 그래프: 테이블-컬럼-용어-메트릭 관계, join path, 엔티티 계층. multi-hop 질의("최근 사용자의 지역별 구매액" → 용어 해석 + join 경로 도출) 시 그래프 순회 |

- **동기화 흐름 (polyglot persistence + CQRS 파생 뷰 패턴)**:
  admin panel 쓰기 → DynamoDB(원본, 조건부 쓰기·버전 이력·감사 추적)
  → DynamoDB Streams → ① OpenSearch Ingestion zero-ETL(관리형) → OpenSearch 인덱싱,
  ② Streams consumer → Neptune upsert (실패 시 DLQ → 재처리).
  - 쓰기는 DynamoDB 한 곳만(dual-write 금지 — 부분 실패 시 영구 불일치 방지).
    파생 인덱스는 언제든 원본에서 전체 재구축(backfill) 가능.
  - `status: candidate` 레코드는 동기화하지 않고 `published`만 인덱스에 반영 (§5.2 참조).
  - 최종 일관성(수 초 지연)은 큐레이션 워크플로 특성상 허용. admin UI에 동기화 상태 표시.
- semantic 정의는 MDL식 YAML 스키마로 export/import 가능하게 하여 리뷰·버전 관리를 지원.
- 예시 시나리오: Manager가 "'최근 사용자' = 최근 3개월 활동 사용자" 용어를 등록하면
  DynamoDB에 `{name, definition, synonyms: ["액티브 유저", ...], sql_fragment, maps_to: [{table, column}]}` 저장 →
  OpenSearch에는 임베딩+키워드 문서로("요즘 들어온 유저" 같은 미등록 표현도 hybrid 검색으로 매칭),
  Neptune에는 `(Term)-MAPS_TO->(Column)`, `(Table)-JOINS{on}->(Table)` 엣지로 반영 →
  에이전트가 schema linking에서 용어 해석(OpenSearch)과 join 경로 도출(Neptune 순회)을 각각 수행해
  `WHERE last_login_at >= CURRENT_DATE - INTERVAL '3 months'` + 올바른 join으로 해석.

### 4.5 Data Layer

- **Aurora PostgreSQL** (RDS Data API) + **Redshift Serverless** (Redshift Data API). 커넥션 풀 불필요, IAM 기반.
- 자격증명: Secrets Manager, **read-only DB 사용자** 강제.
- **다층 SQL 안전장치** (Data API는 DML/DDL도 실행 가능하므로 애플리케이션 레벨 강제가 필수):
  1. SQLGlot AST allow-list — SELECT/WITH만, 단일 statement, dialect 명시, 시스템 카탈로그 차단
  2. 자동 LIMIT 주입, query timeout, scan 상한
  3. read-only 자격증명 (최후 방어선)
  4. 거부된 쿼리 전수 감사 로깅
- 샘플 데이터: seed 고정 생성기 제공 (레포 컨벤션).

## 5. Part 2 — 점진적 개선 파이프라인

개선 루프의 산출물은 **두 트랙**이다. 에이전트의 행동을 바꾸는 것(Track A)과
에이전트가 참조하는 지식을 보강하는 것(Track B). 두 트랙 모두 Manager 승인을 거친다.

### 5.1 Track A — 에이전트 개선 (AgentCore Optimization 네이티브)

```
OTEL 트레이스 → CloudWatch GenAI Observability 대시보드
     │
     ▼
AgentCore Evaluations
  ├─ custom code-based evaluator: Execution Accuracy — 생성 SQL을 read-only 실행,
  │    gold SQL 결과셋과 정규화 비교 (judge 비용 없음)
  │    ※ AgentCore Evaluations의 code-based evaluator는 서비스 규격상 Lambda로 구현됨
  │      (tool layer의 "Lambda 미사용" 결정과 무관한 Evaluations 서비스 요구사항)
  ├─ trajectory evaluator: 도구 시퀀스 준수(schema-link→generate→validate→execute)
  ├─ LLM-as-judge: 내러티브 품질·안전성
  └─ Online evaluation: 라이브 트레이스 샘플링 상시 채점
     │
     ▼
Insights: failure/intent/trajectory 패턴 분석 (silent failure 탐지)
     ▼
Recommendations: system prompt·tool description 개선안 자동 생성
     ▼
Configuration Bundle: 개선안을 불변 버전 스냅샷으로 반영 (재배포 없이 적용, branch/diff)
     ▼
A/B Testing: Gateway 트래픽 분할, 통계적 유의성 확인 → Manager가 admin panel에서 승격 승인
     ▼
적용 대상: AgentCore Runtime(프롬프트·모델), Gateway(tool description)
```

### 5.2 Track B — Semantic layer 보강 (지식 채굴 루프)

semantic layer는 Manager 수동 입력만으로 성장하지 않는다. 운영 데이터에서
semantic 지식 후보를 채굴해 Manager 승인 큐로 보내는 자동 루프를 둔다.

```
소스 1. 실패 클러스터 (Insights/트레이스 분석)
  · schema linking이 매칭 실패한 사용자 표현 반복 등장
    → 동의어 후보 (예: "VIP 고객"이 어떤 용어와도 안 붙음 → 신규 용어/동의어 제안)
  · 특정 테이블 조합에서 join 오류 반복
    → Neptune join-path 엣지 보강 후보
     │
소스 2. 성공 사례 (online eval 고득점 트레이스)
  · EX 통과 + 사용자 긍정 피드백을 받은 (NLQ, SQL) 쌍
    → few-shot example 후보로 자동 수확
     │
소스 3. 사용자 상호작용
  · clarification 폼에서 사용자가 고른 해석 (예: "최근" → 3개월 선택이 우세)
    → 용어 기본값 조정 후보
  · 결과 화면 thumbs-up/down 피드백
     │
     ▼
후보 생성기 (트레이스 분석 배치): DynamoDB에 status: candidate 로 기록
  (candidate는 OpenSearch/Neptune에 동기화되지 않음 — 에이전트에 미노출)
     ▼
Manager 승인 큐 (admin panel): 후보 검토 → 승인 시 status: published
     ▼
§4.4 동기화 파이프라인 경유 OpenSearch·Neptune 반영 → 이후 질의부터 에이전트가 사용
```

- **자동 반영은 하지 않는다**: LLM이 채굴한 후보를 사람 검토 없이 semantic layer에
  넣으면 지식 오염(poisoning) 경로가 된다 (Well-Architected AGENTSEC01 — semantic layer는
  간접 prompt injection surface). candidate/published 분리가 이 방어선이다.
- few-shot 후보는 승인 전 자동 검증(gold 실행 재확인, PII 마스킹)을 거친다.
- 효과 측정: 승인된 semantic 변경 전후의 online eval 스코어 비교를 admin panel에 표시
  (semantic 변경도 Track A의 A/B와 동일하게 효과를 검증할 수 있다).

- 오프라인 평가셋: 자사(샘플) 스키마 기반 NLQ↔gold SQL 커스텀 세트. BIRD/Spider 2.0의 EX·VES 지표 개념 차용.
  Track B에서 수확·승인된 (NLQ, SQL) 쌍은 평가셋으로도 승격 가능 — 평가셋 자체도 점진 성장.
- ⚠️ **GA/Preview 상태 주의**: 조사 시점 기준 Optimization(Insights/Recommendations/Experiments)은 Preview.
  Policy·Evaluations는 출처 간 Preview/GA 표기가 상충(가격 페이지 Preview vs 검토자 GA 판단) —
  **구현 착수 시 최신 문서로 재확인**하고 README에 상태를 정직하게 표기할 것.

## 5.3 버저닝 전략 (횡단 관심사)

지속 개선 루프가 있는 시스템은 "무엇이 바뀌어서 좋아졌나/나빠졌나"를 항상 답할 수 있어야 한다.
독립적으로 진화하는 버전 축 6개를 정의하고, 모든 트레이스에 **version vector**를 스탬핑한다.

| 축 | 버저닝 방식 |
|---|---|
| ① Configuration Bundle | AgentCore 네이티브 (불변 스냅샷, branch/diff) |
| ② Semantic layer | 항목 단위 `VERSION#n`(DynamoDB) + **스냅샷**: 승인 이벤트마다 MDL YAML export를 S3 버전 저장(+git). 롤백 = 이전 YAML import |
| ③ 평가셋 | evalset 버전 필수. 스코어는 항상 "evalset vN에서 X%"로 병기, 버전 간 직접 비교 금지 (Track B로 성장하므로 착시 방지) |
| ④ Evaluator | 채점 로직·judge 프롬프트에 semantic version |
| ⑤ 에이전트 코드/컨테이너 | git + ECR 이미지 태그 + Runtime 버전 |
| ⑥ 데이터 소스 스키마 | 주기 크롤링으로 **schema fingerprint**(sha256) 갱신. 변경 감지 시 영향받는 semantic 항목 자동 `stale` 플래그 → Manager 승인 큐 (Track B의 4번째 소스) |

- **Version vector**: 각 질의 트레이스의 OTEL span attribute에
  `{bundle, semantic_snapshot, evalset, evaluator, agent, schema_fingerprint}` 기록.
  → 귀인(스코어 변화를 축별 분해), 재현(질의 시점의 세계 복원), 공정한 A/B(동일 조건 트래픽만 비교).
- **변경 규율**: 한 번에 한 축만. admin panel 승인 큐에서 "진행 중 A/B 실험이 있으면
  semantic 승인 시 경고/보류" 가드 적용.

## 6. Part 3 — Admin Panel

- **호스팅**: ECS Fargate (web app + API server), Cognito 인증, Admin/Manager group 분리.
- **기능**:
  - 데이터 소스 등록: 연결 설정 → Secrets Manager, 연결 테스트, 스키마 크롤링 → semantic layer 초기 적재
  - semantic 큐레이션: 용어/동의어/관계/few-shot 관리 (DynamoDB → OpenSearch·Neptune 동기화)
  - 권한 관리: 사용자·그룹(Cognito) + 도구 접근(Cedar policy) 관리
  - 상태 대시보드: CloudWatch 메트릭·online eval 스코어
  - 에이전트 디버깅: OTEL 트레이스 탐색기, 세션 인스펙터
  - 평가 관리: 평가 실행·결과 리뷰·configuration bundle 승격 승인
  - **semantic 후보 승인 큐**: Track B가 채굴한 용어/동의어/join-path/few-shot 후보를
    검토·승인/반려 (승인 시 candidate → published → 동기화 파이프라인 반영)

## 7. 구현 마일스톤 (내부 단계)

| 단계 | 범위 | 완료 기준 |
|---|---|---|
| M1 | 코어 파이프라인 E2E | CDK로 인프라 배포, Aurora 샘플 데이터에 자연어 질의 → SQL → 결과 스트리밍 (semantic layer 최소형: OpenSearch만) |
| M2 | Semantic layer 완성 + clarification | Neptune 그래프·DynamoDB CRUD·interrupt 기반 재요청 폼 E2E |
| M3 | Tool/보안 완성 | Gateway·Identity·Policy(Cedar)·Redshift 소스 추가·다층 SQL 가드레일 전체 |
| M4 | Admin panel | 데이터 소스 등록·semantic 큐레이션·권한 관리·디버깅 화면 |
| M5 | 개선 파이프라인 | Track A: custom evaluator·online eval·Insights→Recommendations→config bundle→A/B / Track B: semantic 후보 채굴→승인 큐→published 반영 |

## 8. 리스크 및 완화

| 리스크 | 완화 |
|---|---|
| Preview 기능 의존 (Optimization, Registry 등) | README에 상태 명시, M5로 후치, 각 기능에 폴백 서술 (예: A/B 없이 수동 bundle 전환) |
| Strands AG-UI 통합이 community-maintained | M1에서 통합 검증 스파이크 우선 수행. 폴백: raw SSE + 자체 이벤트 매핑 |
| interrupt 재개의 상태 영속성 | `AgentCoreMemorySessionManager` 명시적 사용, 재개 시나리오 통합 테스트 필수 |
| 도구 노출 모델 혼선 (Gateway target vs Runtime MCP) | §4.3 기준으로 단일화: Gateway가 유일한 도구 평면 |
| Neptune 운영 복잡도·비용 | CDK로 최소 인스턴스 구성, cleanup 문서 필수, semantic 검색 인터페이스를 저장소 중립으로 설계 |
| Data API의 DML/DDL 실행 가능성 | AST allow-list + read-only 자격증명 이중 방어, 거부 쿼리 감사 로깅 |

## 9. 설계·구현 원칙

- **Well-Architected 준수**: 기본 인프라는 기존 Well-Architected pillar를, 에이전트 설계는
  **Agentic AI Lens**([docs](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentic-ai-lens.html))를
  기준으로 설계·구현한다. 구현 체크리스트는 `docs/well-architected-checklist.md`로 별도 관리.
- **최소 권한 원칙**: 컴포넌트별 IAM role 분리(에이전트 실행 role ≠ MCP 서버 role ≠ admin API role),
  scoped-down policy, read-only DB 사용자, Cedar default-deny.
- **코드 스타일**: 복잡해지는 모듈은 OOP로 가독성 있게 구성 (예: 도구 검증기·데이터 소스 커넥터는
  추상 base class + 구현체 패턴).
- **기본 리전**: `us-west-2` (오레곤).
- 작업 브랜치: `feature/agentic-text-to-sql` (main에서 분기).

## 10. 향후 확장 — demo/ 실습 구조 (구현은 나중, 설계에 반영)

솔루션 배포 후 개발자가 시나리오별로 실습할 수 있는 `demo/` 폴더를 둔다.
각 데모는 Jupyter notebook으로 데이터·설정을 주입하며 번호 순으로 경험한다.

```
demo/
├── 00-improve-agent/        # 평가 → insight → recommendation → bundle 승격 실습
├── 01-setup-business-terms/ # 비즈니스 용어·동의어 등록이 SQL 정확도를 바꾸는 과정 실습
└── ...                      # (데이터 소스 추가, A/B 테스트 등 시나리오 확장)
```

- 각 notebook은 배포된 리소스(스택 output)를 읽어 동작하는 self-contained 실습이어야 한다.
- 코어 구현 시 admin API·semantic layer API를 notebook에서도 호출 가능하게 설계해 둔다 (인증 포함).

## 11. 레포 컨벤션 준수 사항

- 한국어 README 본문 (+ 필요 시 README.en.md), 루트 README 프로젝트 테이블에 행 추가
- 시크릿은 `.example` 파일 + .gitignore
- 보안 참고 섹션 + 리소스 정리(cleanup) 섹션 필수
- Python은 uv
- 이 프로젝트는 다른 sibling 프로젝트와 별개이며 그 선례를 따를 의무는 없다 (필요 시 참고만)
