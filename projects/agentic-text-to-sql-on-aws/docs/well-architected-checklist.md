# Well-Architected Agentic AI Lens — 구현 체크리스트

> AWS Well-Architected **Agentic AI Lens**(2026-06-10 공개) 기반의 text-to-SQL 솔루션 구현 체크리스트.
> 기준 문서: `https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/` (필러 6개 + focus area 43개 전수 확인)
> BP ID 체계: `AGENTOPS/AGENTSEC/AGENTREL/AGENTPERF/AGENTCOST/AGENTSUS` + `NN-BPNN`
>
> 구현을 변경할 때마다 해당 항목을 점검한다. ✅ 표시는 구현 완료 후 갱신.

## 최상위 우선 구현 항목 (착수 순서)

| 순위 | 항목 | 관련 BP | 상태 |
|---|---|---|---|
| 1 | **READ-ONLY 4중 심층방어**: Cedar default-deny + SQL AST validator + read-only IAM + DB SELECT-only grant | AGENTSEC02/03 | ✅ |
| 2 | **컴포넌트별 최소권한 IAM + permission boundary**: executor 역할만 Data API 실행 권한(특정 ARN 한정) | AGENTSEC03 | ✅ |
| 3 | **결정론적 SQL AST validator를 LLM 밖 툴 핸들러에** + Data API 에러 정규화(스키마 누출 방지) | AGENTSEC02-BP02 | ✅ |
| 4 | **계층적 HITL**: autonomous(저비용 SELECT) / notify(고비용 스캔) / approve(PII·admin 변경), Step Functions task-token + timeout | AGENTREL02-BP05, AGENTSEC04-BP02 | 후속 |
| 5 | **Guardrails 4지점 배치**: 사용자 입력·semantic 검색 결과·DB 결과 재유입·최종 출력 (prompt-attack block + PII 마스킹) | AGENTSEC08 | 후속 |
| 6 | **소비 상한/circuit breaker**: 세션 token cap·SQL 재시도 max·scan 상한, DynamoDB atomic counter | AGENTCOST07, AGENTREL07-BP02 | 🔶 부분 |
| 7 | **prompt + semantic 캐싱 + DDL 이벤트 invalidation** | AGENTPERF03, AGENTCOST02/04 | 후속 |
| 8 | **AgentCore Evaluations를 CI/CD promotion gate로** (gold NL→SQL→result 데이터셋) | AGENTOPS06 | 🔶 부분 (평가 파이프라인 ✅ — CodePipeline 게이트 연결은 후속) |
| 9 | **grounding + 실행 전 스키마 존재 검증 stage** (환각 SQL 감소) | AGENTREL05-BP03 | 🔶 부분 |
| 10 | **tamper-evident audit**: 생성 SQL·resolved entity·실행 identity 구조화 로깅 → S3 Object Lock + OTel 전구간 tracing | AGENTSEC05, AGENTOPS05 | 후속 |

---

## 1. 운영 우수성 (AGENTOPS)

