#!/usr/bin/env bash
#
# 로컬 개발 실행 스크립트 — backend(FastAPI, :8000) + frontend(Next.js, :3000)을 함께 띄웁니다.
#
# MCP Apps 샌드박스는 호스트와 다른 출처에서 서빙돼야 하므로 (규격 MUST), 기본값으로
# :3001에서 두 번째 dev 서버를 띄웁니다 — 포트가 다르면 브라우저 출처가 다르다.
# 공식 예제도 host 8080 / sandbox 8081로 같은 방식을 쓴다.
#
#   ./run.sh              # backend + frontend + sandbox-dev 동시 실행
#   ./run.sh backend      # backend만
#   ./run.sh frontend     # frontend만
#   ./run.sh --install    # 의존성 설치 후 전체 실행
#
# 환경변수로 포트 변경 가능: SERVER_PORT=8001 WEB_PORT=3001 SANDBOX_PORT=3002 ./run.sh
#
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_DIR="$ROOT_DIR/server"
WEB_DIR="$ROOT_DIR/web"

SERVER_PORT="${SERVER_PORT:-8000}"
WEB_PORT="${WEB_PORT:-3000}"
SANDBOX_PORT="${SANDBOX_PORT:-3001}"
PYTHON_BIN="${PYTHON_BIN:-python3.13}"

TARGET="all"
DO_INSTALL=0

for arg in "$@"; do
  case "$arg" in
    all)                 TARGET="all" ;;
    backend|server|api)  TARGET="backend" ;;
    frontend|web|ui)     TARGET="frontend" ;;
    --install|-i)        DO_INSTALL=1 ;;
    -h|--help)           sed -n '3,10p' "${BASH_SOURCE[0]}" | sed 's/^#\s\{0,1\}//'; exit 0 ;;
    *) echo "알 수 없는 인자: $arg (사용법: ./run.sh --help)" >&2; exit 1 ;;
  esac
done

# ── 로그 유틸 ────────────────────────────────────────────────────────────────
C_RESET=$'\033[0m'; C_BLUE=$'\033[36m'; C_GREEN=$'\033[32m'
C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'

