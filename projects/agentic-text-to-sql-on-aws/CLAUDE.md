# agentic-text-to-sql-on-aws — 프로젝트 컨텍스트

Amazon Bedrock AgentCore 기반 agentic Text-to-SQL 솔루션. 설계·구현·배포 검증 완료 상태이며,
E2E 스모크 테스트(레벨 1~7, `scripts/e2e-smoke.sh`)가 전체 경로를 검증한다.

## 필독 문서

1. `docs/architecture.md` — 전체 설계 문서(단일 진실 원천). 페르소나(Admin/Manager/User),
   핵심 결정 D1~D9, 5계층 설계, 개선 파이프라인(Track A/B), 버저닝 전략(§5.3), 리스크.
2. `docs/well-architected-checklist.md` — Agentic AI Lens 기반 구현 체크리스트.
   구현을 변경할 때 해당 항목을 점검·갱신한다.
3. `docs/architecture-review.html` — 인터랙티브 아키텍처 다이어그램.
   구조 변경 시 이 다이어그램도 갱신할 것.
4. 각 컴포넌트 README — 도구 시그니처·env var·로컬 개발 방법 (인터페이스 계약의 원천).

## 핵심 제약 (위반 금지)

- **리전 us-west-2**, 브랜치 `feature/agentic-text-to-sql`
- **IaC는 CDK (TypeScript)**, Python은 uv
- **Tool layer에 Lambda 금지** — 도구는 AgentCore Runtime 호스팅 MCP 서버 → Gateway MCP target
  (예외: Evaluations code-based evaluator만 Lambda — 서비스 규격)
- **Runtime 배포는 컨테이너(ECR) 방식** — direct code upload 금지. docker 빌드, 실패 시 finch 폴백
- **최소 권한**: 컴포넌트별 IAM role 분리, Cedar default-deny, read-only DB 사용자
- **READ-ONLY 4중 방어**: Cedar + LLM 밖 SQL AST validator(SQLGlot) + read-only IAM + DB SELECT-only grant
- 복잡한 모듈은 OOP (추상 base class + 구현체)
- semantic layer 쓰기는 DynamoDB 한 곳만 (dual-write 금지), candidate/published/rejected 상태 분리
- **도구 시그니처 하위호환**: 기존 필드 제거·개명 금지, 신규 필드는 additive only
  (각 MCP 서버 README 의 시그니처가 계약)
- 한국어 README, 시크릿은 `.example` 파일, cleanup 섹션 필수
- 사용자를 지칭할 때 페르소나 용어: Admin / Manager / User

## 향후 과제

- `demo/` Jupyter notebook 실습 구조 (docs/architecture.md §10)
- CodePipeline 평가 게이트 연결 (체크리스트 AGENTOPS03), HITL·Guardrails 등 체크리스트 후속 항목
- 채팅 UI 인증 강제(현재 admin panel 만 강제), ALB HTTPS 전환

## 실전 학습 사항 — AWS 인프라/배포 (같은 함정 재발 방지)

- Aurora 엔진 버전은 CDK 상수 존재 ≠ 리전 제공. 배포 전
  `aws rds describe-db-engine-versions --engine aurora-postgresql`로 확인 (16.6은 us-west-2 퇴역 → 16.9 사용 중).
  v2는 `DatabaseCluster`+`ClusterInstance.serverlessV2` (`ServerlessCluster`는 v1 전용 deprecated).
- AgentCore L2는 `aws-cdk-lib/aws-bedrockagentcore` **stable** (alpha 아님). AG-UI 에이전트는
  `ProtocolType.AGUI` (HTTP 아님). Runtime L2가 logs/X-Ray/메트릭 권한을 자동 부가.
- Base↔Runtime 스택 순환 의존은 결정적 이름 규칙 ARN 와일드카드로 회피 (runtime명은 언더스코어 `agentic_t2sql_*`).
- **IAM role `description` 은 Latin-1 만 허용** — 한국어 넣으면 배포 실패. Cognito 그룹·CfnOutput·
  AgentCore Runtime/GatewayTarget description 은 한국어 OK (IAM 만 제약).
- **Redshift Serverless workgroup 은 3-AZ 서브넷 요구** — 2-AZ VPC 재사용 불가,
  Data API 만 쓰면 NAT 없는 전용 isolated VPC 로 충분.
- **Redshift Data API 로 CREATE USER 등 관리 작업**은 SecretArn 없이 호출하면 IAM 매핑
  사용자(권한 부족)로 실행됨 — namespace 의 `manageAdminPassword` 가 만든
  `redshift!<ns>-<admin>` 시크릿을 get_namespace 로 찾아 SecretArn 으로 넘겨야 한다.
