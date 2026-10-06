# FastAPI Architecture for Agentic AI Server

## 📐 아키텍처 개요

이 프로젝트는 **Layered Architecture (계층형 아키텍처)** 패턴을 따릅니다. FastAPI와 Agentic AI 시스템에 최적화된 구조입니다.

## 🏗️ 폴더 구조

```
server/
├── main.py                  # FastAPI 앱, 라우터 등록, 백그라운드 collector 기동
│
├── core/                    # 핵심 설정 및 의존성
│   ├── config.py            # 환경 변수, OIDCProvider 설정
│   ├── dependencies.py      # 의존성 주입 (DI Container)
│   ├── auth.py              # Bearer 토큰 검증 + RBAC (Cognito access token / OIDC id_token)
│   ├── oidc_verifier.py     # 범용 OIDC id_token 검증 (Entra ID, Keycloak, Okta …)
│   └── clock.py             # usage 집계가 날짜를 세는 달력(타임존)
│
├── models/                  # Pydantic 모델 (도메인 모델)
│   ├── common.py            # 공통 모델 (Message, StreamEvent 등)
│   ├── thread.py            # Thread 도메인 모델
│   ├── artifact.py          # 에이전트 산출물(사이드 패널 아티팩트)
│   ├── attachment.py        # 첨부 파일 형식·제한
│   ├── auth.py              # 로그인 / 토큰 갱신 / OIDC 세션
│   ├── harness.py           # Managed Agent Harness
│   ├── knowledge.py         # 사용자 생성 Knowledge Base
│   ├── mcp.py, mcp_apps.py  # MCP 서버 / MCP Apps 릴레이
│   ├── registry.py          # Agent Registry 레코드
│   └── skill.py             # 스킬 번들
│
├── repositories/            # 데이터 접근 계층 (DynamoDB, Repository Pattern)
│   ├── base.py              # DynamoDB 기본 Repository
│   ├── thread_repository.py
│   ├── artifact_repository.py
│   ├── knowledge_repository.py
│   ├── prefs_repository.py  # 사용자별 환경설정
│   ├── usage_repository.py  # 사용량 롤업
│   └── user_repository.py   # OIDC 로그인 사용자 (로그인마다 기록)
│
├── services/                # 비즈니스 로직 계층 (Service Layer)
│   ├── streaming_service.py       # 채팅 턴 실행: 클라이언트 선택, 스트리밍, 저장
│   ├── thread_service.py
│   ├── agent_access.py            # 턴이 도달할 수 있는 실행 대상을 서버 측에서 바인딩
│   ├── registry_service.py        # AgentCore Agent Registry (조회/등록/승인)
│   ├── agent_sync_service.py      # 배포된 런타임·게이트웨이를 Registry와 동기화
│   ├── skill_bundle_service.py    # 스킬 번들 검증 + S3 발행
│   ├── harness_service.py         # Managed Agent Harness 생성·삭제·managed memory
│   ├── harness_output_service.py  # harness 턴이 샌드박스에 남긴 파일 수집
│   ├── harness_sandbox_scripts.py # 샌드박스 안에서 실행하는 프로그램
│   ├── artifact_service.py, artifact_preview_service.py, attachment_service.py
│   ├── browser_screenshot_service.py
│   ├── knowledge_service.py, knowledge_provisioner.py   # Bedrock KB 4개 리소스 멱등 프로비저닝
│   ├── mcp_service.py, mcp_apps_service.py
│   ├── usage_service.py, collector_service.py, pricing_service.py, rate_card_service.py,
│   │   billing_service.py, telemetry_service.py, trace_service.py, observability_service.py,
│   │   evaluation_service.py        # Insights 대시보드: 사용량 원장, 요율, Cost Explorer, CloudWatch
│   ├── directory_service.py       # Cognito sub → email
│   ├── layout_service.py, nav_service.py   # 대시보드 레이아웃, 사용자 메뉴 노출 설정
│
├── agents/                  # Agent 클라이언트 (자세한 설명은 agents/README.md)
│   ├── base.py                    # AgentClient 인터페이스 (execute_stream)
│   ├── agentcore_client.py        # AgentCore Runtime (InvokeAgentRuntime, SSE 정규화)
│   ├── harness_client.py          # Managed Agent Harness (InvokeHarness)
│   ├── harness_command_client.py  # harness 세션 컨테이너 안에서 셸 명령 실행
│   ├── harness_event_adapter.py   # InvokeHarness 이벤트 → Strands 이벤트 형식
│   ├── agent_config.py, message_utils.py
│   └── formatters/event_formatter.py
│
├── mcp_core/                # MCP 클라이언트 공통 (세션, 에러 해석, _meta.ui 규칙, model context)
├── data/                    # mcp-servers.json, 모델 단가 테이블
├── scripts/                 # 운영 스크립트 (cost tag 활성화, 원장 백필, 데모 사용자 시드 등)
│
└── routes/                  # API 엔드포인트 (Controller Layer)
    ├── health.py            # GET / 헬스체크
    ├── config.py            # /api/config — UI 기능 플래그 (registry/harness/basicChat). 공개
    ├── auth.py              # /api/auth — Cognito 비밀번호 로그인, OIDC 세션
    ├── threads.py           # /threads — 스레드 CRUD + 스트리밍 실행
    ├── artifacts.py         # /api/artifacts — 아티팩트 조회·다운로드·공유 링크
    ├── knowledge.py         # /api/knowledge — 사용자별 Knowledge Base
    ├── registry.py          # /api/registry — Agent Registry
    ├── harness.py           # /api/harnesses — Harness 조회·생성·삭제
    ├── mcp.py               # /api/mcp — MCP 서버 연결 테스트·툴 목록 (admin)
    ├── mcp_apps.py          # /api/mcp-apps — MCP Apps 릴레이 (저권한)
    ├── insights.py          # /api/insights — 사용량·비용·트레이스 대시보드
    └── settings.py          # /api/settings — 관리자 플랫폼 설정
```

