#!/usr/bin/env bash
# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
# ==============================================================================
# provision_tool_gateway.sh — opt-in AgentCore Tool Gateway (multi-engine search)
#   deploy   : terraform init+apply (tool-gateway-dev) → seed secrets → emit dashboard env
#   status   : print gateway id/url + enabled engines, or "not deployed"
#   teardown : terraform destroy (stops billing/exposure)
#
# This is INDEPENDENT of provision_agentcore_websearch.py (AWS_IAM single WebSearch).
# It provisions the CUSTOM_JWT multi-tool gateway used by the admin-ui Tool Gateway dashboard.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TF_DIR="$DEPLOY_DIR/terraform/environments/tool-gateway-dev"
GEN_ENV_FILE="$DEPLOY_DIR/tool-gateway/dashboard.generated.env"
REGION="${TOOL_GATEWAY_REGION:-us-east-1}"

R='\033[0;31m'; G='\033[0;32m'; Y='\033[1;33m'; B='\033[0;34m'; N='\033[0m'
info(){ echo -e "${B}ℹ${N}  $*"; }
ok(){ echo -e "${G}✓${N}  $*"; }
warn(){ echo -e "${Y}⚠${N}  $*"; }
err(){ echo -e "${R}✗${N}  $*" >&2; }

require_tools(){ for t in terraform jq aws; do command -v "$t" >/dev/null || { err "$t not found"; exit 1; }; done; }

cmd_status(){
  if [ ! -d "$TF_DIR/.terraform" ]; then warn "not initialized — run '$0 deploy'"; return 0; fi
  pushd "$TF_DIR" >/dev/null
  if ! terraform output -json >/dev/null 2>&1; then warn "not deployed (no state outputs)"; popd >/dev/null; return 0; fi
  local o; o=$(terraform output -json)
  echo "gateway_id : $(echo "$o" | jq -r '.gateway_id.value // "-"')"
  echo "gateway_url: $(echo "$o" | jq -r '.gateway_url.value // "-"')"
  echo "engines    : $(echo "$o" | jq -rc '.enabled_engines.value // []')"
  echo "region     : $(echo "$o" | jq -r '.region.value // "-"')"
  popd >/dev/null
}

cmd_deploy(){
  require_tools
  [ -f "$TF_DIR/terraform.tfvars" ] || { err "create $TF_DIR/terraform.tfvars from terraform.tfvars.example first"; exit 1; }
  pushd "$TF_DIR" >/dev/null
  info "terraform init"
  terraform init -input=false >/dev/null
  info "terraform apply (region=$REGION)"
  terraform apply -auto-approve -input=false
  local o; o=$(terraform output -json)
  popd >/dev/null
  # Seed API keys if a key file is provided (optional).
  if [ -n "${TOOL_KEY_FILE:-}" ] && [ -f "$TOOL_KEY_FILE" ]; then
    info "seeding tool secrets from $TOOL_KEY_FILE"
    AWS_REGION="$REGION" "$SCRIPT_DIR/seed-tool-secrets.sh" "$TOOL_KEY_FILE"
  else
    warn "TOOL_KEY_FILE unset — skipping secret seeding (DuckDuckGo works keyless)"
  fi
  # Emit dashboard env for admin-ui (Phase 3 consumes this).
  echo "$o" | jq -r '.dashboard_env.value | to_entries[] | "\(.key)=\(.value)"' > "$GEN_ENV_FILE"
  ok "wrote dashboard env → $GEN_ENV_FILE"
  cmd_status
}

cmd_teardown(){
  require_tools
  [ -d "$TF_DIR/.terraform" ] || { warn "nothing to tear down"; return 0; }
  pushd "$TF_DIR" >/dev/null
  warn "destroying tool-gateway-dev resources"
  terraform destroy -auto-approve -input=false
  popd >/dev/null
  rm -f "$GEN_ENV_FILE"
  ok "teardown complete"
}

case "${1:-status}" in
  deploy) cmd_deploy ;;
  status) cmd_status ;;
  teardown) cmd_teardown ;;
  *) err "usage: $0 {deploy|status|teardown}"; exit 2 ;;
esac
