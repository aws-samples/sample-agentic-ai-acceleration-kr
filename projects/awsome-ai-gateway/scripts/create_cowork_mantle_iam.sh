#!/usr/bin/env bash
# Copyright 2026 © Amazon.com and Affiliates.
#
# One-time IAM setup for Cowork → Bedrock Mantle cross-account routing (Phase 2).
#
#   - In the 222 (Cowork) account: create role `llm-gateway-cowork-bedrock`
#     trusted by the 333 gateway-proxy IRSA role (with ExternalId), granting
#     Bedrock invoke/stream/count on Tokyo (ap-northeast-1) Opus 4.8.
#   - In the 333 (dev) account: grant the gateway-proxy IRSA role sts:AssumeRole
#     on that 222 role.
#
# No long-lived 222 keys are stored anywhere — the running gateway uses STS temp
# creds via its IRSA identity. The 222 creds in .env are used ONLY by this
# one-time bootstrap script. Idempotent where the AWS API allows.
#
# Run from the repo root with the .env present. Requires the 222 creds (lines
# 9-10 of .env) and the 333 creds (lines 4-5).
set -euo pipefail

# ---- fixed identifiers (verified 2026-06-20) ----
ROLE_NAME="llm-gateway-cowork-bedrock"
ACCOUNT_222="222233334444"
ACCOUNT_333="333344445555"
EXTERNAL_ID="cowork-bedrock"
MANTLE_REGION="ap-northeast-1"
GW_ROLE_NAME="llm-gateway-dev-gateway-proxy-bedrock"
GW_ROLE_ARN="arn:aws:iam::${ACCOUNT_333}:role/${GW_ROLE_NAME}"

# scripts/ sits directly under the repo root in this tree, so .env is one level up.
ENV_FILE="$(cd "$(dirname "$0")/.." && pwd)/.env"
[ -f "$ENV_FILE" ] || { echo "ABORT: .env not found at $ENV_FILE"; exit 1; }

# .env layout (verified): lines 4-5 = 333 (Claude Code) creds; lines 9-10 = 222 (Cowork) creds.
KEY_333="$(sed -n '4p' "$ENV_FILE" | cut -d= -f2-)"
SEC_333="$(sed -n '5p' "$ENV_FILE" | cut -d= -f2-)"
KEY_222="$(sed -n '9p' "$ENV_FILE" | cut -d= -f2-)"
SEC_222="$(sed -n '10p' "$ENV_FILE" | cut -d= -f2-)"

# ───────────────────────────────────────────────────────────────────────────
# Part A — in the 222 (Cowork) account: create/update the cross-account role.
# ───────────────────────────────────────────────────────────────────────────
export AWS_ACCESS_KEY_ID="$KEY_222"
export AWS_SECRET_ACCESS_KEY="$SEC_222"
export AWS_REGION="$MANTLE_REGION"
unset AWS_SESSION_TOKEN 2>/dev/null || true

CALLER="$(aws sts get-caller-identity --query Account --output text)"
[ "$CALLER" = "$ACCOUNT_222" ] || { echo "ABORT: expected 222 ($ACCOUNT_222), got $CALLER"; exit 1; }
echo "[222] caller confirmed = $ACCOUNT_222"

TRUST="$(cat <<JSON
{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
  "Principal":{"AWS":"${GW_ROLE_ARN}"},
  "Action":"sts:AssumeRole",
  "Condition":{"StringEquals":{"sts:ExternalId":"${EXTERNAL_ID}"}}}]}
JSON
)"

PERMS="$(cat <<JSON
{"Version":"2012-10-17","Statement":[
  {"Sid":"MantleInvoke","Effect":"Allow",
   "Action":["bedrock:InvokeModel","bedrock:InvokeModelWithResponseStream","bedrock:CountTokens"],
   "Resource":[
     "arn:aws:bedrock:${MANTLE_REGION}::foundation-model/anthropic.claude-opus-4-8",
     "arn:aws:bedrock:${MANTLE_REGION}:${ACCOUNT_222}:inference-profile/*claude-opus-4-8*"]},
  {"Sid":"MantleList","Effect":"Allow","Action":["bedrock:ListFoundationModels"],"Resource":"*"}]}
JSON
)"

if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  echo "[222] role exists — updating trust policy"
  aws iam update-assume-role-policy --role-name "$ROLE_NAME" --policy-document "$TRUST" >/dev/null
else
  echo "[222] creating role $ROLE_NAME"
  aws iam create-role --role-name "$ROLE_NAME" \
    --assume-role-policy-document "$TRUST" \
    --description "333 gateway -> 222 Bedrock Mantle (Cowork Opus 4.8, Tokyo)" \
    --max-session-duration 3600 >/dev/null
fi
aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name "mantle-invoke" \
  --policy-document "$PERMS" >/dev/null
echo "[222] role ready: arn:aws:iam::${ACCOUNT_222}:role/${ROLE_NAME}"

# ───────────────────────────────────────────────────────────────────────────
# Part B — in the 333 (dev) account: grant the gateway IRSA role sts:AssumeRole.
# ───────────────────────────────────────────────────────────────────────────
export AWS_ACCESS_KEY_ID="$KEY_333"
export AWS_SECRET_ACCESS_KEY="$SEC_333"
export AWS_REGION="ap-northeast-2"

CALLER="$(aws sts get-caller-identity --query Account --output text)"
[ "$CALLER" = "$ACCOUNT_333" ] || { echo "ABORT: expected 333 ($ACCOUNT_333), got $CALLER"; exit 1; }
echo "[333] caller confirmed = $ACCOUNT_333"

aws iam put-role-policy --role-name "$GW_ROLE_NAME" --policy-name "assume-cowork-mantle" \
  --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"sts:AssumeRole\",\"Resource\":\"arn:aws:iam::${ACCOUNT_222}:role/${ROLE_NAME}\"}]}" >/dev/null
echo "[333] granted ${GW_ROLE_NAME} sts:AssumeRole on the 222 Mantle role"

echo ""
echo "DONE. Verify with:"
echo "  aws sts assume-role --role-arn arn:aws:iam::${ACCOUNT_222}:role/${ROLE_NAME} \\"
echo "    --role-session-name gw-mantle-verify --external-id ${EXTERNAL_ID} \\"
echo "    --query Credentials.AccessKeyId --output text   # (run with 333 creds)"
