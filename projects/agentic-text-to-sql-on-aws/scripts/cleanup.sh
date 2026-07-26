#!/usr/bin/env bash
# cleanup.sh — 이 프로젝트가 생성한 모든 과금 리소스를 삭제한다.
#
# ⚠️ 파괴적 작업. 스택 삭제는 Aurora/OpenSearch/NAT GW/ALB/ECR 등 과금 리소스를 제거한다.
# 실행 전 반드시 리전/계정을 확인하고, 프로덕션이 아님을 확인할 것.
# 이 스크립트는 `agentic-t2sql*` 접두어 리소스와 AgenticT2Sql* 스택만 대상으로 한다.
#
# 삭제 순서(생성의 역순 — 의존성):
#   1) AgenticT2SqlUiStack
#   2) AgenticT2SqlRuntimeStack
#   3) AgenticT2SqlSemanticStack (DynamoDB/Neptune Serverless/OSIS/graph-sync Lambda)
#   4) AgenticT2SqlBaseStack   (Aurora/OpenSearch/VPC/NAT/ECR/Cognito/Memory)
#
# ECR 리포는 emptyOnDelete=true 라 스택 삭제 시 이미지째 제거된다.
# seed 가 만든 별도 시크릿(agentic-t2sql/aurora/agent-ro; 레거시 경로)이 있으면 함께 정리한다.
# (권장 경로에서는 CDK 관리 시크릿만 쓰므로 이 시크릿이 없을 수 있음.)
#
# 사용:
#   scripts/cleanup.sh            # 확인 프롬프트 후 삭제
#   scripts/cleanup.sh --yes      # 프롬프트 없이 삭제(CI 용)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INFRA="$ROOT/infra"
REGION="${AWS_REGION:-us-west-2}"
AUTO="${1:-}"

echo "리전: $REGION"
echo "계정: $(aws sts get-caller-identity --query Account --output text 2>/dev/null || echo '?')"
echo "삭제 대상 스택: AgenticT2SqlUiStack, AgenticT2SqlRuntimeStack, AgenticT2SqlSemanticStack, AgenticT2SqlBaseStack"
echo

if [[ "$AUTO" != "--yes" ]]; then
  read -r -p "정말 모두 삭제합니까? 'delete' 입력: " ans
  [[ "$ans" == "delete" ]] || { echo "취소됨."; exit 0; }
fi

cd "$INFRA"
[[ -d node_modules ]] || npm install

# 역순 삭제. 각 스택이 없으면 CDK 가 no-op.
for stack in AgenticT2SqlUiStack AgenticT2SqlRuntimeStack AgenticT2SqlSemanticStack AgenticT2SqlBaseStack; do
  echo "[cdk] destroy $stack"
  npx cdk destroy "$stack" --force || echo "  (경고) $stack 삭제 중 문제 — 콘솔에서 확인 필요"
done

# 레거시 seed 시크릿(있으면) 정리. 복구 창 없이 즉시 삭제.
LEGACY_SECRET="agentic-t2sql/aurora/agent-ro"
if aws secretsmanager describe-secret --secret-id "$LEGACY_SECRET" --region "$REGION" >/dev/null 2>&1; then
  echo "[secrets] 레거시 시크릿 삭제: $LEGACY_SECRET"
  aws secretsmanager delete-secret --secret-id "$LEGACY_SECRET" \
    --force-delete-without-recovery --region "$REGION" >/dev/null
fi

# outputs 파일 정리.
rm -f "$INFRA"/base-outputs.json "$INFRA"/semantic-outputs.json "$INFRA"/runtime-outputs.json "$INFRA"/ui-outputs.json

echo "[cleanup] 완료. CloudWatch 로그 그룹(/aws/bedrock-agentcore/runtimes/*, /ecs/*)은"
echo "          보존 정책에 따라 남을 수 있으니 필요 시 수동 삭제하세요."
