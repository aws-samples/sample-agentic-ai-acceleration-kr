# server (FastAPI 백엔드)

agent-platform의 백엔드입니다. 채팅/스트리밍, Agent Registry, Harness, Knowledge Base,
MCP / MCP Apps, Artifacts, Insights, 인증을 담당합니다. 전체 구성은 루트
[`README.md`](../README.md), 계층 구조는 [`ARCHITECTURE.md`](ARCHITECTURE.md) 참고.

- FastAPI + boto3, Python 3.13, 포트 8000
- Layered: `routes/`(컨트롤러) → `services/`(비즈니스 로직) →
  `repositories/`(DynamoDB) + `agents/`(AgentCore 클라이언트)
- 스트리밍(SSE)은 blocking 스트림 읽기를 워커 스레드로 넘겨 이벤트 루프를 잡지
  않습니다 (자세한 이유는 루트 README의 "스트리밍은 이벤트 루프를 잡지 않습니다").

## 사전 요구사항

- AWS 자격증명 (`aws configure` 또는 `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`)
- Bedrock 모델 접근 권한(콘솔에서 활성화). 채팅 실행은 AgentCore Runtime / Harness가
  담당하므로, 그쪽 리소스는 `infra/`로 배포하거나 기존 스택의 `terraform output`을 씁니다.

## 실행

루트의 `run.sh`가 backend·frontend·샌드박스를 함께 띄웁니다 (권장).

```bash
cd .. && ./run.sh backend    # backend만 (:8000)
```

직접 띄우려면 **`server/` 디렉터리에서** 실행합니다.

