#!/usr/bin/env bash
# Keep the bap_* AgentCore runtimes warm.
#
# Why this exists: a runtime's microVM is reaped after some idle window, so the
# first message of a chat after a lull pays a cold start — measured as an
# intermittent jump from ~3s to ~7s time-to-first-token. AgentCore Runtime
# exposes no min-instance / provisioned-concurrency knob, so the only lever is to
# invoke it on a schedule. Invoking an agent for real would run a model call every
# few minutes; instead each runtime short-circuits a {"ping": true} payload in
# main.py, returning before the model. That keeps the expensive part hot (microVM + Python process + imports)
# for near-zero cost.
#
# The runtimes live outside Terraform, so
# their keep-warm lives here beside deploy.sh rather than in the stack. Idempotent:
# re-running updates the role/schedules in place.
#
#   ./scripts/keepwarm.sh up      # create/update role + schedules (default)
#   ./scripts/keepwarm.sh down    # delete schedules + role
set -euo pipefail

REGION="${REGION_NAME:-ap-northeast-1}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
ROLE="bap-keepwarm-scheduler"
RATE="${KEEPWARM_RATE:-rate(5 minutes)}"

# runtime-id -> schedule-name. Add a runtime here to warm it; drop one to stop.
# The IDs below are placeholders: replace them with the runtime IDs that
# deploy.sh printed for your account (aws bedrock-agentcore-control list-agent-runtimes).
RUNTIMES=(
  "bap_default-AbCdE12345:bap-keepwarm-default"
)

runtime_arn() { echo "arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT}:runtime/$1"; }

ensure_role() {
  local trust perm
  trust='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"scheduler.amazonaws.com"},"Action":"sts:AssumeRole","Condition":{"StringEquals":{"aws:SourceAccount":"'"$ACCOUNT"'"}}}]}'
  # One statement listing every warmed runtime (and its session sub-resources).
  local resources=""
  for entry in "${RUNTIMES[@]}"; do
    local arn; arn="$(runtime_arn "${entry%%:*}")"
    resources+="\"$arn\",\"$arn/*\","
  done
  resources="${resources%,}"
  perm='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"bedrock-agentcore:InvokeAgentRuntime","Resource":['"$resources"']}]}'

  if aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
    aws iam update-assume-role-policy --role-name "$ROLE" --policy-document "$trust"
  else
    aws iam create-role --role-name "$ROLE" --assume-role-policy-document "$trust" \
      --description "EventBridge Scheduler role to keep bap runtimes warm" >/dev/null
  fi
  aws iam put-role-policy --role-name "$ROLE" --policy-name invoke-runtimes --policy-document "$perm"
}

up() {
  ensure_role
  local role_arn="arn:aws:iam::${ACCOUNT}:role/${ROLE}"
  # IAM is eventually consistent; a brand-new role can lag a schedule that names it.
  sleep 10
  for entry in "${RUNTIMES[@]}"; do
    local id="${entry%%:*}" name="${entry##*:}" arn; arn="$(runtime_arn "$id")"
    # runtimeSessionId must be 33-128 chars; pad the schedule name.
    local sid="${name}-$(printf '%033d' 0)"; sid="${sid:0:80}"
    local input; input="{\"AgentRuntimeArn\":\"${arn}\",\"RuntimeSessionId\":\"${sid}\",\"Payload\":\"{\\\"ping\\\":true}\"}"
    local target="{\"Arn\":\"arn:aws:scheduler:::aws-sdk:bedrockagentcore:invokeAgentRuntime\",\"RoleArn\":\"${role_arn}\",\"Input\":$(printf '%s' "$input" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')}"
    local args=(--name "$name" --schedule-expression "$RATE" --flexible-time-window '{"Mode":"OFF"}' --state ENABLED --region "$REGION" --target "$target")
    if aws scheduler get-schedule --name "$name" --region "$REGION" >/dev/null 2>&1; then
      aws scheduler update-schedule "${args[@]}" >/dev/null
      echo "updated schedule $name -> $id"
    else
      aws scheduler create-schedule "${args[@]}" >/dev/null
      echo "created schedule $name -> $id"
    fi
  done
}

down() {
  for entry in "${RUNTIMES[@]}"; do
    local name="${entry##*:}"
    aws scheduler delete-schedule --name "$name" --region "$REGION" 2>/dev/null && echo "deleted $name" || true
  done
  aws iam delete-role-policy --role-name "$ROLE" --policy-name invoke-runtimes 2>/dev/null || true
  aws iam delete-role --role-name "$ROLE" 2>/dev/null && echo "deleted role $ROLE" || true
}

case "${1:-up}" in
  up) up ;;
  down) down ;;
  *) echo "usage: $0 [up|down]" >&2; exit 2 ;;
esac
