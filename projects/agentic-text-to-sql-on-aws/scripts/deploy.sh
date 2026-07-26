#!/usr/bin/env bash
# deploy.sh — CDK 스택을 순서대로 배포한다(각 outputs 파일 생성).
#
# 배포 순서(의존성):
#   1) AgenticT2SqlBaseStack     → base-outputs.json     (VPC/Aurora/OpenSearch/ECR/Cognito/Memory/IAM)
#   2) AgenticT2SqlSemanticStack → semantic-outputs.json (DynamoDB/Neptune/OSIS/graph-sync Lambda)
#   3) [이미지 빌드·푸시: scripts/build-and-push.sh]  ← Base 완료 후, Runtime 전에 필수
#   4) [seed: scripts/seed.sh]                        ← Runtime 전에 데이터 적재
#   5) AgenticT2SqlRuntimeStack → runtime-outputs.json (AgentCore Runtime 3개)
#   6) [UI 이미지 빌드·푸시]
#   7) AgenticT2SqlUiStack      → ui-outputs.json     (ECS Fargate + ALB)
#
# 사용:
#   scripts/deploy.sh base       # Base 스택만
#   scripts/deploy.sh semantic   # Semantic 스택만 (Base 이후)
#   scripts/deploy.sh runtime    # Runtime 스택만
#   scripts/deploy.sh ui         # UI 스택만
#
# 주의: --require-approval never 는 IAM/보안그룹 변경 확인 프롬프트를 생략한다.
#       사용자가 전체 배포를 명시적으로 승인한 경우에만 사용할 것.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INFRA="$ROOT/infra"
TARGET="${1:-}"

cd "$INFRA"
[[ -d node_modules ]] || npm install

deploy_stack() {
  local stack="$1"
  local out="$2"
  echo "[cdk] deploy $stack → $out"
  npx cdk deploy "$stack" --require-approval never --outputs-file "$out"
  echo "[cdk] $stack 완료. outputs: $INFRA/$out"
}

case "$TARGET" in
  base)     deploy_stack AgenticT2SqlBaseStack     base-outputs.json ;;
  semantic) deploy_stack AgenticT2SqlSemanticStack semantic-outputs.json ;;
  runtime)  deploy_stack AgenticT2SqlRuntimeStack  runtime-outputs.json ;;
  ui)       deploy_stack AgenticT2SqlUiStack       ui-outputs.json ;;
  *)
    echo "사용법: $0 {base|semantic|runtime|ui}" >&2
    echo "  전체 흐름은 README/스크립트 헤더의 배포 순서를 따를 것." >&2
    exit 2
    ;;
esac