- 이 레포 로컬 기본 리전은 us-east-1 — 스크립트에서 `AWS_REGION=us-west-2` **강제 고정** (`:-` 폴백 금지).
- 이미지만 바뀌면 ECS 스택 재배포 불필요: `aws ecs update-service --force-new-deployment` (:latest 재pull).
- Runtime 이미지만 갱신 시 CFN 변경 없음 → `update-agent-runtime`(동일 설정 재전송)으로
  :latest digest 재해석 + 새 버전 생성. READY 후에도 warm microVM 순환에 수 분 필요.
- docker 데몬이 없으면 finch 사용. finch VM 디스크 풀로 read-only FS 에러 시: `finch vm stop/start` + `finch builder prune`.
  docker keychain 오류(`already exists in the keychain`) 시 `docker logout <registry>` 후 재로그인.
- macOS 기본 bash 3.2 는 `declare -A` 미지원 — 스크립트는 case 함수 매핑으로.
- semantic-layer 를 경로 의존성으로 쓰는 이미지는 빌드 컨텍스트가 레포 루트 —
  Dockerfile 에서 builder/runtime **동일 경로 유지**(venv 절대경로) + 루트 `.dockerignore` 필수.

## 실전 학습 사항 — Gateway/Cedar/Identity

- **Gateway↔PolicyEngine 연결은 gateway 서비스 role 권한이 배포 게이트**: Gateway 생성 시
  서비스가 gateway role 로 `GetPolicyEngine`·`AuthorizeAction`·`PartiallyAuthorizeActions` 를
  호출해 검증. 권한 리소스는 policy-engine ARN **과 gateway ARN 양쪽**
  (gateway 는 생성 전이라 이름 규칙 와일드카드로).
- **GatewayTarget 생성은 Gateway 서비스가 그 자리에서 MCP 서버에 접속해 도구를 fetch** —
  gateway role 의 InvokeAgentRuntime 정책이 target 보다 먼저 적용돼야 한다.
  `addToPrincipalPolicy(...).policyDependable` 에 target 의존을 걸 것
  (안 걸면 "Authorization error when sending message" NotStabilized — 배포 실측).
- **Cedar 정책에 `action in [AgentCore::Action::"<Target>___<tool>"]` 스코프는 생성 시점 검증 실패**
  ("unable to find an applicable action") — 정책이 target 도구 동기화 전에 검증됨.
  도구별 스코프는 2-phase 로: 광역 permit 배포 → target 동기화 후 `-c cedarActionScoping=true`
  재배포(정책 논리 ID 동일 유지 — statement 만 CFN update).
- **Cedar principal 의 JWT claim 은 tag**: `principal.hasTag("cognito:groups") &&
  principal.getTag("cognito:groups") like "*Manager*"` 패턴 (배열 claim 은 문자열 직렬화).
- **Cedar 는 미인가 도구를 tools/list 에서 아예 제외한다** — E2E 거부 검증은 "미노출"이
  1차 증거. 응답 텍스트 키워드 매칭("denied" 등)으로 판정할 때는 테스트 페이로드에
  판정 키워드를 넣지 말 것(에코백 오탐 PASS — 배포 실측). `status=="ok"` 면 무조건 거부 실패로
  우선 판정해야 한다.
- Gateway 도구명은 `<TargetName>___<tool>` 프리픽스 — 클라이언트는 suffix 매칭으로 분류.
- **MCP 서버 이미지에 도구를 추가하면 gateway target 을 `synchronize-gateway-targets` 로
  재동기화**해야 tools/list 에 신규 도구가 노출된다 (CFN 변경 없음 → 자동 동기화 안 됨).

## 실전 학습 사항 — Evaluations/Optimization

- **AgentCore Evaluations/Bundle API 는 로컬 AWS CLI 에 없을 수 있다** (CLI 모델 구버전) —
  실존 확인·스크립트는 boto3(orchestrator venv, 1.43+)로. Evaluations 는 us-west-2 동작 확인,
  Optimization(Recommendations·Bundle)은 Preview, A/B 분할 API 는 미확인 → SSM 포인터 수동 전환 폴백.
- **`CreateOnlineEvaluationConfig` 는 생성 시점에 실행 role 의 `lambda:GetFunction`+`InvokeFunction`
  보유를 검증한다** — `grantInvoke`(InvokeFunction만)로는 ValidationException. 정책 적용 후 config 가
  생성되도록 `addToPrincipalPolicy(...).policyDependable` 의존 필수 (GatewayTarget 과 동일 함정).
- **`StartBatchEvaluation` 은 실행 role 파라미터가 없고 호출자(FAS) 자격증명으로 동작** —
  호출 주체(admin-web task role)에 `logs:StartQuery/GetQueryResults`(runtime 로그 그룹+`aws/spans`)
  와 `logs:CreateLogGroup/CreateLogStream/PutLogEvents/PutRetentionPolicy`
  (`/aws/bedrock-agentcore/evaluations/*`)를 부여해야 한다 (전부 배포 실측 — 오류가 한 번에
  안 나오고 권한 하나씩 순차 노출되니 한꺼번에 부여할 것).