## 채팅 실행 경로

모든 채팅은 **AgentCore에 배포된 에이전트**가 처리하며, 실행 경로는 두 가지입니다.
프론트엔드는 Registry에서 선택한 레코드를 stream config로 전달하고, `streaming_service`가
`agent_access`로 그 턴이 도달할 수 있는 대상을 서버 측에서 확정한 뒤 클라이언트를 고릅니다.

- **Runtime 에이전트** (`agentRuntimeArn`) → `AgentCoreClient`가 `InvokeAgentRuntime` 호출
- **Managed Agent Harness** → `HarnessClient`가 `InvokeHarness` 호출,
  `harness_event_adapter`가 이벤트를 Strands 형식으로 변환

레지스트리 레코드 없이 모델만 골라 대화하는 **기본 채팅**은 `/api/config`의 `basicChat`
플래그로 노출되며, 허용 모델 목록(`BASIC_CHAT_ALLOWED_MODELS`)과 답할 런타임
(`BASIC_CHAT_RUNTIME_ARN` 또는 기본 `AGENT_RUNTIME_ARN`)이 둘 다 있어야 켜집니다.

`AgentCoreClient`는 런타임이 내보내는 두 가지 SSE 형식을 모두 Strands 이벤트로
정규화합니다.

- `{"event": {...}}` (Strands 네이티브) → 그대로 통과
- `{"type": "text_delta"|"final", ...}` → `messageStart`/`contentBlockDelta`/`messageStop`으로 변환

## Agent Registry

`routes/registry.py` + `services/registry_service.py`가 AgentCore Agent Registry를
감쌉니다 (`/api/registry/*`). 대상 registry는 `AGENT_REGISTRY_ID` 환경변수로
지정하며, 미설정 시 503과 안내 메시지를 반환합니다.

레코드 타입은 AWS `descriptorType` enum(`A2A` / `AGENT_SKILLS` / `MCP` / `CUSTOM`)을
그대로 사용합니다. 에이전트(A2A) 레코드는 A2A agent card의 `url`/`agentRuntimeArn`에
런타임 ARN을 담아, UI에서 레코드를 고르면 곧바로 해당 런타임과 채팅할 수 있게 합니다.