info()  { printf '%s[run]%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
warn()  { printf '%s[run]%s %s\n' "$C_YELLOW" "$C_RESET" "$*"; }
error() { printf '%s[run]%s %s\n' "$C_RED" "$C_RESET" "$*" >&2; }

# 자식 프로세스 로그에 컴포넌트 접두어를 붙입니다.
prefix_logs() {
  local label="$1" color="$2"
  while IFS= read -r line; do
    printf '%s[%s]%s %s\n' "$color" "$label" "$C_RESET" "$line"
  done
}

# ── 종료 처리 ────────────────────────────────────────────────────────────────
PIDS=()

# uvicorn reloader / next dev 는 자식 프로세스를 띄우므로 트리 전체를 종료합니다.
kill_tree() {
  local pid="$1" child
  for child in $(pgrep -P "$pid" 2>/dev/null || true); do
    kill_tree "$child"
  done
  kill -TERM "$pid" 2>/dev/null || true
}

cleanup() {
  trap - INT TERM EXIT
  info "종료 중..."
  local pid
  for pid in "${PIDS[@]:-}"; do
    [[ -n "$pid" ]] && kill_tree "$pid"
  done
  wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

port_in_use() {
  local port="$1"
  if command -v lsof >/dev/null 2>&1; then
    lsof -iTCP:"$port" -sTCP:LISTEN -t >/dev/null 2>&1
  elif command -v ss >/dev/null 2>&1; then
    ss -ltn "sport = :$port" 2>/dev/null | grep -q LISTEN
  else
    return 1
  fi
}

check_port() {
  local port="$1" name="$2"
  if port_in_use "$port"; then
    error "$name 포트 $port 이(가) 이미 사용 중입니다. 기존 프로세스를 종료하거나 ${3}=<port> 로 변경하세요."
    exit 1
  fi
}

# ── 사전 점검 ────────────────────────────────────────────────────────────────
check_backend_prereqs() {
  command -v "$PYTHON_BIN" >/dev/null 2>&1 || {
    error "$PYTHON_BIN 을 찾을 수 없습니다. PYTHON_BIN=python3 ./run.sh 처럼 지정하세요."
    exit 1
  }
  if [[ ! -f "$SERVER_DIR/.env" ]]; then
    warn "server/.env 가 없습니다. server/env.example 를 복사해 값을 채워주세요:"
    warn "  cp server/env.example server/.env"
  fi
  if ! "$PYTHON_BIN" -c 'import uvicorn, fastapi' >/dev/null 2>&1; then
    error "backend 의존성이 없습니다. ./run.sh --install 또는 다음을 실행하세요:"
    error "  $PYTHON_BIN -m pip install -r server/requirements.txt"
    exit 1
  fi
}

check_frontend_prereqs() {
  command -v yarn >/dev/null 2>&1 || { error "yarn 을 찾을 수 없습니다. npm i -g yarn 으로 설치하세요."; exit 1; }
  if [[ ! -d "$WEB_DIR/node_modules" ]]; then
    error "web/node_modules 가 없습니다. ./run.sh --install 또는 'cd web && yarn install' 을 실행하세요."
    exit 1
  fi
}

install_deps() {
  if [[ "$TARGET" != "frontend" ]]; then
    info "backend 의존성 설치 (pip install -r server/requirements.txt)"
    "$PYTHON_BIN" -m pip install -q -r "$SERVER_DIR/requirements.txt"
  fi
  if [[ "$TARGET" != "backend" ]]; then
    info "frontend 의존성 설치 (yarn install)"
    (cd "$WEB_DIR" && yarn install --silent)
  fi
}

# ── 실행 ─────────────────────────────────────────────────────────────────────
start_backend() {
  check_port "$SERVER_PORT" "backend" SERVER_PORT
  info "backend 시작 → http://localhost:$SERVER_PORT (docs: /docs)"
  (
    cd "$SERVER_DIR"
    "$PYTHON_BIN" -m uvicorn main:app --reload \
      --host 0.0.0.0 --port "$SERVER_PORT" 2>&1 | prefix_logs server "$C_BLUE"
  ) &
  PIDS+=("$!")
}

start_frontend() {
  check_port "$WEB_PORT" "frontend" WEB_PORT
  info "frontend 시작 → http://localhost:$WEB_PORT"
  (
    cd "$WEB_DIR"
    # Next.js 미들웨어가 /api/* 를 backend 로 넘깁니다 (src/middleware.ts).
    BACKEND_ORIGIN="${BACKEND_ORIGIN:-http://localhost:$SERVER_PORT}" \
      SANDBOX_ORIGIN="${SANDBOX_ORIGIN:-http://localhost:$SANDBOX_PORT}" \
      yarn next dev --turbopack --port "$WEB_PORT" 2>&1 | prefix_logs web "$C_GREEN"
  ) &
  PIDS+=("$!")
}

start_sandbox_dev() {
  check_port "$SANDBOX_PORT" "sandbox" SANDBOX_PORT
  info "sandbox 시작 → http://localhost:$SANDBOX_PORT (다른 출처로 샌드박스 서빙)"
  (
    cd "$WEB_DIR"
    # 샌드박스는 호스트와 다른 출처에서 서빙돼야 합니다 (규격 MUST).
    # 로컬 개발에서는 포트가 다르면 브라우저 출처가 다르므로,
    # 같은 Next.js 앱을 다른 포트에서 띄웁니다.
    #
    # NEXT_DIST_DIR 이 필요한 이유: Next 는 `.next/dev/lock` 으로 빌드 디렉터리를
    # 선점하고, 락이 포트가 아니라 디렉터리 단위입니다. 그래서 그냥 두면 두 번째
    # dev 서버가 "Another next dev server is already running" 으로 죽고 — 샌드박스
    # 출처가 없으니 MCP App 이 아예 로드되지 않습니다.
    NEXT_DIST_DIR=".next-sandbox" \
      BACKEND_ORIGIN="${BACKEND_ORIGIN:-http://localhost:$SERVER_PORT}" \
      yarn next dev --turbopack --port "$SANDBOX_PORT" 2>&1 | prefix_logs sandbox "$C_YELLOW"
  ) &
  PIDS+=("$!")
}

if [[ "$DO_INSTALL" -eq 1 ]]; then
  install_deps
fi

case "$TARGET" in
  backend)  check_backend_prereqs ;;
  frontend) check_frontend_prereqs ;;
  all)      check_backend_prereqs; check_frontend_prereqs ;;
esac

case "$TARGET" in
  backend)  start_backend ;;
  frontend) start_frontend ;;
  all)
    start_backend
    sleep 1   # backend 로그가 먼저 나오도록 살짝 대기
    start_frontend
    sleep 0.5 # 포트 충돌 방지
    start_sandbox_dev
    ;;
esac

info "중지: Ctrl+C"
wait