- Configuration Bundle 의 `components` 는 `map<string, {configuration: document}>` — 키를
  runtime ARN 대신 논리 키("orchestrator")로 쓰는 편이 자기참조·교차 스택 순환을 피한다.

## 실전 학습 사항 — 애플리케이션 (Strands/AG-UI/OpenSearch)

- Strands→AG-UI 공식 패턴: `ag_ui_strands`(PyPI) `StrandsAgent` 래퍼 + `BedrockAgentCoreApp` entrypoint.
  입력은 `RunAgentInput`, actorId/sessionId는 본문이 아닌 **AgentCore 헤더**(Session-Id, ≥33자 필수).
- **`from __future__ import annotations` + strands `@tool` 데코레이터 조합 함정**: 데코레이터가
  `get_type_hints`로 어노테이션을 모듈 전역에서 평가하므로, 함수 내부 지연 임포트한 타입은
  `globals()`에 주입해야 NameError 가 안 난다. 실 SDK 로 도구 생성을 검증하는 단위 테스트를
  반드시 둘 것 (로컬 pytest 는 통과했는데 배포에서 크래시났던 사례).
- **AgentCore Runtime 은 stdout 만 CloudWatch 로 보낸다** — 파이썬 `logging` 은 루트 핸들러가
  없으면 유실된다. 구조화 로그(`t2sql_query_record`)를 남기려면 앱 모듈에서
  `logging.basicConfig(level=INFO, stream=sys.stdout)` 명시 필수.
- CopilotKit v2 핸들러는 `/info`, `/agent/:id/run` 등 하위 경로 라우팅 → Next.js route는 반드시
  **catch-all** `app/api/copilotkit/[[...slug]]/route.ts` (단일 route.ts면 하위 경로가 Next 404).
- TS SigV4 서명(@aws-sdk/signature-v4): `uriEscapePath`는 **기본값(true) 유지**. 서명 전 헤더를
  **소문자 통일·중복 제거** (undici가 대소문자 중복 헤더를 병합해 서명 불일치 403 유발).
- orchestrator는 TOOL_CALL_RESULT를 방출하지 않음 — UI 결과 표는 synthesis TEXT_MESSAGE의 GFM markdown 표로 렌더.
- clarification 재개는 같은 microVM 내 모듈 레벨 세션 캐시(runner+MCP클라이언트 유지)로 동작.
  `AgentCoreMemorySessionManager` 는 Graph(multiagent) 세션 영속화를 지원하지 않음 —
  microVM 교체 시 CLARIFICATION_EXPIRED 로 재질의 안내 (알려진 한계).
- **OSIS sink 는 인덱스가 없으면 동적 매핑으로 자동 생성** → embedding 이 float 로 매핑돼
  kNN 질의가 400. seed 가 DynamoDB 쓰기 전에 knn_vector 매핑으로 인덱스를 선생성해야 한다.
- **OpenSearch `hybrid` 쿼리는 compound(bool) 안에 중첩 불가** — 필터는 `post_filter` 로.
- 관리형 OpenSearch 도메인의 SigV4 서비스명은 **`es`** (aoss는 Serverless 전용).
- pyproject에 `readme = "README.md"`가 있으면 Dockerfile에서 `COPY README.md` 필수 (uv sync 실패 방지).

## 실전 학습 사항 — E2E 검증

- **E2E 는 실행마다 고유 runtimeSessionId 사용** — 고정 ID 는 구버전 이미지의 warm microVM
  으로 계속 라우팅돼 배포 검증이 착시를 일으킨다.
- E2E 채굴 검증은 "이번 실행 mined>0" 가 아니라 **mined+skipped_existing ≥ 1** 로 판정할 것 —
  재실행이면 기존 후보가 skip 되는 게 정상(중복 방지가 곧 검증 대상)이다.

## 에이전트 팀 운영 규칙

- **에이전트는 역할이 끝나면 그때그때 회수(TaskStop)한다** — 사용하지 않는 에이전트를 대기 상태로
  방치하면 메모리를 과도하게 점유한다. "태스크 완료 + 재호출 가능성 소멸" 시점이 회수 시점.
  종료된 에이전트도 이름으로 SendMessage하면 트랜스크립트 기반 재개가 가능하므로 부담 없이 정리할 것.
- 병렬 구현 에이전트에게는 공통 계약(디렉토리·리소스명·도구 시그니처·env var)을 프롬프트에 고정해
  통합 시점 인터페이스 불일치를 방지한다.
- cdk deploy / seed / ECS 재배포 등 과금·상태 변경 작업은 서브에이전트가 아니라 **사용자 승인이 직접
  이뤄진 메인 세션에서 실행**한다 (서브에이전트는 권한 분류기에 막히며, 우회 지시는 권한 세탁).