- [ ] **AGENTOPS01-BP01/02** — 각 Strands 에이전트에 "job description" 정의: supervisor(intent 라우팅), specialist(schema-linking / SQL-generation / SQL-validation / result-explanation). autonomy 경계(예: SQL agent는 SELECT만) + 측정 가능한 성공 기준(SQL 실행 성공률, gold set 정확도, query cost 상한)
- [~] **AGENTOPS02** — 프롬프트/설정 lifecycle: 프롬프트를 코드에서 분리(Configuration Bundle), 버전/롤백, drift 감지 — 🔶 부분: **Configuration Bundle 분리·버전·롤백 구현** ✅ (bundle 불변 버전 스냅샷 + SSM `/agentic-t2sql/active-bundle` 포인터 승격/롤백, orchestrator 60s TTL 캐시 오버라이드·실패 시 코드 기본값 폴백, admin panel 승격 승인 UI, version vector `{bundle,agent}` 를 `t2sql_query_record` 에 스탬프 — docs/architecture.md §5.3 ①축). drift 감지는 후속
- [~] **AGENTOPS03** — AgentOps CI/CD: CodePipeline에 Evaluations promotion gate, 회귀 시 배포 차단·자동 롤백 — 🔶 부분: **평가 게이트 재료 완성** ✅ (goldset-v1 8문항 + EX code-based evaluator + `StartBatchEvaluation` admin API — CI 에서 배치 평가 실행·스코어 비교가 가능한 상태). CodePipeline 연결·자동 차단/롤백은 후속
- [x] **AGENTOPS04** — AgentCore Gateway = 권위 있는 tool catalog: 각 MCP 서버를 owner/version/param schema와 함께 등록, semantic discovery, 툴 fallback 정의 ✅ 구현: Gateway 가 유일한 도구 평면(semantic tool search 기본 활성). orchestrator 는 TOOL_PLANE_MODE=gateway 로 전환됨(direct 는 폴백). MCP target 3개(datasource-admin-mcp 추가 — 관리 도구도 동일 카탈로그·Cedar 인가 경로)
- [ ] **AGENTOPS05** — NL→schema-linking→SQL-gen→validation→실행→설명 전 구간 OTel span + W3C Trace Context 전파(UI가 inbound에서 trace header 주입), 구조화 JSON audit 로깅
- [x] **AGENTOPS06** — 다층 테스트 + 지속 평가(LLM-as-judge) + Manager 승인 워크플로우(admin panel) ✅ 구현: Manager 승인 워크플로우(승인 큐 publish + **rejected 반려 이력**) + **지속 평가** — online evaluation(트레이스 샘플링, builtin LLM-as-judge Correctness·ToolSelectionAccuracy + custom EX code-based evaluator) + admin panel 배치 평가 실행·결과 리뷰. 다층 테스트: 단위(pytest 380+)·E2E(레벨1~7). CodePipeline promotion gate 연결만 후속(AGENTOPS03)
- [ ] **AGENTOPS07** — 소비 상한: 세션 token cap, SQL 재시도 max, Data API scan 상한 → 초과 시 loop 중단. break-glass 런북(분석가 직접 read-only 접근 경로)을 시스템 밖에 문서화
- [ ] CloudFormation drift detection + AWS Config로 Runtime/Gateway/Cedar/Data API role 구성 drift 감지

## 2. 보안 (AGENTSEC)

- [x] **AGENTSEC03** — 컴포넌트별 IAM role 분리: orchestrator / sql-mcp / semantic-mcp / ui / gateway / OSIS / graph-sync Lambda 각각 별도 role. **sql-mcp만** rds-data(클러스터 ARN 한정)·redshift-data 실행 권한 보유 ✅ 구현 (redshift-data 액션은 리소스 조건 제약상 계정 스코프 — 알려진 완화)
- [x] **AGENTSEC02** — READ-ONLY 4중 방어: ① Cedar default-deny ② 툴 핸들러 내 결정론적 SQL AST validator(non-SELECT/DDL/DML/multi-statement/시스템 카탈로그/COPY·UNLOAD 거부 — LLM 밖에서) ③ read-only IAM ④ DB SELECT-only grant ✅ 구현: PolicyEngine ENFORCE(default-deny+forbid-wins) + postgres/redshift dialect AST validator(UNLOAD/COPY 차단 테스트) + Aurora·Redshift 각 agent_ro SELECT-only. E2E: Denied 그룹 차단·DELETE rejected 확인
- [ ] **AGENTSEC08** — prompt injection 방어: 직접(NL 질문) + **간접(semantic layer 콘텐츠, DB 결과 row)** injection surface 모두에 ApplyGuardrail(prompt-attack block). 출력에 sensitive-info 필터로 PII 마스킹
- [~] **AGENTSEC03-BP02** — 사용자 위임: Cognito 인증 → user context 전파 → downstream authz. 에이전트가 사용자 역할 assume 금지 — 🔶 부분: **admin 경로 사용자 JWT On-Behalf-Of 구현** ✅ (admin panel 이 사용자 AccessToken 을 그대로 Gateway MCP 에 전달 → Cedar 가 실제 사용자 그룹으로 도구 단위 인가, E2E: e2e-user 는 admin 도구 미노출/403·e2e-manager 는 허용). orchestrator 도 `forwardedProps.userAccessToken` additive 로 OBO 지원 — 단 채팅 UI 에 로그인이 없어 기본은 서비스 계정 위임(M2M). `GetWorkloadAccessTokenForJWT` 기반 workload identity 전파·row/table 수준 authz 는 후속(현 구현은 gateway 인바운드 JWT 로 동등한 사용자 신원 인가를 달성)
- [ ] **AGENTSEC06** — trust zone 분리: data-access 컴포넌트를 상위 trust zone subnet/SG로 분리
- [ ] **AGENTSEC07** — rogue-agent 격리: 이상 SQL 볼륨/미승인 테이블 접근 시 EventBridge → deny-all policy 부착 + forensic state S3 캡처
- [ ] **AGENTSEC05** — non-repudiation: 생성 SQL·resolved entity·실행 identity 구조화 로깅 → S3 Object Lock(immutable audit)
- [ ] VPC: Bedrock·AgentCore·Data API·Secrets Manager·KMS에 VPC endpoint(데이터 계층 인터넷 경로 제거), CMK 암호화(Memory·token vault·audit S3)
- [~] **AGENTSEC01** — Memory 격리: actor별 namespace, 입력 sanitize, poisoning 방지 — 🔶 부분: semantic layer candidate/published 분리 구현(candidate 는 OpenSearch·Neptune 에 미노출 — 간접 prompt injection 방어선). Memory actor 격리·sanitize 는 후속

