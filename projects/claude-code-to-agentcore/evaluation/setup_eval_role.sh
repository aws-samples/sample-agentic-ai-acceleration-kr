#!/usr/bin/env bash
# setup_eval_role.sh — AgentCore Evaluations 실행 IAM 역할 생성.
#
# 온라인/배치 평가가 CloudWatch 로그를 읽고 Bedrock 평가 모델을 호출하려면
# 전용 실행 역할이 필요합니다. 이 스크립트가 최소 권한으로 생성합니다.
#
#   bash setup_eval_role.sh
#   export EVAL_ROLE_ARN=<출력된 ARN>
#
# 샘플 계정 ID는 111122223333 로 표기되어 있으니 본인 계정으로 바뀝니다(자동 조회).
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
ROLE_NAME="anycompany-ecommerce-eval-role"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"

TRUST=$(cat <<JSON
{"Version":"2012-10-17","Statement":[{
  "Effect":"Allow",
  "Principal":{"Service":"bedrock-agentcore.amazonaws.com"},
  "Action":"sts:AssumeRole",
  "Condition":{"StringEquals":{"aws:SourceAccount":"${ACCOUNT}"}}
}]}
JSON
)

POLICY=$(cat <<JSON
{"Version":"2012-10-17","Statement":[
  {"Sid":"ReadRuntimeLogs","Effect":"Allow",
   "Action":["logs:GetLogEvents","logs:FilterLogEvents","logs:DescribeLogGroups","logs:DescribeLogStreams","logs:StartQuery","logs:GetQueryResults"],
   "Resource":"arn:aws:logs:${REGION}:${ACCOUNT}:log-group:/aws/bedrock-agentcore/*"},
  {"Sid":"WriteEvalResults","Effect":"Allow",
   "Action":["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"],
   "Resource":"arn:aws:logs:${REGION}:${ACCOUNT}:log-group:/aws/bedrock-agentcore/evaluations/*"},
  {"Sid":"InvokeJudgeModel","Effect":"Allow",
   "Action":["bedrock:InvokeModel","bedrock:InvokeModelWithResponseStream"],
   "Resource":["arn:aws:bedrock:*::foundation-model/*",
               "arn:aws:bedrock:${REGION}:${ACCOUNT}:inference-profile/*"]}
]}
JSON
)

if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  echo "역할 재사용: $ROLE_NAME"
else
  aws iam create-role --role-name "$ROLE_NAME" \
    --assume-role-policy-document "$TRUST" >/dev/null
  echo "역할 생성: $ROLE_NAME"
fi

aws iam put-role-policy --role-name "$ROLE_NAME" \
  --policy-name eval-access --policy-document "$POLICY"

ARN="arn:aws:iam::${ACCOUNT}:role/${ROLE_NAME}"
echo ""
echo "EVAL_ROLE_ARN=${ARN}"
echo "   export EVAL_ROLE_ARN=${ARN}"
