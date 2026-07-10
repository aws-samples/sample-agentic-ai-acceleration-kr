# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ─── partial backend config ───
# bucket / dynamodb_table 은 계정마다 다르므로 init 시 -backend-config 로 주입.
# 본 deliverable 의 환경 (계정 123456789012 / ap-northeast-2):
#   terraform init \
#     -backend-config="bucket=llm-gateway-vanilla-tfstate-123456789012" \
#     -backend-config="dynamodb_table=llm-gateway-vanilla-tflock"
#
# 다른 계정에 처음 적용하려면 deployment/scripts/bootstrap-tfstate.sh 로
# 본인 계정의 tfstate bucket + dynamodb table 을 먼저 만든 뒤 그 이름으로 init.
terraform {
  backend "s3" {
    key     = "dev/terraform.tfstate"
    region  = "ap-northeast-2"
    encrypt = true
  }
}
