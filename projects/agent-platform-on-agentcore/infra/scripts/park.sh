#!/usr/bin/env bash
# Park/unpark the agent-platform environment: pause without destroying.
#
#   ./park.sh status   Show current state and estimated fixed costs
#   ./park.sh down     Scale tasks to 0, apply hibernation (remove NAT/EIP, save costs)
#   ./park.sh up       Restore: remove hibernation, recreate NAT/EIP, scale tasks back to 1
#
# Implementation: controlled solely by `hibernate` and `desired_count` variables
# in envs/standalone. Tasks must reach 0 before deleting NAT or they lose egress.
#
# Preserved: all DynamoDB tables, S3 buckets, Cognito, AgentCore runtimes/memory/
# gateways/registry, Knowledge Bases, ALB (with its DNS name unchanged), VPC.
#
# Hibernation setup: Ensure `desired_count` is 0 before hibernating, and restore
# with `terraform apply` (desired_count defaults back to 1, hibernate defaults to false).
set -euo pipefail

export AWS_REGION="${AWS_REGION:-ap-northeast-1}"
AWS_PAGER=""; export AWS_PAGER

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ap_root=$(cd "$script_dir/../.." && pwd)
tf_dir="$ap_root/infra/envs/standalone"

# Derive project from terraform outputs or use default
project="${PROJECT:-bap}"
cluster="$project-cluster"
services=(server web)
hibernate_tfvars="hibernate.tfvars"

log()  { printf '\033[0;34m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
ok()   { printf '\033[0;32m[OK]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[WARN]\033[0m %s\n' "$*"; }
die()  { printf '\033[0;31m[ERR]\033[0m %s\n' "$*" >&2; exit 1; }

tf() { (cd "$tf_dir" && terraform "$@"); }

ecs_counts() {
  aws ecs describe-services --cluster "$cluster" --services "${services[@]}" \
    --query 'services[].[serviceName,desiredCount,runningCount]' --output text 2>/dev/null || true
}

nat_id() {
  aws ec2 describe-nat-gateways \
    --filter "Name=tag:Name,Values=${project}-nat" "Name=state,Values=pending,available" \
    --query 'NatGateways[0].NatGatewayId' --output text 2>/dev/null | grep -v None || true
}

wait_running_zero() {
  log "Waiting for ECS tasks to reach 0..."
  for _ in $(seq 1 60); do
    local total; total=$(ecs_counts | awk '{s+=$3} END{print s+0}')
    [[ "$total" == 0 ]] && { ok "running=0"; return 0; }
    sleep 5
  done
  die "Tasks did not reach 0 within 60×5s"
}

wait_services_stable() {
  log "Waiting for ECS services to stabilize..."
  aws ecs wait services-stable --cluster "$cluster" --services "${services[@]}"
  ok "Services stable"
}

cmd_status() {
  echo "== ECS ($cluster) — service desired running"
  ecs_counts | column -t
  local nat; nat=$(nat_id)
  echo "== NAT Gateway: ${nat:-none (hibernating)}"
  if [[ -n "$nat" ]]; then
    aws ec2 describe-nat-gateways --nat-gateway-ids "$nat" \
      --query 'NatGateways[0].NatGatewayAddresses[0].PublicIp' --output text | sed 's/^/   public IP: /'
  fi
  echo "== ALB"
  aws elbv2 describe-load-balancers --names "$project-alb" \
    --query 'LoadBalancers[0].[DNSName,State.Code]' --output text 2>/dev/null || echo "   none"
  echo "== Preserved data"
  for t in threads users usage artifacts knowledge-bases prefs; do
    aws dynamodb describe-table --table-name "$project-$t" 2>/dev/null \
      --query 'Table.[TableName,ItemCount]' --output text | awk '{printf "   %-40s items=%s\n",$1,$2}' || true
  done
}

cmd_down() {
  log "[1/3] terraform apply desired_count=0 (scale tasks to 0)"
  tf apply -auto-approve -var desired_count=0
  wait_running_zero
  if [[ -f "$tf_dir/$hibernate_tfvars" ]]; then
    log "[2/3] terraform apply -var-file=$hibernate_tfvars (delete NAT/EIP, remove private route)"
    tf apply -auto-approve -var-file="$hibernate_tfvars"
  else
    warn "$hibernate_tfvars not found, using -var instead"
    log "[2/3] terraform apply -var hibernate=true -var desired_count=0"
    tf apply -auto-approve -var hibernate=true -var desired_count=0
  fi
  log "[3/3] Status"
  cmd_status
  ok "Hibernated. To restore: $0 up"
}

cmd_up() {
  log "[1/2] terraform apply (restore hibernation, recreate NAT/EIP, scale tasks)"
  tf apply -auto-approve
  wait_services_stable
  log "[2/2] Health check"
  cmd_status
  ok "Restored."
}

case "${1:-}" in
  status) cmd_status ;;
  down)   cmd_down ;;
  up)     cmd_up ;;
  *) sed -n '2,15p' "$0"; exit 1 ;;
esac