## 3. 안정성 (AGENTREL)

- [ ] **AGENTREL02-BP05** — 계층적 HITL: LIMIT+저비용 SELECT=autonomous / 대규모·고비용 스캔=notify / PII·admin 변경=approve. Step Functions `.waitForTaskToken` + timeout→safe-default(무한 대기 금지), reviewer·rationale·timestamp 감사
- [ ] **AGENTREL06-BP04** — query 실행 idempotency: key = hash(최종 SQL + params + data-source + user scope), DynamoDB conditional write + TTL result cache
- [~] **AGENTREL07-BP02** — Data API retry 분류 — 🔶 부분: Redshift Data API async polling(0.5s 간격·60s 타임아웃·초과 시 cancel) 구현. self-correction 루프가 오류를 SQL 재생성으로 되먹임(MAX_SQL_CORRECTIONS 예산). 세분화된 retryable/non-retryable 분류·circuit breaker 는 후속
- [~] **AGENTREL04-BP03** — fallback chain: SQL-gen 주 모델 → 저가 모델 → 캐시/템플릿 쿼리 → graceful 응답("스키마/유사 템플릿 제시"). DB 에러를 SQL-gen에 되먹여 bounded self-repair(1회) — 🔶 부분: **설정 fallback 구현** ✅ (bundle 오버라이드 실패·빈 포인터 시 코드 기본 프롬프트·모델로 자동 폴백 — 개선 루프 장애가 코어 질의 경로를 막지 않음). self-correction 루프(MAX_SQL_CORRECTIONS 예산)도 DB 에러 되먹임에 해당. 저가 모델·캐시/템플릿 체인은 후속
- [x] **AGENTREL05-BP03** — grounding: SQL 생성기는 semantic layer/`information_schema`에서 검색한 실제 스키마에 grounding. semantic store 다운 시 degrade + 사용자 고지 ✅ 구현: schema hybrid + 용어/fewshot 검색(Composite) grounding, term 검색·Neptune 순회 실패 시 graceful degrade 구현. (실행 전 컬럼 존재 검증 stage 는 후속)
- [ ] **AGENTREL08** — query timeout·LIMIT·max-scanned-bytes를 AppConfig로 외부화(핫스왑), 초과 시 truncated 결과 + 범위 축소 안내
- [x] **AGENTREL03** — Memory 분류: 단기(session, TTL) / 장기(semantic) namespace 분리, checkpoint 복구 ✅ 구현: STM(AgentCore Memory) ≠ semantic layer(DynamoDB system-of-record) 분리 유지. clarification interrupt 상태는 세션 캐시(LRU)로 복구, 캐시 미스 시 CLARIFICATION_EXPIRED graceful 처리. (AgentCoreMemorySessionManager 는 Graph 세션 미지원 — 알려진 한계)

