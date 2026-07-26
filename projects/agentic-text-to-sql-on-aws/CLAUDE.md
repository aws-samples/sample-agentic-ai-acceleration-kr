# agentic-text-to-sql-on-aws — 프로젝트 컨텍스트

Amazon Bedrock AgentCore 기반 agentic Text-to-SQL 솔루션. **설계는 합의 완료, 구현 단계.**

## 필독 문서 (설계의 단일 진실 원천)

1. `ARCHITECTURE.md` — 전체 설계 확정본. 페르소나(Admin/Manager/User), 확정 결정 D1~D9,
   5계층 설계, 개선 파이프라인(Track A/B), 버저닝 전략(§5.3), 마일스톤 M1~M5, 리스크.
2. `docs/well-architected-checklist.md` — Agentic AI Lens 기반 구현 체크리스트.
   최상위 우선 10개 항목의 마일스톤 매핑 포함. 마일스톤 완료 시 점검.
3. `docs/architecture-review.html` — 사용자와 합의된 인터랙티브 아키텍처 다이어그램.
   구조 변경 시 이 다이어그램도 갱신할 것.

## 핵심 제약 (위반 금지)

- **리전 us-west-2**, 브랜치 `feature/agentic-text-to-sql`
- **IaC는 CDK (TypeScript)**, Python은 uv
- **Tool layer에 Lambda 금지** — 도구는 AgentCore Runtime 호스팅 MCP 서버 → Gateway MCP target
  (예외: Evaluations code-based evaluator만 Lambda — 서비스 규격)
- **Runtime 배포는 컨테이너(ECR) 방식** — direct code upload 금지. docker 빌드, 실패 시 finch 폴백
- **최소 권한**: 컴포넌트별 IAM role 분리, Cedar default-deny, read-only DB 사용자
- **READ-ONLY 4중 방어**: Cedar + LLM 밖 SQL AST validator(SQLGlot) + read-only IAM + DB SELECT-only grant
- 복잡한 모듈은 OOP (추상 base class + 구현체)
- semantic layer 쓰기는 DynamoDB 한 곳만 (dual-write 금지), candidate/published 분리
- 한국어 README, 시크릿은 `.example` 파일, cleanup 섹션 필수
- 사용자를 지칭할 때 페르소나 용어: Admin / Manager / User

## 현재 상태

- [x] 설계 합의 (ARCHITECTURE.md + 다이어그램)
- [x] M1: CDK 인프라 + 코어 파이프라인 E2E — **배포·E2E 3레벨 검증 완료** (2026-07-26).
      3스택(Base/Runtime/Ui) us-west-2 배포됨, E2E 검증은 `scripts/e2e-smoke.sh`
- [x] M2: Semantic layer 완성(Neptune·DynamoDB·동기화) + clarification E2E — **배포·E2E 17체크 검증 완료** (2026-07-26).
      Semantic 스택(DynamoDB·Neptune Serverless·OSIS·graph-sync Lambda) 추가 배포,
      semantic MCP 는 VPC 모드 전환. 인터페이스 기록: `docs/m2-m3-interface-contract.md`
- [x] M3: Gateway·Identity·Cedar·Redshift·가드레일 전체 — **배포·E2E 22체크 검증 완료** (2026-07-26).
      Gateway 스택(Gateway+MCP target 2+Cedar PolicyEngine ENFORCE), Redshift Serverless(Base),
      orchestrator 는 TOOL_PLANE_MODE=gateway 전환(Cognito M2M 서비스 계정 위임).
      사용자별 JWT On-Behalf-Of 전파는 M4+ 로 이월.
- [ ] M4: Admin panel
- [ ] M5: 개선 파이프라인 (Track A + Track B)
- 향후: `demo/` Jupyter notebook 실습 구조 (ARCHITECTURE.md §10)

## M1 실전 학습 사항 (M2+ 작업 시 참고 — 같은 함정 재발 방지)

