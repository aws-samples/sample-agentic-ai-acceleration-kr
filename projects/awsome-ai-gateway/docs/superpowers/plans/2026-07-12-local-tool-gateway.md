# 로컬 admin-ui → 배포된 Tool Gateway 연결 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `./run.sh up --tools`로 띄운 로컬 admin-ui가 이미 배포된 AgentCore Tool Gateway의 Tool 카탈로그 조회·호출을 하도록 한다.

**Architecture:** `dashboard.generated.env`(provision 스크립트 산출물)를 단일 진실 소스로 삼아, `run.sh`가 `--tools`일 때 이를 source하고 `docker-compose.tools.yml` override를 얹는다. override는 admin-ui의 build args(NEXT_PUBLIC_* 빌드타임 인라인용)와 runtime env(서버 route의 Cognito M2M + gateway 호출용)를 주입한다. Dockerfile builder 스테이지는 그 build args를 받도록 ARG를 선언한다.

**Tech Stack:** Bash, Docker Compose(v2), Next.js(standalone build), 기존 admin-ui TypeScript route(변경 없음).

## Global Constraints

- 기존 `./run.sh up`(플래그 없음) 동작은 절대 회귀 없어야 함 — Tool Gateway는 opt-in.
- override의 모든 변수는 `${VAR:-}` 기본 빈 문자열 — generated.env 없이도 compose 파싱이 깨지지 않아야 함.
- 파일 경로는 리포 루트(`projects/awsome-ai-gateway/`) 기준.
- 커밋 메시지 말미: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`

---

### Task 1: `.gitignore`에 dashboard.generated.env 추가 (보안 선행)

`dashboard.generated.env`는 Cognito client secret 평문을 담는데 현재 git-ignore되지 않는다. 후속 작업 전에 먼저 막는다.

**Files:**
- Modify: `.gitignore`

**Interfaces:**
- Consumes: 없음
- Produces: 없음

- [ ] **Step 1: 현재 추적 상태 확인**

Run: `git check-ignore deployment/tool-gateway/dashboard.generated.env; echo "exit=$?"`
Expected: 아무 출력 없이 `exit=1` (= 무시되지 않음)

- [ ] **Step 2: `.gitignore` 끝에 규칙 추가**

`.gitignore` 파일 맨 끝에 아래 블록을 추가:

```gitignore
# Tool Gateway provisioning 산출물 (Cognito client secret 평문 포함 — 커밋 금지)
deployment/tool-gateway/dashboard.generated.env
deployment/tool-gateway/.env.key
```

- [ ] **Step 3: 무시 적용 확인**

Run: `git check-ignore deployment/tool-gateway/dashboard.generated.env && echo OK`
Expected: 경로 출력 + `OK`

- [ ] **Step 4: 이미 스테이징/추적 중이면 인덱스에서 제거**

Run: `git rm --cached deployment/tool-gateway/dashboard.generated.env deployment/tool-gateway/.env.key 2>/dev/null; git status --short deployment/tool-gateway/ | head`
Expected: 파일이 이전에 추적된 적 없으면 아무 변화 없음(에러 무시). 추적됐다면 `D` 표시.

- [ ] **Step 5: 커밋**

```bash
git add .gitignore
git commit -m "chore: gitignore Tool Gateway generated.env (contains plaintext secret)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Dockerfile builder 스테이지에 NEXT_PUBLIC ARG 추가

`NEXT_PUBLIC_*`는 `next build` 시점에 브라우저 번들로 인라인되므로 build arg로 받아야 한다.

**Files:**
- Modify: `admin-ui/Dockerfile:10-15`

**Interfaces:**
- Consumes: docker build args `NEXT_PUBLIC_TOOL_GATEWAY_{ENABLED,URL,ID,REGION}` (Task 3의 compose override가 전달)
- Produces: 이 값이 인라인된 브라우저 번들

