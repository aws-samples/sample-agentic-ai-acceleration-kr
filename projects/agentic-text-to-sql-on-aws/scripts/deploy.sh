#!/usr/bin/env bash
# deploy.sh — CDK 스택을 순서대로 배포한다(각 outputs 파일 생성).
#
# 배포 순서(의존성):
#   1) AgenticT2SqlBaseStack     → base-outputs.json     (VPC/Aurora/OpenSearch/ECR/Cognito/Memory/IAM)
#   2) AgenticT2SqlSemanticStack → semantic-outputs.json (DynamoDB/Neptune/OSIS/graph-sync Lambda)
#   3) [이미지 빌드·푸시: scripts/build-and-push.sh]  ← Base 완료 후, Runtime 전에 필수
#                                                     (datasource-admin-mcp 이미지 포함)
#   4) [seed: scripts/seed.sh]                        ← Runtime 전에 데이터 적재
#   5) AgenticT2SqlRuntimeStack → runtime-outputs.json (AgentCore Runtime 4개, admin-mcp 포함)
#   6) AgenticT2SqlGatewayStack → gateway-outputs.json (Gateway·Cedar·Identity + admin target)
#   7) [Cedar 2-phase] scripts/deploy.sh gateway-scoped
#                                 ← admin target 도구 동기화 후 action 스코프 정책으로 갱신
#                                   (action 목록 정책은 target 생성 전에는 검증에 실패한다)
#   8) AgenticT2SqlEvaluationStack → evaluation-outputs.json
#                                 (EX evaluator Lambda·AgentCore Evaluator·online eval·
#                                  SSM 활성 bundle 포인터. Base+Runtime 이후, Admin 이전)
#   9) [UI / admin-web 이미지 빌드·푸시]
#  10) AgenticT2SqlUiStack      → ui-outputs.json     (ECS Fargate + ALB)
#  11) AgenticT2SqlAdminStack   → admin-outputs.json  (admin panel Fargate + 전용 ALB,
#                                 Gateway·Evaluation 이후 — 평가 화면 env 를 소비)
#
# 사용:
#   scripts/deploy.sh base           # Base 스택만
#   scripts/deploy.sh semantic       # Semantic 스택만 (Base 이후)
#   scripts/deploy.sh runtime        # Runtime 스택만
#   scripts/deploy.sh gateway        # Gateway 스택만 (Runtime 이후, Cedar phase 1)
#   scripts/deploy.sh gateway-scoped # Gateway 재배포 (Cedar phase 2 — action 스코프 활성)
#   scripts/deploy.sh evaluation     # Evaluation 스택만 (Runtime 이후, Admin 이전)
#   scripts/deploy.sh ui             # UI 스택만
#   scripts/deploy.sh admin          # Admin panel 스택만 (Gateway·Evaluation 이후)
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
  shift 2
  echo "[cdk] deploy $stack → $out ${*:+(추가 옵션: $*)}"
  npx cdk deploy "$stack" --require-approval never --outputs-file "$out" "$@"
  echo "[cdk] $stack 완료. outputs: $INFRA/$out"
}

case "$TARGET" in
  base)     deploy_stack AgenticT2SqlBaseStack     base-outputs.json ;;
  semantic) deploy_stack AgenticT2SqlSemanticStack semantic-outputs.json ;;
  runtime)  deploy_stack AgenticT2SqlRuntimeStack  runtime-outputs.json ;;
  gateway)  deploy_stack AgenticT2SqlGatewayStack  gateway-outputs.json ;;
  # Cedar 2-phase 의 phase 2: admin target 도구 동기화 후 action 스코프 정책으로 갱신.
  # 정책 논리 ID 는 동일하므로 statement 만 CFN update 된다.
  gateway-scoped)
            deploy_stack AgenticT2SqlGatewayStack  gateway-outputs.json \
              -c cedarActionScoping=true ;;
  # 평가 파이프라인(EX evaluator Lambda + AgentCore Evaluator + online eval + SSM 포인터).
  evaluation)
            deploy_stack AgenticT2SqlEvaluationStack evaluation-outputs.json ;;
  ui)       deploy_stack AgenticT2SqlUiStack       ui-outputs.json ;;
  admin)    deploy_stack AgenticT2SqlAdminStack    admin-outputs.json ;;
  *)
    echo "사용법: $0 {base|semantic|runtime|gateway|gateway-scoped|evaluation|ui|admin}" >&2
    echo "  전체 흐름은 README/스크립트 헤더의 배포 순서를 따를 것." >&2
    exit 2
    ;;
esac
