# UI — Agentic Text-to-SQL 콘솔 (Next.js + CopilotKit + AG-UI)

자연어 질의를 받아 AgentCore Runtime 의 오케스트레이터 에이전트로 전달하고, 에이전트의
스트리밍 응답(파이프라인 진행 · 텍스트 델타 · 도구 진행 · SQL · 결과)을 실시간 렌더링하는 웹앱입니다.

> 기능: 자연어 질의 → SSE 스트리밍(STEP 진행 바 + 텍스트 + 도구 칩 + SQL) → 결과.
> 결과 표는 synthesis 단계의 markdown 표를 CopilotChat 이 렌더합니다(아래 "결과 표시" 참고).
> 정보가 부족한 질의에는 clarification(재요청) 인터랙티브 폼을 띄웁니다
> (아래 "clarification 재요청 폼" 참고).

## 아키텍처 요약

```
브라우저 (CopilotKit v2 · CopilotKitProvider + CopilotChat)
   │  POST /api/copilotkit  (X-Session-Id 헤더)
   ▼
Next.js 서버 사이드 프록시 (route handler, Node 런타임)
   │  CopilotRuntime(v2) → AG-UI 에이전트(@ag-ui/client HttpAgent) → SigV4 서명 fetch
   ▼
AgentCore Runtime /invocations  (SSE, text/event-stream)
   Accept: text/event-stream
   X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: <세션 UUID>
```

- CopilotKit **v2 진입점**(`@copilotkit/runtime/v2`, `@copilotkit/react-core/v2`)을 사용합니다.
  v2 에서는 LLM 어댑터가 필요 없습니다 — AG-UI 에이전트가 곧 백엔드입니다.
- 오케스트레이터(BedrockAgentCoreApp, POST /invocations SSE)는 **표준 AG-UI wire-format
  (camelCase) 이벤트**(`data: {JSON}\n\n`)를 직접 방출합니다. 기본 경로는 `@ag-ui/client`
  `HttpAgent` 가 파싱하며, 문제 시 `raw-sse` 폴백 파서로 전환합니다.
- **브라우저는 AgentCore 를 직접 호출하지 않습니다.** 자격증명 노출·CORS 문제 때문에
  서버 사이드(프록시)가 SigV4 로 서명해 대신 호출합니다.
- 자격증명: ECS 배포 시 **task role**(설정 불필요), 로컬은 기본 credential chain(`AWS_PROFILE` 등).

### 이벤트 계약 (orchestrator 확정)

| 이벤트 | UI 처리 |
|---|---|
| `STEP_STARTED`/`STEP_FINISHED {stepName}` | 상단 파이프라인 진행 바 (`PipelineProgress`). stepName ∈ intent·schema_linking·sql_generation·execution·synthesis → 의도 분석·스키마 연결·SQL 생성·실행·결과 정리 |
| `TEXT_MESSAGE_START/CONTENT/END` | CopilotChat 자동 렌더 (markdown, GFM 표 포함) |
| `TOOL_CALL_START/ARGS/END` (`search_schema`, `run_sql`) | 상태 칩(스키마 검색 중…/SQL 실행 중…) + `run_sql` args.sql → SQL 코드블록 |
| `CUSTOM {name:"clarification_request"}` | clarification 재요청 폼(`ClarificationHost` + `ClarificationForm`). 아래 참고 |
| `RUN_STARTED`/`RUN_FINISHED`/`RUN_ERROR` | 실행 수명주기 |

- ⚠️ **`TOOL_CALL_RESULT` 는 방출되지 않습니다.** 쿼리 결과 표는 도구 결과가 아니라
  **synthesis 단계의 TEXT_MESSAGE(markdown 표)** 로 흐르며, CopilotChat 이 streamdown
  (remark-gfm)으로 자동 렌더합니다.
- 요청 규약: `threadId` = 브라우저 세션 UUID(고정, AgentCore Memory 세션 격리),
  `forwardedProps.actorId` = 사용자 식별자(기본 고정값 `demo-user`, `AGENT_ACTOR_ID` 로
  override — 사용자 인증 도입 시 Cognito sub 로 교체). actorId 는 프록시의 SigV4 fetch
  단계에서 body 에 주입됩니다.

