#!/usr/bin/env bash
# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ==============================================================================
# Terraform State Backend 최초 1회 셋업 — S3 + DynamoDB
# ------------------------------------------------------------------------------
# Terraform이 자기 자신으로 state 백엔드를 만들 수 없는 chicken-and-egg 때문에
# 이 스크립트로 "state를 저장할 S3 + lock용 DynamoDB" 를 먼저 만듭니다.
# 한 번 실행하면 반복 실행 불필요 (idempotent).
# ==============================================================================

set -euo pipefail

# ---- 기본값 (필요 시 환경변수로 오버라이드) ----
: "${AWS_REGION:=ap-northeast-2}"
: "${TFSTATE_BUCKET:=llm-gateway-tfstate}"
: "${TFLOCK_TABLE:=llm-gateway-tflock}"

echo "==> Bootstrapping Terraform state backend"
echo "    region        : ${AWS_REGION}"
echo "    state bucket  : ${TFSTATE_BUCKET}"
echo "    lock table    : ${TFLOCK_TABLE}"
echo ""

# 0) AWS 인증 확인
if ! aws sts get-caller-identity --region "${AWS_REGION}" >/dev/null 2>&1; then
    echo "❌ AWS 인증이 안 됩니다. aws configure / aws sso login 먼저 수행."
    exit 1
fi

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
echo "✓ AWS Account: ${ACCOUNT_ID}"

# 1) S3 버킷 생성 (이미 있으면 스킵)
if aws s3api head-bucket --bucket "${TFSTATE_BUCKET}" 2>/dev/null; then
    echo "✓ S3 bucket ${TFSTATE_BUCKET} 이미 존재"
else
    echo "==> S3 bucket 생성: ${TFSTATE_BUCKET}"
    if [ "${AWS_REGION}" = "ap-northeast-2" ]; then
        aws s3api create-bucket --bucket "${TFSTATE_BUCKET}" --region "${AWS_REGION}"
    else
        aws s3api create-bucket \
            --bucket "${TFSTATE_BUCKET}" \
            --region "${AWS_REGION}" \
            --create-bucket-configuration LocationConstraint="${AWS_REGION}"
    fi
fi

# 2) Versioning (이전 state 복원용)
aws s3api put-bucket-versioning \
    --bucket "${TFSTATE_BUCKET}" \
    --versioning-configuration Status=Enabled
echo "✓ Versioning 활성화"

# 3) Server-side 암호화
aws s3api put-bucket-encryption \
    --bucket "${TFSTATE_BUCKET}" \
    --server-side-encryption-configuration '{
      "Rules": [{
        "ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"},
        "BucketKeyEnabled": true
      }]
    }'
echo "✓ SSE-S3 암호화 설정"

# 4) Public access 완전 차단
aws s3api put-public-access-block \
    --bucket "${TFSTATE_BUCKET}" \
    --public-access-block-configuration \
        "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
echo "✓ Public access 차단"

# 5) Lifecycle — 오래된 버전 정리
aws s3api put-bucket-lifecycle-configuration \
    --bucket "${TFSTATE_BUCKET}" \
    --lifecycle-configuration '{
      "Rules": [{
        "ID": "expire-old-versions",
        "Status": "Enabled",
        "Filter": {"Prefix": ""},
        "NoncurrentVersionExpiration": {"NoncurrentDays": 90}
      }]
    }'
echo "✓ 90일 지난 이전 버전 자동 삭제"

# 6) DynamoDB 테이블 (lock) — 이미 있으면 스킵
if aws dynamodb describe-table --table-name "${TFLOCK_TABLE}" --region "${AWS_REGION}" >/dev/null 2>&1; then
    echo "✓ DynamoDB table ${TFLOCK_TABLE} 이미 존재"
else
    echo "==> DynamoDB table 생성: ${TFLOCK_TABLE}"
    aws dynamodb create-table \
        --table-name "${TFLOCK_TABLE}" \
        --attribute-definitions AttributeName=LockID,AttributeType=S \
        --key-schema AttributeName=LockID,KeyType=HASH \
        --billing-mode PAY_PER_REQUEST \
        --region "${AWS_REGION}" \
        --tags Key=Project,Value=llm-gateway Key=ManagedBy,Value=bootstrap-script

    echo "   DynamoDB 테이블 생성 대기..."
    aws dynamodb wait table-exists --table-name "${TFLOCK_TABLE}" --region "${AWS_REGION}"
fi

echo ""
echo "✅ Bootstrap 완료. 다음 단계:"
echo ""
echo "   cd deployment/terraform/environments/dev  (또는 prod)"
echo "   cp terraform.tfvars.example terraform.tfvars"
echo "   # terraform.tfvars 편집"
echo "   terraform init"
echo "   terraform plan"
echo "   terraform apply"
echo ""