### 검색 경로

`search_records`는 `SearchRegistryRecords`(데이터 플레인)를 쓰고, `list_records`는
`ListRegistryRecords`(컨트롤 플레인)를 씁니다. 둘은 대체 관계가 아닙니다.

- 검색은 APPROVED 승인본만, 관련도순, `maxResults` 상한 **20**, `nextToken` 없음.
  타입 narrowing은 네이티브 메타데이터 필터(`descriptorType.$in`)로 넘깁니다 —
  랭킹 전에 후보를 줄이고, AWS 문서가 쿼리 텍스트에 타입 조건을 섞지 말라고
  명시합니다. 반환 순서는 재정렬하지 않습니다.
- 검색 결과에는 descriptors가 포함되므로 hydrate가 필요 없습니다. list에는
  없어서 A2A/CUSTOM 레코드를 개별 조회해 채웁니다.

### 스킬 번들

스킬은 파일 하나가 아니라 디렉터리입니다 — `SKILL.md` + 선택적 `references/`,
`scripts/`, `assets/` (AgentSkills 스펙). 그런데 **Registry 레코드는 디렉터리를
담을 수 없습니다**. `AgentSkillsDescriptor`에는 `skillMd.inlineContent`와
`skillDefinition.inlineContent` 두 개의 인라인 필드밖에 없고, AWS 문서도 마크다운은
검색용 메타데이터일 뿐이며 "Registry does not support storing other agent skill
files"라고 명시합니다. S3를 가리킬 수 있는 쪽은 **Harness의 skill source**
(`{"s3": {"uri": "s3://…/skills/<name>/"}}`)입니다.

그래서 역할을 이렇게 나눕니다.

- **S3(`SKILLS_BUCKET`)가 번들의 원본**입니다. `skill_bundle_service`가 업로드된
  `.md` / `.zip`을 스펙에 맞게 검증한 뒤 `skills/<name>/` 로 발행합니다. prefix는
  frontmatter의 `name`이고, 이는 "name은 부모 디렉터리명과 같아야 한다"는 스펙
  규칙을 자동으로 만족시키면서 이름을 공유 레지스트리의 고유 키로 만듭니다.
  재발행 시 새 번들에 없는 객체는 삭제합니다 — harness는 레코드의 파일 목록이 아니라
  prefix 전체를 받아가므로, 남겨두면 지운 파일이 계속 배포됩니다.
- **레코드는 검색 인덱스 + 포인터**입니다. `SKILL.md`는 인라인으로 넣어
  `SearchRegistryRecords`가 랭킹할 수 있게 하고, S3 위치는
  `skillDefinition._meta["com.amazonaws.agent-platform/skillSource"]` 에 넣습니다.
  `_meta`는 0.1.0 skill-definition 스키마가 벤더 확장용으로 예약한(reverse-DNS)
  지점이라, 스키마가 나중에 쓸 수 있는 키를 침범하지 않습니다.
- **Composer는 그 prefix를 그대로 붙입니다** (`_resolve_skills`). 여기서 S3에 쓰면
  업로드된 `references/`·`scripts/`를 `SKILL.md` 하나로 덮어쓰게 됩니다. 번들
  업로드가 생기기 전에 만들어진 인라인 레코드는 포인터가 없으므로, 예전처럼
  마크다운을 발행하는 경로를 폴백으로 남겨둡니다.

검증은 실패를 예외가 아니라 데이터로 돌려줍니다 (`SkillBundleInfo.errors`) — 업로드
한 번에 어긴 규칙을 전부 보여주기 위한 것입니다. 반면 prefix를 벗어나는 경로나
심볼릭 링크 같은 아카이브 안전성 위반은 치명적이라 즉시 중단합니다.

### 편집

`update_record`는 `UpdateRegistryRecord`를 호출합니다. mutable 필드는
`{"optionalValue": ...}` 래퍼를 쓰고, 건드리지 않을 필드는 파라미터 자체를
생략합니다. 부분 편집은 기존 `descriptor_content`에 병합하므로, 설명만 고쳐도
레코드를 채팅 가능하게 만드는 runtime ARN이 사라지지 않습니다. DEPRECATED는
terminal이라 서비스 계층에서 거부합니다.