## clarification 재요청 폼

orchestrator 는 질의에 필요한 정보가 부족하면 **AG-UI CUSTOM 이벤트**를 방출하고
`RUN_FINISHED` 로 스트림을 닫습니다. UI 는 이를 인라인 폼으로 렌더하고, 사용자 응답을
**동일 threadId 로 재실행**하며 전달합니다.

이벤트 value 스키마(확정 계약):

```json
{ "type": "CUSTOM", "name": "clarification_request",
  "value": {
    "interruptId": "…", "interruptName": "clarification",
    "question": "조회 기간을 알려주세요",
    "fields": [
      { "name": "period", "label": "기간", "type": "select",
        "options": ["최근 1개월", "최근 3개월", "전체"] },
      { "name": "from_to", "label": "직접 지정", "type": "date_range" },
      { "name": "note", "label": "비고", "type": "text" }
    ]
  } }
```

- field `type`: `select`(드롭다운) · `date_range`(시작·종료 두 date 인풋) · `text`.
- **재실행 응답**: `forwardedProps.clarificationResponse = { interruptId, values }` 로 전달합니다.
  - `values` 는 필드명→값 맵. `date_range` 값은 `{ "from": "YYYY-MM-DD", "to": "YYYY-MM-DD" }`,
    그 외는 문자열. 입력하지 않은 필드는 생략합니다.
  - **건너뛰기**는 빈 `values`(`{}`)로 재실행 → orchestrator 가 최선 추정으로 진행합니다.

### forwardedProps 전달 방식 (근거)

헤더/route.ts 우회 없이 **CopilotKit v2 의 네이티브 재실행 경로**를 사용합니다:

- `useCopilotKit().copilotkit.runAgent({ agent, forwardedProps })` 가 per-run `forwardedProps` 를
  provider properties 와 병합해 `agent.runAgent` 입력에 싣습니다(`@copilotkit/core` `runAgent`).
- `AbstractAgent.prepareRunAgentInput` 이 `forwardedProps` 를 `RunAgentInput` 에 포함하고,
  `HttpAgent` 가 전체 입력을 프록시 요청 본문(JSON)으로 직렬화합니다(`@ag-ui/client` 0.0.57).
- v2 런타임(`handle-run` → in-memory runner)이 파싱한 `input` 을 백엔드 에이전트로 **그대로**
  넘기므로 `forwardedProps` 가 AgentCore `/invocations` 까지 도달합니다.

CUSTOM 이벤트 수신은 `@ag-ui/client` 0.0.57 `AgentSubscriber` 의 **네이티브 `onCustomEvent`**
훅으로 처리합니다(`agent.subscribe({ onCustomEvent })`, PipelineProgress 의 STEP 구독과 동일 패턴).
같은 run 에서 동일 `interruptId` 중복 수신은 방어하며, 다음 `RUN_STARTED` 시 폼을 정리합니다.

## 환경 변수

`.env.example` 참고. 로컬은 `.env.local` 로 복사해 사용합니다.

| 변수 | 설명 |
|---|---|
| `AGENT_RUNTIME_ARN` | 오케스트레이터가 배포된 AgentCore Runtime ARN (필수) |
| `AWS_REGION` | 리전 (기본 `us-west-2`) |
| `AGENT_RUNTIME_QUALIFIER` | Runtime qualifier (기본 `DEFAULT`) |
| `AGENT_ACTOR_ID` | 사용자 식별자(forwardedProps.actorId). 기본 `demo-user` |
| `AGUI_ADAPTER` | 이벤트 어댑터: `agui`(기본) 또는 `raw-sse`(폴백) |

## 로컬 실행

```bash
# 1) 의존성 설치
npm install

# 2) 환경 변수 준비
cp .env.example .env.local
#   .env.local 에서 AGENT_RUNTIME_ARN 을 실제 값으로 채웁니다.

# 3) AWS 자격증명 (아래 중 하나)
export AWS_PROFILE=your-profile         # 권장
#   또는 AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN

# 4) 개발 서버
npm run dev                              # http://localhost:3000
```

