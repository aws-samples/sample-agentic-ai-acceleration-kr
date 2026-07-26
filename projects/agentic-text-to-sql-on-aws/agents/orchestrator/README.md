# orchestrator — agentic Text-to-SQL 오케스트레이터 에이전트

Amazon Bedrock AgentCore Runtime 에 배포되는 Strands Agents 기반 오케스트레이터.
자연어 질의를 안전한 PostgreSQL `SELECT` 로 변환·실행·요약한다. AG-UI 프로토콜(SSE)로
UI(CopilotKit)와 스트리밍 통신한다.

## 파이프라인 (Strands Graph)

```
intent ──▶ schema_linking ──▶ sql_generation ──▶ execution ──▶ synthesis
                                     ▲                 │
                                     └──(rejected/error, 최대 N회)──┘
```

- **intent**: 질의 의도 해석. 모호하면 `request_clarification` 도구로 사용자에게 되물음(M2 clarification), 명확하면 최선 해석으로 진행
- **schema_linking**: `search_schema`(semantic-retrieval-mcp) 호출로 관련 테이블/컬럼 컨텍스트 수집
- **sql_generation**: 스키마 컨텍스트 기반 PostgreSQL SELECT 생성
- **execution**: `run_sql`(sql-execution-mcp) 호출. `rejected`/`error` 시 오류를 되먹여
  SQL 재생성 (self-correction, 기본 최대 3회) — Graph 조건부 엣지로 인코딩
- **synthesis**: 결과 표 기반 한국어(질의 언어) 자연어 요약

`ORCHESTRATOR_MODE=agent` 로 설정하면 단일 Strands Agent + 도구 2개 폴백 경로로 실행된다
(동일 시스템 프롬프트로 순서 유도).

## 로컬 개발

```bash
# 의존성 설치
uv sync

# 테스트 + 린트 (순수 로직만; LLM/MCP 는 mock 없이 미의존)
uv run pytest
uv run ruff check .
```

> 유닛 테스트는 순수 로직(설정 파싱, MCP 응답 파싱, self-correction 판단, 프롬프트 빌더,
> AG-UI 이벤트 변환, 요청 파싱, URL 조립, Graph 조건 함수, clarification reason/필드 정규화,
> interrupt→CUSTOM 변환, 세션 캐시 LRU, clarification 재개/만료 흐름)만 커버한다. 실제 Bedrock/MCP
> 호출은 통합 테스트(E2E)에서 검증한다.

## 환경 변수

`.env.example` 참고. 핵심:

| 변수 | 설명 |
|---|---|
| `SQL_MCP_ARN` | sql-execution-mcp Runtime ARN |
| `SEMANTIC_MCP_ARN` | semantic-retrieval-mcp Runtime ARN |
| `MEMORY_ID` | AgentCore Memory(STM) ID (비우면 메모리 비활성) |
| `MODEL_ID` | Bedrock Claude inference profile (기본 `us.anthropic.claude-sonnet-5`) |
| `AWS_REGION` | 리전 (기본 `us-west-2`) |
| `MAX_SQL_CORRECTIONS` | self-correction 최대 재시도 (기본 3) |
| `ORCHESTRATOR_MODE` | `graph`(기본) \| `agent`(폴백) |

## 컨테이너 빌드 (ARM64)

```bash
# uv.lock 이 있어야 함 (uv sync 로 생성/커밋)
docker build --platform linux/arm64 -t orchestrator:local .
# docker 실패 시 finch 폴백
finch build --platform linux/arm64 -t orchestrator:local .
```

AgentCore Runtime 규격: `0.0.0.0:8080`, `POST /invocations`(SSE), `GET /ping`, non-root.

## AG-UI 이벤트 포맷 (UI 담당 참고)

`POST /invocations` 는 SSE 로 AG-UI 프로토콜 wire-format(camelCase) 이벤트를 스트리밍한다.
`src/orchestrator/agui_events.py` 가 단일 정의 지점이다.

| 이벤트 | 필드 |
|---|---|
| `RUN_STARTED` | `threadId`, `runId` |
| `RUN_FINISHED` | `threadId`, `runId` |
| `RUN_ERROR` | `message`, (`code`) |
| `STEP_STARTED` / `STEP_FINISHED` | `stepName` (Graph 노드명: intent/schema_linking/sql_generation/execution/synthesis) |
| `TEXT_MESSAGE_START` | `messageId`, `role`(=assistant) |
| `TEXT_MESSAGE_CONTENT` | `messageId`, `delta` |
| `TEXT_MESSAGE_END` | `messageId` |
| `TOOL_CALL_START` | `toolCallId`, `toolCallName`(run_sql/search_schema/request_clarification) |
| `TOOL_CALL_ARGS` | `toolCallId`, `delta`(JSON 조각) |
| `TOOL_CALL_END` | `toolCallId` |
| `CUSTOM` | `name`(=`clarification_request`), `value`(아래 clarification 절 참고) |

