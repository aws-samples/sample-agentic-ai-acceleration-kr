#!/usr/bin/env bash
# e2e-smoke.sh — E2E 3레벨 스모크 테스트 실행 래퍼.
#
#  레벨1 (MCP): scripts/e2e_verify.py --level 1  (orchestrator venv 에서 실행)
#  레벨2 (에이전트): scripts/e2e_verify.py --level 2
#  레벨3 (UI): ALB URL 200 확인 + /api/health 확인
#  레벨4 (M2): clarification interrupt E2E + semantic 검색 확장(용어/fewshot)
#  레벨5 (M3): Gateway 집약 + Cedar 허용/거부 + Redshift datasource
#             (E2E_USER_PASSWORD env 필요 — Cognito 테스트 사용자)
#
# 전제: Runtime 스택 배포 완료(runtime-outputs.json). UI 레벨은 ui-outputs.json 필요.
#
# 사용:
#   scripts/e2e-smoke.sh          # 가능한 레벨 전부
#   scripts/e2e-smoke.sh 1        # MCP 레벨만
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INFRA="$ROOT/infra"
ORCH="$ROOT/agents/orchestrator"
LEVEL="${1:-all}"
export AWS_REGION="${AWS_REGION:-us-west-2}"

run_py_levels() {
  local lvl="$1"
  echo "== E2E 레벨 $lvl (MCP/에이전트) =="
  # orchestrator venv 에 mcp-proxy-for-aws/mcp/boto3 가 있으므로 그 환경에서 실행.
  ( cd "$ORCH" && [[ -d .venv ]] || uv sync )
  ( cd "$ORCH" && uv run python "$ROOT/scripts/e2e_verify.py" --level "$lvl" )
}

verify_ui() {
  local out="$INFRA/ui-outputs.json"
  if [[ ! -f "$out" ]]; then
    echo "[레벨3] ui-outputs.json 없음 — UI 미배포. skip."
    return 0
  fi
  local url
  url="$(jq -r '.AgenticT2SqlUiStack.AlbUrl' "$out")"
  echo "== E2E 레벨3 (UI): $url =="
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$url/" || echo 000)"
  echo "  ALB GET / → HTTP $code"
  [[ "$code" =~ ^2|^3 ]] && echo "  [PASS] UI 200/3xx" || echo "  [FAIL] UI 응답 $code"
  local hcode
  hcode="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$url/api/health" || echo 000)"
  echo "  /api/health → HTTP $hcode"
}

case "$LEVEL" in
  1) run_py_levels 1 ;;
  2) run_py_levels 2 ;;
  3) verify_ui ;;
  4) run_py_levels 4 ;;
  5) run_py_levels 5 ;;
  all)
    run_py_levels all
    verify_ui
    ;;
  *) echo "사용법: $0 {1|2|3|4|5|all}" >&2; exit 2 ;;
esac