```bash
cd server
pip install -r requirements.txt
cp env.example .env          # 아래 환경 변수 채우기
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

- API: http://localhost:8000 · 문서: `/docs` · ReDoc: `/redoc`
- `uvicorn --reload`는 `.env`를 다시 읽지 않으니 env 변경 후에는 재시작합니다.

## 환경 변수

`env.example`을 복사해 채웁니다. 배포된 스택 값은
`cd infra/envs/standalone && terraform output`에서 가져옵니다.

기능은 env로 게이트됩니다 — 값이 없으면 그 기능만 꺼지고 서버는 뜹니다.

| 그룹 | 키 | 없을 때 |
| --- | --- | --- |
| 인증(Cognito) | `COGNITO_USER_POOL_ID`, `COGNITO_USER_POOL_CLIENT_ID`, `COGNITO_REGION` | 비밀번호 로그인 없음. Entra 도 없으면 인증 라우트 503. 로컬은 `AUTH_ENFORCED=false`로 우회(배포 금지) |
| 인증(Entra ID) | `OIDC_PROVIDERS_JSON`(terraform) 또는 `OIDC_ISSUER_URL`·`OIDC_CLIENT_ID`·`OIDC_REDIRECT_URIS`·`OIDC_ADMIN_GROUPS`, `USERS_TABLE` | SSO 버튼 없음. `USERS_TABLE` 없으면 Entra 사용자는 Insights 에 sub 로만 보임 |
| 스레드 | `DYNAMODB_THREADS_TABLE`, `AWS_REGION` | 필수 |
| 기본 런타임 | `AGENT_RUNTIME_ARN`, 선택 `AGENT_RUNTIME_DISCOVERY_REGIONS` | 레코드가 ARN 을 안 주는 채팅의 실행 대상이 없고, 선택기에 기본 에이전트가 고정되지 않음(`is_default` 레코드 없음) |
| 모델 오버라이드 | `ALLOWED_MODELS`(쉼표 구분 inference profile id) | 스레드 오버라이드의 모델 선택이 사라지고 `model_id` 를 보낸 턴은 403. 팀 허용 목록은 이 목록을 더 좁힐 수만 있다 |
| Registry | `AGENT_REGISTRY_ID`, `AP_USE_REGISTRY`(`auto`/`true`/`false`) | Registry 페이지가 안내 표시. 채팅 바인딩·harness 조합은 배포된 AgentCore 리소스로 폴백 |
| Harness | `HARNESS_EXECUTION_ROLE_ARN`, `SKILLS_BUCKET` | harness 조합 비활성(`/api/config` 의 `harnessEnabled` false) |
| Artifacts | `ARTIFACTS_BUCKET`, `ARTIFACTS_TABLE` | 화면에만 표시(저장·공유 비활성) |
| Knowledge | `KNOWLEDGE_BUCKET`, `KNOWLEDGE_TABLE`, `KB_SERVICE_ROLE_ARN`, `KB_GATEWAY_ROLE_ARN`; 선택 `KNOWLEDGE_SOURCE_BUCKETS`, `KNOWLEDGE_PLATFORM_SOURCE_BUCKET` | 넷 중 하나라도 없으면 `/api/knowledge` 501. 소스 버킷이 없으면 업로드형 KB 만 |
| Insights | `USAGE_TABLE`(필수), `PREFS_TABLE`, `USAGE_TIMEZONE`(기본 UTC), `PLATFORM`(기본 `bap`), `COLLECTOR_ENABLED`(일별 런타임 비용 수집기, 기본 off), `USAGE_LOG_GROUP`(세션별 런타임 비용), `MODEL_COST_AIP_ENABLED`(기본 off) | `USAGE_TABLE` 없으면 Insights 501. `PREFS_TABLE` 없으면 위젯 배치·메뉴 노출 설정이 저장되지 않음 |
| Harness 출력 | `HARNESS_OUTPUT_SWEEP`, `HARNESS_OUTPUT_ROOTS`, `HARNESS_OUTPUT_EXTENSIONS` 등 | 출력 sweep 비활성 |
| Browser 스크린샷 | `BROWSER_SCREENSHOT_BUCKET` | 스크린샷이 1시간 뒤 로드 실패 |

DynamoDB 테이블을 로컬/수동 생성: `python dynamodb_setup.py`.

## 라우트

| 라우터 | 경로 | 요약 |
| --- | --- | --- |
| `health` | `/` | 헬스 체크 |
| `config` | `/api/config` | 기능 플래그(`registryEnabled`·`harnessEnabled`·`allowedModels` 오버라이드 허용 모델). 공개 |
| `auth` | `/api/auth/*` | `config`(로그인 공급자 목록, 공개) · Cognito 로그인·리프레시(`USER_PASSWORD_AUTH`) · `session`(OIDC 로그인 직후 프로필·사용자 기록) |
| `threads` | `/threads/*` | 스레드 CRUD, 첨부 업로드, `POST /threads/{id}/runs/stream`(SSE 시작), `GET /threads/{id}/runs/stream`(진행 중 런에 재접속: 재생+라이브), `POST /threads/{id}/runs/cancel`(Stop), browser 스크린샷 |
| `registry` | `/api/registry/*` | Agent Registry 조회·검색·등록·편집·상태변경·sync, 스킬 번들 검증·업로드·버킷 관리 (쓰기 admin) |
| `harness` | `/api/harnesses/*` | 카탈로그·조회·조합(로그인), 수정(`PUT`)·삭제(admin) |
| `knowledge` | `/api/knowledge/*` | 사용자별 지식 베이스 |
| `mcp` | `/api/mcp/*` | MCP 서버/툴 테스트·호출 (admin — 임의 subprocess 가능), 레지스트리 MCP 레코드 툴 시험 호출 |
| `mcp_apps` | `/api/mcp-apps/*` | MCP Apps 릴레이 (로그인만) |
| `artifacts` | `/api/artifacts/*` | 산출물 조회·다운로드·공유·미리보기 |
| `insights` | `/api/insights/*` | 사용량·비용·평가·트레이스·요율(`rates`)·위젯 배치(`layout`). 집계(summary/telemetry/composition/users)는 admin, `me` 는 본인(또는 admin) |
| `settings` | `/api/settings/nav` | 일반 사용자에게 보일 메뉴 (읽기 로그인, 쓰기 admin) |

역할 강제는 서버에서 합니다. bearer 의 `iss` 가 Cognito 풀이면 access token 으로,
설정된 OIDC 공급자(Entra ID)면 id_token 으로 각자의 JWKS 에 검증하고, 관리자 판정은
Cognito 는 `cognito:groups` 의 `admin`, Entra 는 공급자 설정의 `admin_groups`/`admin_emails` 입니다.
조회 경로는 로그인만, 생성·편집·상태변경·삭제·sync와 harness·MCP Tools는 admin을
요구합니다. 스레드는 `owner_sub`에 귀속되어 소유자만 스트리밍합니다.

## MCP Apps (SEP-1865)

MCP 서버가 제공하는 대화형 UI 도구를 호스트가 렌더링하는 공식 확장입니다
([SEP-1865](https://github.com/modelcontextprotocol/ext-apps/blob/main/specification/2026-01-26/apps.mdx)).

- `POST /api/mcp-apps/resources/read` — `record_id` + `_meta.ui.resourceUri`로 HTML·CSP·권한 메타 조회
- `POST /api/mcp-apps/tools/call` — iframe 앱이 호출한 MCP 도구를 서버로 릴레이

두 엔드포인트는 **로그인만** 요구합니다(저권한). 에이전트용 `/api/mcp/tools/call`은
임의 subprocess 실행이 가능해 admin을 요구하므로 재사용하지 않습니다. 도구
`visibility`가 `["app"]`이면 에이전트 목록에서 숨기고 앱 안에서만 호출합니다(spec MUST).

## 검증

```bash
pytest tests/ -k <focused>   # focused 테스트로 검증
```

> 로컬 `.env`가 헐린 테이블/스택을 가리키면 전체 pytest가 collection에서 죽을 수
> 있습니다. 바뀐 부분만 focused 테스트로 돌리십시오.