기타 스크립트: `npm run build`(프로덕션 빌드) · `npm run lint` · `npm run typecheck` ·
`npm run format`.

## 이벤트 어댑터 교체 (폴백 전략)

오케스트레이터(Task #4)의 Strands AG-UI 통합은 community-maintained 이므로, 통합이
실패하면 자체 SSE 포맷으로 폴백할 수 있습니다. 이 경우를 위해 **이벤트 파싱 레이어를
한 파일로 분리**해 두었습니다.

- 교체 스위치: 환경 변수 `AGUI_ADAPTER`
  - `agui` (기본): `@ag-ui/client` 의 `HttpAgent` 가 표준 AG-UI SSE 파싱을 담당합니다.
  - `raw-sse`: `src/lib/raw-sse-agent.ts` 의 `RawSSEAgent` 가 동일한 표준 AG-UI SSE 를 직접
    파싱해 통과시킵니다. 비표준 스키마로 바뀌면 같은 파일의 `normalizeEvent()` 한 곳만 수정합니다.
- 두 어댑터 모두 동일한 SigV4 서명 fetch(`src/lib/sigv4-fetch.ts`)를 공유합니다.
- 교체 지점 파일: **`src/lib/agui-adapter.ts`** (어댑터 선택) + `src/lib/raw-sse-agent.ts`(파싱).

## 주요 파일

| 파일 | 역할 |
|---|---|
| `src/app/api/copilotkit/route.ts` | 서버 사이드 프록시 (CopilotKit runtime + 인증 훅 자리) |
| `src/lib/sigv4-fetch.ts` | AgentCore `/invocations` SigV4 서명 fetch |
| `src/lib/agentcore-endpoint.ts` | 엔드포인트 URL·세션 헤더 조립 |
| `src/lib/agui-adapter.ts` | **어댑터 교체 지점** (agui / raw-sse) |
| `src/lib/raw-sse-agent.ts` | 폴백 파서 (`normalizeEvent()` 교체 지점) |
| `src/components/Providers.tsx` | CopilotKitProvider(v2) + renderToolCalls 등록 |
| `src/components/T2SChat.tsx` | 채팅 본체 (CopilotChat v2, threadId 고정) |
| `src/components/PipelineProgress.tsx` | STEP_* 구독 → 파이프라인 진행 바 |
| `src/components/toolRenderers.tsx` | 와일드카드 도구 렌더러 (진행 칩 + run_sql SQL 코드블록) |
| `src/components/ToolProgressChip.tsx` | search_schema/run_sql → 한국어 상태 칩 |
| `src/components/ClarificationHost.tsx` | CUSTOM(clarification) 수신 + forwardedProps 재실행 트리거 |
| `src/components/ClarificationForm.tsx` | clarification value 스키마 → select/date_range/text 폼 렌더 |

## 인증

이 채팅 UI 는 로그인이 없어 인증을 강제하지 않습니다(알려진 한계 — 도구 평면 인가는 Gateway
앞단의 Cedar 가 서비스 계정 위임으로 처리). 프록시(`route.ts`)에 Cognito JWT 검증 훅
**자리(주석)** 를 마련해 두었으므로, 사용자 인증이 필요하면 여기에 JWT 검증·`sub` 전파를
추가합니다.

## Docker / 배포

ARM64 멀티 스테이지 빌드(Next standalone, non-root, 포트 3000):

```bash
# docker 기본, 실패 시 finch 폴백
docker build --platform linux/arm64 -t agentic-t2s-ui .
#   finch build --platform linux/arm64 -t agentic-t2s-ui .

docker run -p 3000:3000 \
  -e AGENT_RUNTIME_ARN=arn:aws:bedrock-agentcore:us-west-2:000000000000:runtime/... \
  -e AWS_REGION=us-west-2 \
  agentic-t2s-ui
```

ECS Fargate 에서는 자격증명 대신 **task role** 을 사용합니다(SigV4). `/api/health` 로
헬스체크합니다.
