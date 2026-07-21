#!/usr/bin/env bash
# ============================================================
# AWSome AI Gateway — 로컬 실행/테스트 스크립트
# ============================================================
# 전체 스택(Postgres+pgvector / Redis / Gateway Proxy / Admin API /
# Admin UI / 워커)을 docker compose 로 로컬에 띄웁니다.
#
# 사용법:
#   ./run.sh              # 핵심 서비스 기동 (기본)
#   ./run.sh up           # 핵심 서비스 기동
#   ./run.sh up --obs     # 관측 스택(Grafana 등)까지 함께 기동
#   ./run.sh up --tools   # 배포된 Tool Gateway 대시보드 연결(=admin-ui 재빌드)
#   ./run.sh down         # 중지 (데이터 보존)
#   ./run.sh reset        # 중지 + 볼륨/데이터 완전 삭제
#   ./run.sh logs [svc]   # 로그 tail (서비스명 생략 시 전체)
#   ./run.sh ps           # 상태 확인
#   ./run.sh health       # 헬스 엔드포인트 점검
# ============================================================
set -euo pipefail

cd "$(dirname "$0")"

# ---- 색상 ----
if [[ -t 1 ]]; then
  R='\033[0;31m'; G='\033[0;32m'; Y='\033[1;33m'; B='\033[0;34m'; N='\033[0m'
else
  R=''; G=''; Y=''; B=''; N=''
fi
info() { echo -e "${B}▶${N} $*"; }
ok()   { echo -e "${G}✓${N} $*"; }
warn() { echo -e "${Y}!${N} $*"; }
err()  { echo -e "${R}✗${N} $*" >&2; }

# ---- docker compose 명령 감지 ----
if docker compose version >/dev/null 2>&1; then
  DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  DC="docker-compose"
else
  err "docker compose 를 찾을 수 없습니다. Docker 를 설치하세요."
  exit 1
fi

# mock-vllm 은 ./mock 빌드 컨텍스트가 레포에 없어 제외.
# admin-chat-agent 는 AgentCore Runtime 에서 별도 호스팅되어 compose 대상 아님.
CORE_SERVICES=(postgres redis migration gateway-proxy admin-api admin-ui \
               cost-recorder-worker notification-worker scheduler)
OBS_SERVICES=(otel-collector prometheus loki tempo grafana)

# ------------------------------------------------------------
# .env 준비: 없으면 .env.example 복사 + VK 암호화 키 생성
# ------------------------------------------------------------
ensure_env() {
  if [[ ! -f .env ]]; then
    info ".env 가 없어 .env.example 에서 생성합니다."
    cp .env.example .env
    ok ".env 생성"
  fi
  # placeholder(전부 0) 이면 실제 키 주입
  if grep -q '^VIRTUAL_KEY_ENCRYPTION_KEY=0\{64\}$' .env; then
    local key
    key="$(openssl rand -hex 32)"
    # 이식성 위해 임시파일로 치환 (macOS/Linux sed 차이 회피)
    awk -v k="$key" '/^VIRTUAL_KEY_ENCRYPTION_KEY=/{print "VIRTUAL_KEY_ENCRYPTION_KEY=" k; next} {print}' .env > .env.tmp && mv .env.tmp .env
    ok "VIRTUAL_KEY_ENCRYPTION_KEY 생성·주입 (openssl rand -hex 32)"
  fi
}

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
  # 메트릭/트레이스 탭은 admin-ui 서버가 CloudWatch/X-Ray 를 직접 조회한다.
  # 컨테이너는 non-root(uid 1001) 라 host ~/.aws (0600) 를 못 읽으므로,
  # 자격증명을 정적 env 로 추출해 주입한다(profile/SSO 도 여기서 해석됨).
  if command -v aws >/dev/null 2>&1; then
    local creds
    if creds="$(aws configure export-credentials --format env 2>/dev/null)"; then
      eval "$creds"
      export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
      ok "AWS 자격증명 주입 — 메트릭/트레이스 탭 활성화"
    else
      warn "AWS 자격증명을 추출하지 못했습니다 — 메트릭/트레이스는 '연결할 수 없음' 으로 표시됩니다."
    fi
  else
    warn "aws CLI 없음 — 메트릭/트레이스는 '연결할 수 없음' 으로 표시됩니다."
  fi
  export COMPOSE_FILE="docker-compose.yml:docker-compose.tools.yml"
  ok "Tool Gateway override 활성화 (admin-ui 재빌드 필요 시 자동 수행)"
}

# ------------------------------------------------------------
# 포트 충돌 자동 회피: 사용 중이면 대체 포트를 찾아 env 로 export
# ------------------------------------------------------------
port_in_use() {
  local p="$1"
  # 우리 compose 컨테이너가 이미 쓰는 포트는 충돌로 보지 않음
  ss -ltn 2>/dev/null | grep -q ":${p} " || return 1
  return 0
}