- [ ] **Step 1: builder 스테이지에 ARG/ENV 삽입**

`admin-ui/Dockerfile`의 아래 블록:

```dockerfile
# Stage 2: builder
FROM public.ecr.aws/docker/library/node:20-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build
```

을 다음으로 교체:

```dockerfile
# Stage 2: builder
FROM public.ecr.aws/docker/library/node:20-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
# Tool Gateway 대시보드 게이트: NEXT_PUBLIC_* 는 next build 시점에 브라우저 번들로
# 인라인되므로 build arg 로 받는다. 비어 있으면 대시보드는 비활성(기존 동작).
ARG NEXT_PUBLIC_TOOL_GATEWAY_ENABLED=false
ARG NEXT_PUBLIC_TOOL_GATEWAY_URL=
ARG NEXT_PUBLIC_TOOL_GATEWAY_ID=
ARG NEXT_PUBLIC_TOOL_GATEWAY_REGION=us-east-1
ENV NEXT_PUBLIC_TOOL_GATEWAY_ENABLED=$NEXT_PUBLIC_TOOL_GATEWAY_ENABLED \
    NEXT_PUBLIC_TOOL_GATEWAY_URL=$NEXT_PUBLIC_TOOL_GATEWAY_URL \
    NEXT_PUBLIC_TOOL_GATEWAY_ID=$NEXT_PUBLIC_TOOL_GATEWAY_ID \
    NEXT_PUBLIC_TOOL_GATEWAY_REGION=$NEXT_PUBLIC_TOOL_GATEWAY_REGION
RUN npm run build
```

- [ ] **Step 2: build args 없이 빌드해도 깨지지 않는지 확인(회귀)**

Run: `docker build -q -t admin-ui-regtest admin-ui >/dev/null && echo BUILD_OK`
Expected: `BUILD_OK` (ARG 기본값으로 기존과 동일하게 빌드됨)

- [ ] **Step 3: 커밋**

```bash
git add admin-ui/Dockerfile
git commit -m "build(admin-ui): accept NEXT_PUBLIC Tool Gateway build args

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: `docker-compose.tools.yml` override 생성

admin-ui에 build args + runtime env를 덧씌우는 override. 새 서비스는 만들지 않는다(admin-ui만 수정).

**Files:**
- Create: `docker-compose.tools.yml`

**Interfaces:**
- Consumes: 셸 환경변수 `NEXT_PUBLIC_TOOL_GATEWAY_*`, `TOOL_GATEWAY_ARN`, `COGNITO_TOOL_*` (Task 4의 run.sh가 source해서 export)
- Produces: `-f docker-compose.yml -f docker-compose.tools.yml`로 병합 가능한 override

- [ ] **Step 1: 파일 작성**

`docker-compose.tools.yml`:

```yaml
# ============================================================
# Tool Gateway override (opt-in) — ./run.sh up --tools 로만 사용
# ============================================================
# 이미 배포된 AgentCore Tool Gateway 에 로컬 admin-ui 를 연결한다.
# 값은 deployment/tool-gateway/dashboard.generated.env 에서 run.sh 가 source 한다.
# 모든 변수는 기본 빈 문자열이라 값이 없어도 compose 파싱은 깨지지 않는다.
services:
  admin-ui:
    build:
      context: ./admin-ui
      args:
        # NEXT_PUBLIC_* 는 빌드타임 인라인 → 브라우저 번들 게이트용
        NEXT_PUBLIC_TOOL_GATEWAY_ENABLED: ${NEXT_PUBLIC_TOOL_GATEWAY_ENABLED:-false}
        NEXT_PUBLIC_TOOL_GATEWAY_URL: ${NEXT_PUBLIC_TOOL_GATEWAY_URL:-}
        NEXT_PUBLIC_TOOL_GATEWAY_ID: ${NEXT_PUBLIC_TOOL_GATEWAY_ID:-}
        NEXT_PUBLIC_TOOL_GATEWAY_REGION: ${NEXT_PUBLIC_TOOL_GATEWAY_REGION:-us-east-1}
    environment:
      # 서버 사이드 route(/api/tools/*)도 NEXT_PUBLIC_* 를 읽는다
      NEXT_PUBLIC_TOOL_GATEWAY_ENABLED: ${NEXT_PUBLIC_TOOL_GATEWAY_ENABLED:-false}
      NEXT_PUBLIC_TOOL_GATEWAY_URL: ${NEXT_PUBLIC_TOOL_GATEWAY_URL:-}
      NEXT_PUBLIC_TOOL_GATEWAY_ID: ${NEXT_PUBLIC_TOOL_GATEWAY_ID:-}
      NEXT_PUBLIC_TOOL_GATEWAY_REGION: ${NEXT_PUBLIC_TOOL_GATEWAY_REGION:-us-east-1}
      TOOL_GATEWAY_ARN: ${TOOL_GATEWAY_ARN:-}
      # 서버 route 가 Cognito M2M 토큰 발급 → gateway 호출에 사용
      COGNITO_TOOL_TOKEN_ENDPOINT: ${COGNITO_TOOL_TOKEN_ENDPOINT:-}
      COGNITO_TOOL_M2M_CLIENT_ID: ${COGNITO_TOOL_M2M_CLIENT_ID:-}
      COGNITO_TOOL_M2M_CLIENT_SECRET: ${COGNITO_TOOL_M2M_CLIENT_SECRET:-}
      COGNITO_TOOL_M2M_SCOPE: ${COGNITO_TOOL_M2M_SCOPE:-agentcore/invoke}
