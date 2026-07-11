# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ─── partial backend config ───
# bucket / dynamodb_table are account-specific, so injected via -backend-config at init.
# For this deliverable's environment (us-east-1):
#   terraform init \
#     -backend-config="bucket=tool-gateway-tfstate-<account-id>" \
#     -backend-config="dynamodb_table=tool-gateway-tflock"
#
# For new accounts, first bootstrap the state bucket and lock table via deployment/scripts/bootstrap-tfstate.sh,
# then init with the generated bucket/table names.
terraform {
  backend "s3" {
    key     = "tool-gateway-dev/terraform.tfstate"
    region  = "us-east-1"
    encrypt = true
  }
}