## 🔄 계층별 역할

### 1. **Routes (Controller Layer)**
- HTTP 요청/응답 처리
- 입력 검증 (Pydantic 모델)
- Service 계층 호출
- **책임**: HTTP 프로토콜 처리만 담당

### 2. **Services (Business Logic Layer)**
- 비즈니스 로직 처리
- 여러 Repository 조합
- 트랜잭션 관리
- **특화**: Agentic AI의 경우 `StreamingService`로 스트리밍 로직 분리

### 3. **Repositories (Data Access Layer)**
- 데이터베이스 접근만 담당
- CRUD 작업
- 데이터 변환 (DB ↔ Domain Model)
- **책임**: 데이터 저장/조회만 담당

### 4. **Models (Domain Layer)**
- 도메인 모델 정의
- Pydantic 스키마
- 데이터 검증

### 5. **Agents (Agent Clients)**
- Agent 실행 클라이언트 (`AgentClient.execute_stream` 인터페이스)
- AgentCore Runtime (`AgentCoreClient`), Managed Agent Harness (`HarnessClient`)

## 🎯 Agentic AI 특화 고려사항

### 1. **Streaming 처리**
- `StreamingService`: 실시간 스트리밍 로직 분리
- SSE (Server-Sent Events) 처리
- 비동기 이벤트 스트리밍

### 2. **State 관리**
- Thread·Message 히스토리는 DynamoDB(`thread_repository`)에 저장
- 대화 복원은 런타임 쪽 AgentCore Memory가 담당하고, 서버는 `runtimeSessionId`를
  `thread_id`에서 파생해 넘김 (harness는 자체 managed memory 사용)
- 아티팩트는 버전 단위로 DynamoDB + S3에 저장

### 3. **Agent Client 패턴**
- `AgentCoreClient`, `HarnessClient`가 `AgentClient` 인터페이스(`execute_stream`)를 구현
- 두 클라이언트 모두 Strands 이벤트 형식으로 정규화해 내보내므로, 이후 계층은
  실행 경로를 구분하지 않음

## ✅ FastAPI Best Practices 준수

1. **의존성 주입 (DI)**: `core/dependencies.py`에서 중앙 관리
2. **계층 분리**: Routes → Services → Repositories
3. **도메인 모델 분리**: 도메인별로 모델 분리
4. **비동기 처리**: async/await 패턴 사용
5. **에러 처리**: HTTPException으로 일관된 에러 응답

## 🔍 왜 이 구조인가?

### ✅ 장점
1. **유지보수성**: 각 계층의 책임이 명확
2. **테스트 용이성**: 각 계층을 독립적으로 테스트 가능
3. **확장성**: 새로운 기능 추가가 쉬움
4. **재사용성**: Service 로직을 여러 Route에서 재사용 가능

### 🎯 Agentic AI에 적합한 이유
1. **복잡한 상태 관리**: Service 계층에서 중앙 관리
2. **스트리밍 처리**: 별도 Service로 분리하여 관리
3. **다양한 Agent**: Agent 계층으로 분리하여 교체 용이
4. **확장성**: 새로운 Agent 타입 추가가 쉬움

## MCP Apps 라우트 (`routes/mcp_apps.py`)

**SEP-1865 공식 MCP Apps 확장** 지원을 위한 릴레이 엔드포인트입니다.

### `/api/mcp-apps/resources/read` (POST)

MCP 서버의 `resources/read`를 호출하고, 리소스 `_meta.ui`(`csp`, `permissions`,
`prefersBorder`)와 함께 반환합니다. `ui://` 스킴이 아니면 400.

**인증**: `current_user` (login 필수)

**요청**:
```json
{
  "record_id": "레지스트리 record_id",
  "uri": "ui://app/index.html"
}
```