```

- [ ] **Step 2: override 병합이 유효한지 검증(값 주입 상태)**

Run:
```bash
NEXT_PUBLIC_TOOL_GATEWAY_ENABLED=true \
NEXT_PUBLIC_TOOL_GATEWAY_URL=https://example/mcp \
docker compose -f docker-compose.yml -f docker-compose.tools.yml config \
  | grep -A2 'NEXT_PUBLIC_TOOL_GATEWAY_URL' | head
```
Expected: `NEXT_PUBLIC_TOOL_GATEWAY_URL: https://example/mcp` 가 build args와 environment 양쪽에 나타남. config 명령이 에러 없이 종료.

- [ ] **Step 3: 값 없이도 파싱되는지 검증(회귀)**

Run: `docker compose -f docker-compose.yml -f docker-compose.tools.yml config >/dev/null && echo CONFIG_OK`
Expected: `CONFIG_OK` (빈 기본값으로 파싱 성공)

- [ ] **Step 4: 커밋**

```bash
git add docker-compose.tools.yml
git commit -m "feat: docker-compose.tools.yml override wiring admin-ui to deployed Tool Gateway

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: `run.sh`에 `--tools` 플래그 배선

`--tools`일 때 generated.env를 source하고 override 파일을 compose에 얹는다. `COMPOSE_FILE` 환경변수(docker compose 네이티브, 콜론 구분)를 써서 개별 `$DC` 호출을 건드리지 않는다.

**Files:**
- Modify: `run.sh` (`cmd_up`, `print_endpoints`, help 텍스트)

**Interfaces:**
- Consumes: `deployment/tool-gateway/dashboard.generated.env`
- Produces: `COMPOSE_FILE` export, `WITH_TOOLS` 상태

- [ ] **Step 1: generated.env 로더 함수 추가**

`run.sh`의 `ensure_env()` 함수 정의 바로 뒤(66번째 줄 `}` 다음)에 삽입:

```bash
# ------------------------------------------------------------
# Tool Gateway: 배포된 인프라 값(dashboard.generated.env)을 로드하고
# override compose 파일을 COMPOSE_FILE 에 얹는다.
# ------------------------------------------------------------
TOOL_ENV_FILE="deployment/tool-gateway/dashboard.generated.env"
enable_tools() {
  if [[ ! -f "$TOOL_ENV_FILE" ]]; then
    err "Tool Gateway 설정 파일이 없습니다: $TOOL_ENV_FILE"
    err "먼저 배포하세요: deployment/scripts/provision_tool_gateway.sh deploy"
    exit 1
  fi
  info "Tool Gateway 설정 로드: $TOOL_ENV_FILE"
  set -a; source "$TOOL_ENV_FILE"; set +a
  if [[ -z "${NEXT_PUBLIC_TOOL_GATEWAY_URL:-}" ]]; then
    warn "NEXT_PUBLIC_TOOL_GATEWAY_URL 이 비어 있음 — 대시보드가 Unavailable 로 보일 수 있습니다."
  fi
  export COMPOSE_FILE="docker-compose.yml:docker-compose.tools.yml"
  ok "Tool Gateway override 활성화 (admin-ui 재빌드 필요 시 자동 수행)"
}
```

- [ ] **Step 2: `cmd_up`에서 `--tools` 파싱 + 호출**

`cmd_up()`의 아래 블록:

```bash
cmd_up() {
  local with_obs=0
  for a in "$@"; do
    [[ "$a" == "--obs" || "$a" == "--observability" ]] && with_obs=1
  done
  WITH_OBS="$with_obs"

  ensure_env
  resolve_ports
```

을 다음으로 교체:

```bash
cmd_up() {
  local with_obs=0 with_tools=0
  for a in "$@"; do
    [[ "$a" == "--obs" || "$a" == "--observability" ]] && with_obs=1
    [[ "$a" == "--tools" ]] && with_tools=1
  done
  WITH_OBS="$with_obs"

  ensure_env
  [[ "$with_tools" == "1" ]] && enable_tools
  resolve_ports
```

- [ ] **Step 3: `print_endpoints`에 Tool Gateway 안내 추가**

`print_endpoints()` 안의 obs 블록:

```bash
  if [[ "${WITH_OBS:-0}" == "1" ]]; then
    echo -e "   ${G}Grafana${N}       http://localhost:3001   (admin / admin)"
  fi
```

바로 뒤에 추가:

```bash
  if [[ -n "${COMPOSE_FILE:-}" && "$COMPOSE_FILE" == *docker-compose.tools.yml* ]]; then
    echo -e "   ${G}Tool Gateway${N}  http://localhost:${ADMIN_UI_PORT:-3000}/tools   (Tool 카탈로그)"
  fi
```

- [ ] **Step 4: help 텍스트에 `--tools` 추가**

`run.sh` 상단 주석의 사용법 블록에서:

```bash
#   ./run.sh up --obs     # 관측 스택(Grafana 등)까지 함께 기동
```

바로 아래 줄에 추가:

```bash
#   ./run.sh up --tools   # 배포된 Tool Gateway 대시보드 연결(=admin-ui 재빌드)
```

- [ ] **Step 5: 문법 검사**

Run: `bash -n run.sh && echo SYNTAX_OK`
Expected: `SYNTAX_OK`

- [ ] **Step 6: `--tools`인데 generated.env 없을 때 에러 확인**

Run: `mv deployment/tool-gateway/dashboard.generated.env /tmp/gen.env.bak 2>/dev/null; ./run.sh health --tools 2>&1 | head; :`

주의: `health`는 `--tools`를 파싱하지 않으므로 이 스텝은 `cmd_up` 경로만 검증한다. 대신 아래로 검증:

Run:
```bash
mv deployment/tool-gateway/dashboard.generated.env /tmp/gen.env.bak 2>/dev/null
bash -c 'source run.sh 2>/dev/null; enable_tools' 2>&1 | head -3; echo "exit=${PIPESTATUS[0]}"
mv /tmp/gen.env.bak deployment/tool-gateway/dashboard.generated.env 2>/dev/null
```
Expected: "Tool Gateway 설정 파일이 없습니다" 에러 출력.
(참고: `source run.sh`는 `main "$@"`를 인자 없이 실행해 `cmd_up`로 들어가므로, 검증이 번거로우면 이 스텝은 육안 코드리뷰로 대체 가능. 핵심은 `enable_tools`의 파일 부재 분기다.)

- [ ] **Step 7: help 출력 확인**

Run: `./run.sh --help | grep -A1 'tools'`
Expected: `--tools` 안내 줄이 출력됨

- [ ] **Step 8: 커밋**

```bash
git add run.sh
git commit -m "feat(run.sh): --tools flag connects admin-ui to deployed Tool Gateway

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: 엔드투엔드 검증 + 문서 (README)

**Files:**
- Modify: `README.md` (또는 `run.sh` 사용법이 이미 문서화된 위치 — 실제 위치 확인 후)

**Interfaces:**
- Consumes: Task 1-4 전부
- Produces: 없음

- [ ] **Step 1: 배포된 gateway 전제 확인**

Run: `test -f deployment/tool-gateway/dashboard.generated.env && grep -c NEXT_PUBLIC_TOOL_GATEWAY_URL deployment/tool-gateway/dashboard.generated.env`
Expected: `1` (배포 산출물 존재). 없으면 `provision_tool_gateway.sh deploy` 선행 필요 — 실행자에게 안내.

- [ ] **Step 2: `--tools`로 기동**

Run: `./run.sh up --tools`
Expected: admin-ui 재빌드 후 헬스 통과, 엔드포인트 안내에 `Tool Gateway ... /tools` 줄 표시.

- [ ] **Step 3: 카탈로그 API 확인**

Run: `curl -s "http://localhost:${ADMIN_UI_PORT:-3000}/api/tools/list" | head -c 400`
Expected: `"status":"Complete"` 와 `tools` 배열(엔진 목록). `"Disabled"`가 아니어야 함.
(주의: 이 route는 인증이 필요할 수 있음 — 401이면 브라우저로 로그인 후 `/tools` 페이지에서 육안 확인.)

- [ ] **Step 4: 회귀 — 플래그 없이 기동 시 비활성 확인**

Run: `./run.sh up && curl -s "http://localhost:${ADMIN_UI_PORT:-3000}/api/tools/list" | head -c 200`
Expected: `"status":"Disabled"` (기존 동작 유지).

- [ ] **Step 5: README에 사용법 한 줄 추가**

`README.md`에서 `./run.sh` 사용법이 언급된 섹션을 찾아(`grep -n 'run.sh' README.md`), 로컬 실행 안내에 다음 한 줄 추가:

```markdown
- `./run.sh up --tools` — 이미 배포된 Tool Gateway(AgentCore) 대시보드를 로컬 admin-ui에 연결. 사전에 `deployment/scripts/provision_tool_gateway.sh deploy` 필요.
```

`README.md`에 해당 섹션이 없으면 이 스텝은 건너뛰고 `run.sh --help`만으로 문서화된 것으로 간주.

- [ ] **Step 6: 커밋**

```bash
git add README.md
git commit -m "docs: document ./run.sh up --tools for local Tool Gateway

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review 결과

- **Spec coverage:** compose override(Task 3), Dockerfile ARG(Task 2), run.sh --tools(Task 4), .gitignore 보안(Task 1), 검증(Task 5) — spec 전 항목 매핑됨.
- **범위 밖 준수:** 트레이스/메트릭 route, Lambda 로컬 실행, gateway 프로비저닝은 손대지 않음.
- **회귀 방지:** Task 2 Step 2, Task 3 Step 3, Task 4 Step 5, Task 5 Step 4에서 명시 검증.
- **타입/이름 일관성:** `enable_tools`, `TOOL_ENV_FILE`, `COMPOSE_FILE`, 변수명이 override YAML의 키와 정확히 일치.
