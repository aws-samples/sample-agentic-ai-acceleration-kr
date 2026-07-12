#!/usr/bin/env bash
# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
# deploy-tui.sh — venv 부트스트랩 후 Deploy TUI 실행.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
VENV="$REPO_ROOT/deployment/tui/.venv"

if [ ! -d "$VENV" ]; then
    echo "==> creating venv at $VENV"
    python3 -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"
pip install -q -r "$REPO_ROOT/deployment/tui/requirements.txt"

cd "$REPO_ROOT"
exec python -m deployment.tui