## 4. 성능 효율성 (AGENTPERF)

- [ ] **AGENTPERF02-BP01** — iteration cap + confidence 조기종료: 단순 lookup 1~2 loop, self-correction 루프 상한
- [ ] **AGENTPERF02-BP02** — 모델 tiering: intent 분류·disambiguation은 소형 모델, SQL 생성·복잡 join만 premium. 실패 시 상위 모델 cascade
- [ ] **AGENTPERF03** — 다층 캐싱: ① Bedrock prompt caching(안정적 schema/system prefix) ② semantic cache(동등 질문→기존 SQL 재사용) ③ tool-output cache. DDL 이벤트 기반 invalidation
- [x] **AGENTPERF03-BP02** — context budgeting: 전체 스키마 dump 금지, relevance 필터링 검색 + hybrid + re-rank top-k ✅ 구현: 스키마·용어·fewshot 모두 hybrid(BM25+kNN) top-k 검색으로만 주입, Composite 병합 상한 top_k*2
- [ ] **AGENTPERF02-BP04** — streaming: sub-second TTFT, Data API 실행 중 progress 이벤트
- [ ] **AGENTPERF06** — tool > sub-agent: 결정론적 작업(schema lookup·SQL validation·실행)은 MCP 툴로, 추론 sub-agent 금지. 반복 시퀀스는 meta-tool로 축약
- [ ] 대용량 result set은 참조(S3/result token)로 전달, supervisor context에 inline 금지

## 5. 비용 최적화 (AGENTCOST)

- [ ] **AGENTCOST01-BP03** — hybrid supervisor: 결정론적 라우팅(데이터소스 선택, 메타데이터 질문 여부)은 규칙/경량 classifier로
- [ ] **AGENTCOST07** — termination contract: iteration cap·confidence exit·세션 token budget을 AgentCore Policy + Guardrails(제어 평면)에서 강제. 계층적 budget(cycle/task/day) + 자동 cutoff
- [ ] **AGENTCOST05** — 비용 attribution 태그: `agent-id, agent-role, workflow-id, task-type, environment` 태그를 invocation·session에 부착, Cost Explorer 활성화, query 완료당 비용 산출
- [ ] 핵심 지표: cost-per-correct-response, cache hit rate, cascade escalation rate
- [ ] 비긴급 eval/admin 워크로드는 batch inference 검토

## 6. 지속 가능성 (AGENTSUS)

- [x] **AGENTSUS01-BP02** — 반복 패턴(schema retrieval, SQL validation, result format)을 재사용 가능한 파라미터화 MCP 툴로 (1회 구축, 전체 재사용) ✅ 구현: search_schema 단일 도구가 스키마+용어+fewshot+join-path 를 파라미터(query, top_k)로 통합 제공
- [ ] 공유 서비스: 단일 캐시, 단일 auth 경로(AgentCore Identity), 공유 FM 접근
- [ ] ECS Fargate right-size + target-tracking autoscaling (피크 고정 provisioning 금지)
- [ ] **AGENTSUS03-BP04** — decommission lifecycle: agent catalog(owner/purpose/usage), inactive 플래그, 중복 에이전트 방지

---

## Focus area 매핑 (렌즈에 별도 필러 없음, 분산 내장)

| Focus | 관련 BP |
|---|---|
| Agent loop safety | AGENTCOST07-BP01(자동 cutoff), AGENTOPS07-BP01(runaway 감지), AGENTREL07-BP02(retry budget + circuit breaker), AGENTPERF02-BP01(iteration cap) |
| Tool governance | AGENTOPS04(registry/MCP/fallback), AGENTSEC02(tool authz, Cedar default-deny) |
| Memory management | AGENTSEC01(격리/poisoning), AGENTREL03(분류/checkpoint), AGENTCOST03, AGENTPERF03 |
| Human oversight | AGENTREL02-BP05(autonomous/notify/approve), AGENTSEC04-BP02(중요 결정 HITL), AGENTSEC07(oversight 보호) |
