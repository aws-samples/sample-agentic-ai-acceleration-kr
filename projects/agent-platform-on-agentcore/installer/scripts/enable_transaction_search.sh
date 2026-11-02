#!/bin/sh
# Transaction Search 켜기 (계정·리전 단위, 한 번만). 설치기의 transaction_search 단계가 실행합니다.
# 직접 실행: sh installer/scripts/enable_transaction_search.sh <region>
#
# sh 로 돌리므로 zsh 의 "$ACCOUNT:l" 소문자 수식어 문제가 없습니다.
# 수집된 스팬 수만큼 과금됩니다. 샘플링 비율은 아래 DesiredSamplingPercentage 로 정합니다.
set -eu
REGION=${1:?usage: sh enable_transaction_search.sh <region>}
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)

# 1) X-Ray 가 스팬을 CloudWatch Logs 에 쓸 수 있게 하는 로그 리소스 정책.
#    없으면 2) 가 "XRay does not have permission to call PutLogEvents" 로 거부됩니다.
aws logs put-resource-policy --region "${REGION}" --policy-name TransactionSearchXRayAccess \
  --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Sid\":\"TransactionSearchXRayAccess\",
    \"Effect\":\"Allow\",\"Principal\":{\"Service\":\"xray.amazonaws.com\"},\"Action\":\"logs:PutLogEvents\",
    \"Resource\":[\"arn:aws:logs:${REGION}:${ACCOUNT}:log-group:aws/spans:*\",
                 \"arn:aws:logs:${REGION}:${ACCOUNT}:log-group:/aws/application-signals/data:*\"],
    \"Condition\":{\"ArnLike\":{\"aws:SourceArn\":\"arn:aws:xray:${REGION}:${ACCOUNT}:*\"},
                  \"StringEquals\":{\"aws:SourceAccount\":\"${ACCOUNT}\"}}}]}"
sleep 10   # 정책 전파 전에 2) 를 실행하면 같은 권한 오류가 납니다

# 2) 스팬 목적지와 샘플링 비율
aws xray update-trace-segment-destination --destination CloudWatchLogs --region "${REGION}"
aws xray update-indexing-rule --name Default --region "${REGION}" \
  --rule '{"Probabilistic":{"DesiredSamplingPercentage":10}}'

aws xray get-trace-segment-destination --region "${REGION}"