**응답**:
```json
{
  "success": true,
  "uri": "ui://app/index.html",
  "mime_type": "text/html;profile=mcp-app",
  "text": "...HTML content...",
  "ui_meta": {
    "csp": { "connectDomains": [], "resourceDomains": [] },
    "permissions": { "clipboardWrite": {} },
    "prefersBorder": true
  }
}
```

**과정**:
1. `record_id`로 레지스트리에서 MCP 서버 엔드포인트 조회 (`server.remotes[].url`)
2. 해당 서버에 `resources/read` 호출 (AgentCore 엔드포인트면 SigV4 자동 서명)
3. MIME이 정확히 `text/html;profile=mcp-app`인지 검증하고 `_meta.ui` 추출

### `/api/mcp-apps/tools/call` (POST)

iframe 내 앱이 MCP 도구를 호출합니다. 게이트웨이 접두사(`<target>___<tool>`)는 벗겨서
찾되, `visibility` 판정은 벗긴 뒤의 원본 툴로 합니다. `visibility`에 `"app"`이 없으면
403, 없는 툴이면 400.

**인증**: `current_user` (login 필수)

**요청**:
```json
{
  "record_id": "레지스트리 record_id",
  "tool_name": "도구 이름",
  "arguments": { "...도구 인자..." }
}
```

**응답** (`result`는 서버의 `CallToolResult` 그대로):
```json
{
  "success": true,
  "result": {
    "content": [{ "type": "text", "text": "...결과..." }],
    "structuredContent": { "...": "..." }
  }
}
```

### `/api/mcp-apps/tools/describe` (POST)

앱을 띄운 툴의 정의(MCP `Tool`)를 돌려줍니다. 호스트가 규격 `hostContext.toolInfo`로
View에 넘깁니다. visibility는 보지 않습니다 — 정의를 읽는 것은 호출이 아니고, 호출은
여전히 `/tools/call`의 검사를 거칩니다. 게이트웨이 접두사 이름은 벗겨서 찾되 `name`은
호스트가 부른 이름 그대로 돌려줍니다(앱이 그 이름으로 다시 호출하기 때문).

**요청**: `{ "record_id": "...", "tool_name": "..." }` → **응답**: `{ "success": true, "tool": { "name", "description", "inputSchema", "_meta" } }`

### 앱 → 대화·모델 (`ui/message`, `ui/update-model-context`)

- `ui/message`는 브라우저가 일반 사용자 메시지로 제출합니다(규격 SHOULD). 응답이 진행
  중이면 `isError`로 거부합니다.
- `ui/update-model-context`는 브라우저가 마지막 것만 기억해 다음 전송의
  `config.app_model_context`에 싣습니다. 서버(`streaming_service`)는 그것을
  **런타임/harness로 나가는 사본**의 마지막 human 메시지에만 덧붙이고
  (`mcp_core/model_context.py`), DynamoDB에 저장되는 대화에는 넣지 않습니다 —
  사용자가 쓴 말이 아니기 때문입니다.

### 앱 마운트 신호의 시점

`mcpApp` 이벤트는 툴 블록이 **시작될 때**(`contentBlockStart`의 toolUse) 나갑니다 —
런타임(`agent-runtime/main.py`)과 harness 경로(`_app_signal_for`) 모두. 규격의
"UI preloading": 호스트가 인자가 다 모이기 전에 앱을 띄우고
`ui/notifications/tool-input-partial`로 인자를 스트리밍할 수 있어야 합니다.

**설계 이유**: `/api/mcp/tools/call`은 admin 토큰을 요구하는데, 이유는 임의의 
subprocess를 실행할 수 있기 때문입니다. MCP Apps 앱 사용자는 일반 사용자이므로, 
저권한 별도 엔드포인트를 제공합니다.

## 📚 참고 자료

- [FastAPI Best Practices](https://fastapi.tiangolo.com/tutorial/)
- [Repository Pattern](https://martinfowler.com/eaaCatalog/repository.html)
- [Clean Architecture](https://blog.cleancoder.com/uncle-bob/2012/08/13/the-clean-architecture.html)
- [SEP-1865: MCP Apps Extension](https://github.com/modelcontextprotocol/ext-apps/blob/main/specification/2026-01-26/apps.mdx)