pick_port() {
  # $1: 기본포트, $2: env 변수명 → 사용 중이면 +1 씩 올려 빈 포트 선택
  local base="$1" var="$2" p="$1"
  while port_in_use "$p"; do
    p=$((p+1))
  done
  if [[ "$p" != "$base" ]]; then
    warn "포트 ${base} 사용 중 → ${var}=${p} 로 매핑"
  fi
  export "$var"="$p"
}

resolve_ports() {
  # 이미 우리 스택이 떠서 그 포트를 쓰는 경우엔 재기동이므로 그대로 사용.
  # 순수 외부 점유일 때만 대체 포트로 이동.
  local running
  running="$($DC ps --services --status running 2>/dev/null || true)"
  if echo "$running" | grep -q .; then
    # 재기동: 기존 매핑 유지 (env 없으면 기본값 사용)
    info "이미 실행 중인 스택 감지 — 기존 포트 매핑 유지"
    return
  fi
  pick_port 5432 POSTGRES_PORT
  pick_port 6379 REDIS_PORT
  pick_port 8000 GATEWAY_PROXY_PORT
  pick_port 8080 ADMIN_API_PORT
  pick_port 3000 ADMIN_UI_PORT
}

# ------------------------------------------------------------
# 헬스 점검
# ------------------------------------------------------------
health() {
  local gp="${GATEWAY_PROXY_PORT:-8000}"
  local api="${ADMIN_API_PORT:-8080}"
  local ui="${ADMIN_UI_PORT:-3000}"
  echo ""
  info "헬스 엔드포인트 점검"
  check_http "gateway-proxy" "http://localhost:${gp}/health"
  check_http "admin-api"     "http://localhost:${api}/health"
  check_http "admin-ui"      "http://localhost:${ui}/api/health"
}

check_http() {
  local name="$1" url="$2" code
  for _ in $(seq 1 30); do
    code="$(curl -s -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || echo 000)"
    if [[ "$code" == "200" ]]; then
      ok "$(printf '%-14s' "$name") $url → 200"
      return 0
    fi
    sleep 2
  done
  err "$(printf '%-14s' "$name") $url → $code (타임아웃)"
  return 1
}

print_endpoints() {
  echo ""
  ok "스택이 기동되었습니다. 접속 정보:"
  echo -e "   ${G}Admin UI${N}      http://localhost:${ADMIN_UI_PORT:-3000}   (dev 로그인 활성)"
  echo -e "   ${G}Admin API${N}     http://localhost:${ADMIN_API_PORT:-8080}"
  echo -e "   ${G}Gateway Proxy${N} http://localhost:${GATEWAY_PROXY_PORT:-8000}"
  echo -e "   ${G}Postgres${N}      localhost:${POSTGRES_PORT:-5432}"
  echo -e "   ${G}Redis${N}         localhost:${REDIS_PORT:-6379}"
  if [[ "${WITH_OBS:-0}" == "1" ]]; then
    echo -e "   ${G}Grafana${N}       http://localhost:3001   (admin / admin)"
  fi
  if [[ -n "${COMPOSE_FILE:-}" && "$COMPOSE_FILE" == *docker-compose.tools.yml* ]]; then
    echo -e "   ${G}Tool Gateway${N}  http://localhost:${ADMIN_UI_PORT:-3000}/tools   (Tool 카탈로그)"
  fi
  echo ""
  echo -e "   로그: ${B}./run.sh logs${N}   상태: ${B}./run.sh ps${N}   중지: ${B}./run.sh down${N}"
}

# ------------------------------------------------------------
# 명령
# ------------------------------------------------------------
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

  local svcs=("${CORE_SERVICES[@]}")
  [[ "$with_obs" == "1" ]] && svcs+=("${OBS_SERVICES[@]}")

  info "빌드 및 기동: ${svcs[*]}"
  $DC up -d --build "${svcs[@]}"

  health || warn "일부 서비스 헬스체크 실패 — './run.sh logs' 로 확인하세요."
  print_endpoints
}

cmd_down()  { info "중지 (데이터 보존)"; $DC down; ok "중지 완료"; }
cmd_reset() { warn "중지 + 볼륨/데이터 완전 삭제"; $DC down -v; ok "초기화 완료"; }
cmd_logs()  { $DC logs -f --tail=100 "$@"; }
cmd_ps()    { $DC ps; }

main() {
  local cmd="${1:-up}"
  [[ $# -gt 0 ]] && shift || true
  case "$cmd" in
    up|"")     cmd_up "$@";;
    down)      cmd_down;;
    reset)     cmd_reset;;
    logs)      cmd_logs "$@";;
    ps|status) cmd_ps;;
    health)    resolve_ports; health;;
    -h|--help|help)
      sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//';;
    *)
      err "알 수 없는 명령: $cmd"
      echo "사용법: ./run.sh [up|down|reset|logs|ps|health]  (도움말: ./run.sh --help)"
      exit 1;;
  esac
}

main "$@"
