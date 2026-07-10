#!/usr/bin/env bash
# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
#
# build-lambdas.sh — package query_db / get_schema Lambdas for Terraform.
#
# Produces the build/ tree that the agentcore-runtime module's archive_file
# data sources zip:
#   deployment/terraform/modules/agentcore-runtime/build/query_db/
#   deployment/terraform/modules/agentcore-runtime/build/get_schema/
#
# Each contains: lambda_function.py + schema_whitelist.yaml + vendored deps.
#
# CRITICAL — platform-targeted wheels:
#   query_db needs psycopg2-binary, a native extension. Installing on macOS
#   yields Mac wheels that crash on Lambda's Amazon Linux (manylinux). We pin
#   --platform manylinux2014_x86_64 + --only-binary=:all: so pip fetches the
#   Linux wheels regardless of the build host. Runtime is python3.12, matching
#   the Lambda function runtime in lambdas.tf.
#
# Usage:  ./admin-chat-agent/lambdas/build-lambdas.sh
# Re-run whenever lambda_function.py, requirements.txt, or the whitelist change.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"        # admin-chat-agent/lambdas
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
MODULE_DIR="$REPO_ROOT/deployment/terraform/modules/agentcore-runtime"
BUILD_DIR="$MODULE_DIR/build"
WHITELIST="$REPO_ROOT/admin-chat-agent/config/schema_whitelist.yaml"

PY_VERSION="3.12"
PLATFORM="manylinux2014_x86_64"   # matches Lambda x86_64 python3.12

if [[ ! -f "$WHITELIST" ]]; then
  echo "FATAL: schema_whitelist.yaml not found at $WHITELIST" >&2
  exit 1
fi

build_one() {
  local name="$1"          # query_db | get_schema
  local src="$HERE/$name"
  local out="$BUILD_DIR/$name"

  echo ">>> Building $name → $out"
  rm -rf "$out"
  mkdir -p "$out"

  # 1) handler + whitelist (both Lambdas read the bundled whitelist)
  cp "$src/lambda_function.py" "$out/"
  cp "$WHITELIST" "$out/schema_whitelist.yaml"

  # 2) dependencies — Linux wheels only
  if [[ -s "$src/requirements.txt" ]]; then
    python3 -m pip install \
      --platform "$PLATFORM" \
      --python-version "$PY_VERSION" \
      --implementation cp \
      --only-binary=:all: \
      --target "$out" \
      --upgrade \
      -r "$src/requirements.txt" \
      --quiet
  fi

  # 3) trim bloat that inflates the zip (tests, dist-info caches)
  find "$out" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
  find "$out" -type d -name "*.dist-info" -prune -exec rm -rf {} + 2>/dev/null || true

  echo "    done — $(du -sh "$out" | cut -f1)"
}

mkdir -p "$BUILD_DIR"
build_one "query_db"
build_one "get_schema"

echo ""
echo ">>> Build complete. Terraform will zip build/{query_db,get_schema}."
echo "    Next: terraform plan -var enable_chat_agent=true -var enable_chat_db_tools=true"
