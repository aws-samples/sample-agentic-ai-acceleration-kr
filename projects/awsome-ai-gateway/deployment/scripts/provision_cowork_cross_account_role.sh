#!/usr/bin/env bash
# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
#
# provision_cowork_cross_account_role.sh — 905 계정에 cowork cross-account Mantle 역할 생성(멱등).
#
# cowork 클라이언트는 Bedrock Mantle Opus 4.8 을 **별도 계정(905, 도쿄)** 에서 쓴다.
# gateway-proxy(859 IRSA)가 이 역할을 AssumeRole 하여 Mantle bearer 를 발급받는다.
# codex/claude-code 는 in-account(859)라 assume 불필요 — cowork 만 유일한 cross-account.
#
# 이 스크립트는 905 계정 자격증명으로 실행한다(gateway-proxy 계정 아님).
#   AWS_ACCESS_KEY_ID/SECRET = 905 admin, 그 다음:
#   ./deployment/scripts/provision_cowork_cross_account_role.sh
#
# 대응물:
#   - 859 쪽 AssumeRole 권한 = terraform modules/irsa (var.cowork_role_arn).
#   - routing_profiles.account_role_arn(client=cowork) = migration 0009 (이 ARN 과 일치해야).
#
# 검증(생성 후, 859 pod 안에서):
#   kubectl exec -n llm-gateway <gateway-proxy-pod> -- python3 -c \
#     "import boto3;print(boto3.client('sts').assume_role(
#       RoleArn='${COWORK_ROLE_ARN}',RoleSessionName='t',ExternalId='cowork-bedrock')['Credentials']['AccessKeyId'])"
set -euo pipefail

ROLE_NAME="${COWORK_ROLE_NAME:-llm-gateway-cowork-bedrock}"
# 이 역할을 assume 할 gateway-proxy IRSA principal (859 dev). 여러 환경이면 공백구분으로 추가.
GATEWAY_PROXY_ROLE_ARNS="${GATEWAY_PROXY_ROLE_ARNS:-arn:aws:iam::123456789012:role/llm-gateway-dev-gateway-proxy-bedrock}"
EXTERNAL_ID="${COWORK_EXTERNAL_ID:-cowork-bedrock}"

echo "[cowork-role] 대상 계정 확인:"
aws sts get-caller-identity --query '{Account:Account,Arn:Arn}' --output json

# trust: 지정한 gateway-proxy IRSA 들만 assume 허용 + external_id 조건
PRINCIPALS=$(printf '"%s",' $GATEWAY_PROXY_ROLE_ARNS | sed 's/,$//')
cat > /tmp/cowork_trust.json <<JSON
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "AWS": [ ${PRINCIPALS} ] },
    "Action": "sts:AssumeRole",
    "Condition": { "StringEquals": { "sts:ExternalId": "${EXTERNAL_ID}" } }
  }]
}
JSON

# Mantle 권한 (도쿄 Opus 4.8). bedrock-mantle 네임스페이스 + bedrock invoke.
cat > /tmp/cowork_mantle.json <<'JSON'
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "BedrockMantleAll", "Effect": "Allow", "Action": ["bedrock-mantle:*"], "Resource": "*" },
    { "Sid": "BedrockInvoke", "Effect": "Allow",
      "Action": ["bedrock:InvokeModel","bedrock:InvokeModelWithResponseStream","bedrock:CountTokens","bedrock:ListFoundationModels"],
      "Resource": "*" }
  ]
}
JSON

if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  echo "[cowork-role] 역할 존재 → trust 갱신"
  aws iam update-assume-role-policy --role-name "$ROLE_NAME" --policy-document file:///tmp/cowork_trust.json
else
  echo "[cowork-role] 역할 생성: $ROLE_NAME"
  aws iam create-role --role-name "$ROLE_NAME" \
    --assume-role-policy-document file:///tmp/cowork_trust.json \
    --description "clean llm-gateway cowork Mantle Opus (cross-account assume from gateway-proxy, Tokyo)" \
    --max-session-duration 3600 >/dev/null
fi

aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name mantle-invoke \
  --policy-document file:///tmp/cowork_mantle.json

echo "[cowork-role] 완료: arn:aws:iam::$(aws sts get-caller-identity --query Account --output text):role/${ROLE_NAME}"
echo "[cowork-role] trust principals: ${GATEWAY_PROXY_ROLE_ARNS}"
echo "[cowork-role] external_id: ${EXTERNAL_ID}"