**AWS/배포**
- Aurora 엔진 버전은 CDK 상수 존재 ≠ 리전 제공. 배포 전
  `aws rds describe-db-engine-versions --engine aurora-postgresql`로 확인 (16.6은 us-west-2 퇴역 → 16.9 사용 중).
  v2는 `DatabaseCluster`+`ClusterInstance.serverlessV2` (`ServerlessCluster`는 v1 전용 deprecated).
- AgentCore L2는 `aws-cdk-lib/aws-bedrockagentcore` **stable** (alpha 아님). AG-UI 에이전트는
  `ProtocolType.AGUI` (HTTP 아님). Runtime L2가 logs/X-Ray/메트릭 권한을 자동 부가.
- Base↔Runtime 스택 순환 의존은 결정적 이름 규칙 ARN 와일드카드로 회피 (runtime명은 언더스코어 `agentic_t2sql_*`).
- 관리형 OpenSearch 도메인의 SigV4 서비스명은 **`es`** (aoss는 Serverless 전용) — 인덱서·검색기 모두.
- Runtime 이미지/env 갱신 후에도 warm microVM이 잠시 옛 config로 응답할 수 있음 — 몇 분 내 순환되니 재시도.
- 이미지만 바뀌면 스택 재배포 불필요: `aws ecs update-service --force-new-deployment` (:latest 재pull).
- 이 레포 로컬 기본 리전은 us-east-1 — 스크립트에서 `AWS_REGION=us-west-2` **강제 고정** (`:-` 폴백 금지).
- docker 데몬이 없으면 finch 사용. finch VM 디스크 풀로 read-only FS 에러 시: `finch vm stop/start` + `finch builder prune`.

**AG-UI/CopilotKit/Strands**
- Strands→AG-UI 공식 패턴: `ag_ui_strands`(PyPI) `StrandsAgent` 래퍼 + `BedrockAgentCoreApp` entrypoint.
  입력은 `RunAgentInput`, actorId/sessionId는 본문이 아닌 **AgentCore 헤더**(Session-Id, ≥33자 필수).
- CopilotKit v2 핸들러는 `/info`, `/agent/:id/run` 등 하위 경로 라우팅 → Next.js route는 반드시
  **catch-all** `app/api/copilotkit/[[...slug]]/route.ts` (단일 route.ts면 하위 경로가 Next 404).
- TS SigV4 서명(@aws-sdk/signature-v4): `uriEscapePath`는 **기본값(true) 유지** (AgentCore가 표준
  canonical 재인코딩 기대). 서명 전 헤더를 **소문자 통일·중복 제거** (HttpAgent가 대소문자 중복 헤더를
  넘기고 undici가 병합해 서명 불일치 403 유발).
- orchestrator는 TOOL_CALL_RESULT를 방출하지 않음 — UI 결과 표는 synthesis TEXT_MESSAGE의 GFM markdown 표로 렌더.
- pyproject에 `readme = "README.md"`가 있으면 Dockerfile에서 `COPY README.md` 필수 (uv sync 실패 방지).

## M2 실전 학습 사항 (M3+ 작업 시 참고)

- **`from __future__ import annotations` + strands `@tool` 데코레이터 조합 함정**: 데코레이터가
  `get_type_hints`로 어노테이션을 모듈 전역에서 평가하므로, 함수 내부 지연 임포트한 타입
  (예: ToolContext)은 `globals()`에 주입해야 NameError 가 안 난다. 실 SDK 로 도구 생성을
  검증하는 단위 테스트를 반드시 둘 것 (로컬 pytest 는 통과했는데 배포에서 크래시났던 사례).
- **OSIS sink 는 인덱스가 없으면 동적 매핑으로 자동 생성** → embedding 이 float 로 매핑돼
  kNN 질의가 400. seed 가 DynamoDB 쓰기 전에 knn_vector 매핑으로 인덱스를 선생성해야 한다
  (`seed-semantic` 이 수행, 잘못된 매핑 감지 시 재생성 — 문서는 Streams 재전파로 복원).
