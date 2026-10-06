#!/usr/bin/env bash
#
# 인프라 배포 TUI 실행 스크립트.
#
#   ./install.sh                    # 배포 TUI
#   ./install.sh --dry-run          # 실행 없이 조립된 명령만 확인
#
# 의존성은 installer/.venv 에 격리됩니다 (시스템 파이썬을 건드리지 않습니다).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$ROOT_DIR/installer/.venv"
PYTHON_BIN="${PYTHON_BIN:-python3.13}"

C_RESET=$'\033[0m'; C_GREEN=$'\033[32m'; C_RED=$'\033[31m'
info()  { printf '%s[install]%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
error() { printf '%s[install]%s %s\n' "$C_RED" "$C_RESET" "$*" >&2; }

command -v "$PYTHON_BIN" >/dev/null 2>&1 || {
  error "$PYTHON_BIN 을 찾을 수 없습니다. PYTHON_BIN=python3 ./install.sh 처럼 지정하세요."
  exit 1
}

if [[ ! -x "$VENV/bin/python" ]]; then
  info "venv 생성 ($VENV)"
  "$PYTHON_BIN" -m venv "$VENV"
fi

# requirements 가 venv 보다 새로우면 다시 설치합니다.
STAMP="$VENV/.requirements-stamp"
REQ="$ROOT_DIR/installer/requirements.txt"
if [[ ! -f "$STAMP" || "$REQ" -nt "$STAMP" ]]; then
  info "의존성 설치"
  "$VENV/bin/pip" install -q -r "$REQ"
  touch "$STAMP"
fi

cd "$ROOT_DIR"
exec "$VENV/bin/python" -m installer "$@"
