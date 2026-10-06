#!/usr/bin/env bash
# Backup agent-platform state before full destruction.
#
#   ./backup.sh [output-dir]     Default: ~/ap-backup-<timestamp>
#
# Backups: DynamoDB tables, S3 buckets, Terraform state, Cognito users,
# AgentCore metadata (runtimes, memories, gateways).
#
# Not included: password resets (Cognito passwords cannot be exported),
# AgentCore Memory events (no official import), registry records (recreated
# with new IDs after registry destroy).
set -uo pipefail

export AWS_REGION="${AWS_REGION:-ap-northeast-1}"
AWS_PAGER=""; export AWS_PAGER

project="${PROJECT:-bap}"
acct=$(aws sts get-caller-identity --query Account --output text)
stamp=$(date -u +%Y%m%dT%H%M%SZ)
out="${1:-$HOME/ap-backup-$stamp}"
mkdir -p "$out"/{dynamodb,s3,terraform,cognito,agentcore}

log() { printf '\033[0;34m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }

log "Output: $out (account $acct, region $AWS_REGION, project $project)"

# DynamoDB
for t in threads users usage artifacts knowledge-bases prefs; do
  tbl="$project-$t"
  log "DynamoDB $tbl: backup + scan"
  aws dynamodb create-backup --table-name "$tbl" --backup-name "$tbl-$stamp" \
    --query 'BackupDetails.BackupArn' --output text >> "$out/dynamodb/backup-arns.txt" 2>/dev/null || true
  aws dynamodb scan --table-name "$tbl" --output json > "$out/dynamodb/$tbl.json" 2>/dev/null || true
done

# S3
for b in artifacts knowledge skills; do
  bkt="$project-$b-$AWS_REGION"
  log "S3 $bkt → $out/s3/$b"
  aws s3 sync "s3://$bkt" "$out/s3/$b" --quiet 2>/dev/null || log "  (bucket not found or empty)"
done

# Terraform state
log "Terraform state"
tf_state_bucket="$project-tfstate-$acct"
aws s3 sync "s3://$tf_state_bucket/" "$out/terraform/tfstate/" --quiet 2>/dev/null || true

# Cognito
log "Cognito users"
pool=$(aws cognito-idp list-user-pools --max-results 20 \
  --query "UserPools[?Name=='$project-user-pool'].Id" --output text 2>/dev/null)
if [[ -n "$pool" ]]; then
  echo "$pool" > "$out/cognito/pool-id.txt"
  aws cognito-idp list-users --user-pool-id "$pool" --output json > "$out/cognito/users.json" 2>/dev/null || true
fi

# AgentCore
log "AgentCore metadata"
aws bedrock-agentcore-control list-agent-runtimes --output json > "$out/agentcore/runtimes.json" 2>/dev/null || true
aws bedrock-agentcore-control list-memories --output json > "$out/agentcore/memories.json" 2>/dev/null || true
aws bedrock-agentcore-control list-gateways --output json > "$out/agentcore/gateways.json" 2>/dev/null || true

log "Done."
du -sh "$out"/* 2>/dev/null | sed 's/^/   /'