- **OpenSearch `hybrid` 쿼리는 compound(bool) 안에 중첩 불가** — 필터는 `post_filter` 로.
- **E2E 는 실행마다 고유 runtimeSessionId 사용** — 고정 ID 는 구버전 이미지의 warm microVM
  으로 계속 라우팅돼 배포 검증이 착시를 일으킨다.
- Runtime 이미지만 갱신 시 CFN 변경 없음 → `update-agent-runtime`(동일 설정 재전송)으로
  :latest digest 재해석 + 새 버전 생성. READY 후에도 warm VM 순환에 수 분 필요.
- clarification 재개는 같은 microVM 내 모듈 레벨 세션 캐시(runner+MCP클라이언트 유지)로 동작.
  `AgentCoreMemorySessionManager` 는 Graph(multiagent) 세션 영속화를 지원하지 않음 —
  microVM 교체 시 CLARIFICATION_EXPIRED 로 재질의 안내 (알려진 한계).
- macOS 기본 bash 3.2 는 `declare -A` 미지원 — 스크립트는 case 함수 매핑으로.

## M3 실전 학습 사항 (M4+ 작업 시 참고)

- **Gateway↔PolicyEngine 연결은 gateway 서비스 role 권한이 배포 게이트**: Gateway 생성 시
  서비스가 gateway role 로 `GetPolicyEngine`·`AuthorizeAction`·`PartiallyAuthorizeActions` 를
  호출해 검증(GenesisPolicyEngineCheck). 권한 리소스는 policy-engine ARN **과 gateway ARN 양쪽**
  (gateway 는 생성 전이라 이름 규칙 와일드카드로).
- **Cedar 정책에 `action in [AgentCore::Action::"<Target>___<tool>"]` 스코프는 생성 시점 검증 실패**
  ("unable to find an applicable action") — CFN 상 정책이 target 도구 동기화 전에 검증됨.
  도구별 스코프는 target 생성 후 2-phase 로 갱신하거나 when 절 조건으로 표현할 것.
- **Cedar principal 의 JWT claim 은 tag**: `principal.hasTag("cognito:groups") &&
  principal.getTag("cognito:groups") like "*Manager*"` 패턴 (배열 claim 은 문자열 직렬화).
  Denied 그룹 forbid 는 tools/list 자체를 빈 목록으로 만든다(도구 미노출 = deny 증거).
- **Redshift Data API 로 CREATE USER 등 관리 작업**은 SecretArn 없이 호출하면 IAM 매핑
  사용자(권한 부족)로 실행됨 — namespace 의 `manageAdminPassword` 가 만든
  `redshift!<ns>-<admin>` 시크릿을 get_namespace 로 찾아 SecretArn 으로 넘겨야 한다.
- **Redshift Serverless workgroup 은 3-AZ 서브넷 요구** — 기존 2-AZ VPC 재사용 불가,
  Data API 만 쓰면 NAT 없는 전용 isolated VPC 로 충분.
- Gateway 도구명은 `<TargetName>___<tool>` 프리픽스 — 클라이언트는 suffix 매칭으로 분류.
- docker keychain 오류(`already exists in the keychain`) 시 `docker logout <registry>` 후 재로그인.

## 에이전트 팀 운영 규칙

- **에이전트는 역할이 끝나면 그때그때 회수(TaskStop)한다** — 사용하지 않는 에이전트를 대기 상태로
  방치하면 메모리를 과도하게 점유한다. "태스크 완료 + 재호출 가능성 소멸" 시점이 회수 시점.
  종료된 에이전트도 이름으로 SendMessage하면 트랜스크립트 기반 재개가 가능하므로 부담 없이 정리할 것.
- 병렬 구현 에이전트에게는 공통 계약(디렉토리·리소스명·도구 시그니처·env var)을 프롬프트에 고정해
  통합 시점 인터페이스 불일치를 방지한다.
- cdk deploy / seed / ECS 재배포 등 과금·상태 변경 작업은 서브에이전트가 아니라 **사용자 승인이 직접
  이뤄진 메인 세션에서 실행**한다 (서브에이전트는 권한 분류기에 막히며, 우회 지시는 권한 세탁).