요청 페이로드(RunAgentInput)에서 `threadId`→sessionId, `forwardedProps.actorId`(또는 `state.actorId`)
→actorId 를 추출해 AgentCore Memory 세션을 격리한다.

## clarification (재요청) 흐름 (M2)

질의가 모호하면(기간·지표 정의·대상 미지정 등) intent 단계가 `request_clarification` 도구를
호출해 Strands **interrupt** 를 발생시킨다. 실행이 그 지점에서 정지하고, 오케스트레이터는
`CUSTOM(clarification_request)` 이벤트로 UI 에 폼을 요청한 뒤 정상 `RUN_FINISHED` 로 스트림을
닫는다(연결을 열어둔 채 대기하지 않음). 사용자가 폼을 채워 보내면 **새 HTTP 요청**으로 재개된다.

```
[1차 요청] 모호한 질의
   └─ intent → request_clarification(interrupt)
        └─ CUSTOM(clarification_request) 방출 → RUN_FINISHED (스트림 종료)
                (실행 세션은 microVM 로컬 캐시에 보존)

[UI] clarification_request.value 로 폼 렌더 → 사용자 입력

[2차 요청] forwardedProps.clarificationResponse = {interruptId, values}
   └─ 캐시에서 같은 세션(Graph/Agent)을 찾아 interruptResponse 로 재개
        └─ schema_linking → sql_generation → execution → synthesis → RUN_FINISHED
```

- `CUSTOM(clarification_request)` 의 `value`:
  ```json
  {"interruptId": "<Interrupt.id>", "interruptName": "clarification",
   "question": "어느 기간의 매출인가요?",
   "fields": [{"name": "period", "label": "기간", "type": "select", "options": ["이번달", "지난달"]}]}
  ```
  `type` 은 `select` | `date_range` | `text` (그 외 값은 `text` 로 강등). `options` 는 `select` 에만.
- **재개 페이로드**: `forwardedProps.clarificationResponse = {"interruptId": str, "values": {<field name>: <값>}}`
  (`state.clarificationResponse` 도 수용). `request.py` 가 `ParsedRequest.clarification_response` 로 파싱.
- **세션/재개 전략**: 재개는 **같은 microVM 내 모듈 레벨 캐시**(session_id → 실행 세션)에 의존한다.
  interrupt 로 정지한 Graph/Agent 인스턴스와 열린 MCP 클라이언트를 캐시에 살려두었다가 2차 요청에서
  같은 인스턴스로 재개한다(LRU, 기본 상한 32).
- **한계 — 세션 매니저 영속화 미지원**: `AgentCoreMemorySessionManager` 는 multiagent(Graph) 세션
  영속화를 지원하지 않는다(`create/read/update_multi_agent` 미구현 → 상위 `SessionRepository` 가
  `NotImplementedError`). 따라서 Graph 에는 세션 매니저를 연결하지 않고 캐시 전략만 쓴다. 캐시 미스
  (microVM 교체 등)로 재개할 수 없으면 `RUN_ERROR` 대신 질문을 다시 하도록 안내하는 안내 메시지 +
  `RUN_FINISHED` 로 마무리한다(로그 코드 `CLARIFICATION_EXPIRED`). 단일 Agent 모드도 동일 CUSTOM 계약을 따른다.

## 보안 참고

- SQL 안전은 **sql-execution-mcp 도구 핸들러**(SQLGlot AST allow-list + read-only DB 사용자)가
  최종 방어선이다. 오케스트레이터는 시스템 프롬프트로 SELECT-only 를 유도할 뿐, 강제는 도구 계층이 담당한다.
- MCP 접속은 SigV4(`bedrock-agentcore` 서비스) 서명. DB 자격증명은 에이전트/LLM 컨텍스트에 노출되지 않는다.

## 범위 밖 (후속)

- Gateway 경유 도구 접근 + Cedar (M3) — 현재는 Runtime MCP 직접 연결
- Long-term memory / 개인화 (M2+)
- clarification 재개의 microVM 간 영속화 — 현재는 microVM 로컬 캐시(같은 VM 내)만 지원
