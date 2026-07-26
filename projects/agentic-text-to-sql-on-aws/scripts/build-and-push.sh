#!/usr/bin/env bash
# build-and-push.sh — 컴포넌트 컨테이너 이미지를 ARM64 로 빌드해 ECR 에 푸시.
#
# D9: Runtime/UI 는 컨테이너(ECR) 방식. 로컬 빌드는 docker 우선, 데몬이 없으면 finch 로 자동 폴백.
# 모든 이미지는 linux/arm64, 태그 latest.
#
# 사용:
#   scripts/build-and-push.sh <component> [<component> ...]
#   scripts/build-and-push.sh all
# 컴포넌트: orchestrator | sql-execution-mcp | semantic-retrieval-mcp | ui
#
# 필요 env(없으면 자동 조회):
#   AWS_REGION(기본 us-west-2), AWS_ACCOUNT_ID(기본 sts get-caller-identity)
set -euo pipefail

REGION="${AWS_REGION:-us-west-2}"
ACCOUNT_ID="${AWS_ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# 컴포넌트 → (빌드 컨텍스트 경로, ECR 리포명) 매핑.
# macOS 기본 bash 3.2 는 연관 배열(declare -A)을 지원하지 않으므로 case 함수로 매핑한다.
ctx_of() {
  case "$1" in
    orchestrator)           echo "$ROOT/agents/orchestrator" ;;
    sql-execution-mcp)      echo "$ROOT/agents/sql-execution-mcp" ;;
    semantic-retrieval-mcp) echo "$ROOT/agents/semantic-retrieval-mcp" ;;
    ui)                     echo "$ROOT/ui" ;;
    *)                      echo "" ;;
  esac
}
repo_of() {
  case "$1" in
    orchestrator)           echo "agentic-t2sql/orchestrator" ;;
    sql-execution-mcp)      echo "agentic-t2sql/sql-execution-mcp" ;;
    semantic-retrieval-mcp) echo "agentic-t2sql/semantic-retrieval-mcp" ;;
    ui)                     echo "agentic-t2sql/ui" ;;
    *)                      echo "" ;;
  esac
}

# --- 컨테이너 CLI 선택: docker 데몬이 살아있으면 docker, 아니면 finch ---
pick_engine() {
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    echo docker
  elif command -v finch >/dev/null 2>&1; then
    echo finch
  else
    echo "ERROR: docker 데몬도 finch 도 사용할 수 없습니다." >&2
    exit 1
  fi
}
ENGINE="$(pick_engine)"
echo "[engine] $ENGINE 사용 (region=$REGION account=$ACCOUNT_ID)"

# --- ECR 로그인 ---
aws ecr get-login-password --region "$REGION" \
  | "$ENGINE" login --username AWS --password-stdin "$REGISTRY"

build_one() {
  local comp="$1"
  local ctx="$(ctx_of "$comp")"
  local repo="$(repo_of "$comp")"
  if [[ -z "$ctx" || -z "$repo" ]]; then
    echo "ERROR: 알 수 없는 컴포넌트 '$comp'" >&2
    exit 2
  fi
  local image="${REGISTRY}/${repo}:latest"
  echo "[build] $comp → $image"
  # BuildKit cache-mount(Dockerfile 의 --mount=type=cache)은 docker buildx / finch 모두 지원.
  "$ENGINE" build --platform linux/arm64 -t "$image" "$ctx"
  echo "[push]  $image"
  "$ENGINE" push "$image"
  echo "[done]  $comp"
}

COMPONENTS=("$@")
if [[ "${1:-}" == "all" ]]; then
  COMPONENTS=(orchestrator sql-execution-mcp semantic-retrieval-mcp ui)
fi
if [[ ${#COMPONENTS[@]} -eq 0 ]]; then
  echo "사용법: $0 <component>... | all" >&2
  echo "  컴포넌트: orchestrator sql-execution-mcp semantic-retrieval-mcp ui" >&2
  exit 2
fi

for comp in "${COMPONENTS[@]}"; do
  build_one "$comp"
done
echo "[all-done] 이미지 빌드·푸시 완료: ${COMPONENTS[*]}"
