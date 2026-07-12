#!/bin/bash
set -euo pipefail

# ============================================================
# Seed Lambda tool API keys into AWS Secrets Manager
# ============================================================
# Keys NEVER pass through Terraform. This writes the value directly to the
# secret container created by the tool-secret module.
#
# Usage:
#   deployment/scripts/seed-tool-secrets.sh <keys-file>
# where <keys-file> is an UNTRACKED file of KEY=VALUE lines:
#   serper=aa3488...
#   exa=d89a33...
#   anthropic=sk-ant-...
#   firecrawl=fc-...
#   you=ydc-sk-...
#   brave=BSA...
#   tavily=tvly-dev-...
#
# Engines map to secret name ${PROJECT}/${ENV}/tool/<engine>.

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
ENV_DIR="$SCRIPT_DIR/../terraform/environments/tool-gateway-dev"

KEYS_FILE="${1:?Usage: seed-tool-secrets.sh <keys-file>}"
[ -f "$KEYS_FILE" ] || { echo "ERROR: keys file not found: $KEYS_FILE" >&2; exit 1; }

PROJECT_NAME=$(grep '^project_name' "$ENV_DIR/terraform.tfvars" | cut -d'=' -f2 | xargs | tr -d '"')
ENVIRONMENT=$(grep '^environment'  "$ENV_DIR/terraform.tfvars" | cut -d'=' -f2 | xargs | tr -d '"')
AWS_REGION=$(grep '^aws_region'    "$ENV_DIR/terraform.tfvars" | cut -d'=' -f2 | xargs | tr -d '"')

echo "Seeding tool secrets: project=$PROJECT_NAME env=$ENVIRONMENT region=$AWS_REGION"

while IFS='=' read -r engine value; do
  [ -z "$engine" ] && continue
  case "$engine" in \#*) continue ;; esac
  value="$(echo "$value" | xargs)"
  [ -z "$value" ] && { echo "  ⚠ $engine: empty (skipped)"; continue; }
  secret_name="${PROJECT_NAME}/${ENVIRONMENT}/tool/${engine}"
  if aws secretsmanager put-secret-value \
       --secret-id "$secret_name" \
       --secret-string "$value" \
       --region "$AWS_REGION" >/dev/null 2>&1; then
    echo "  ✓ $engine -> $secret_name"
  else
    echo "  ✗ $engine: put-secret-value failed (does secret $secret_name exist? run terraform apply first)"
  fi
done < "$KEYS_FILE"

echo "Done. Verify with: aws secretsmanager get-secret-value --secret-id ${PROJECT_NAME}/${ENVIRONMENT}/tool/serper --region ${AWS_REGION} --query SecretString --output text"
