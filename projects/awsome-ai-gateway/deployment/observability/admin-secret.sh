#!/usr/bin/env bash
# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ============================================================================
# Grafana admin password 생성 & K8s Secret 등록
# ----------------------------------------------------------------------------
# 출력된 비밀번호를 반드시 기록해둘 것. (재조회 시 secret 에서 decode 가능)
# ============================================================================
set -euo pipefail

NS="observability"
SECRET_NAME="kps-grafana-admin"

kubectl get ns "$NS" >/dev/null 2>&1 || kubectl apply -f "$(dirname "$0")/namespace.yaml"

# 이미 있으면 skip (덮어쓰려면 --force-recreate 1)
if [ "${FORCE_RECREATE:-0}" != "1" ] && kubectl -n "$NS" get secret "$SECRET_NAME" >/dev/null 2>&1; then
  echo "[skip] Secret '$SECRET_NAME' 이미 존재. FORCE_RECREATE=1 로 덮어쓰기."
  PASS=$(kubectl -n "$NS" get secret "$SECRET_NAME" -o jsonpath='{.data.admin-password}' | base64 -d)
  echo "current admin password (이미 생성된 값): $PASS"
  exit 0
fi

PASS=$(openssl rand -base64 24 | tr -d '=+/\n' | cut -c1-24)

kubectl -n "$NS" create secret generic "$SECRET_NAME" \
  --from-literal=admin-user=admin \
  --from-literal=admin-password="$PASS" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "---"
echo "Grafana admin credentials created in ${NS}/${SECRET_NAME}"
echo "  username: admin"
echo "  password: $PASS"
echo "---"
echo "⚠️  위 비밀번호를 기록해두세요. (secret 에서 재조회 가능:"
echo "   kubectl -n $NS get secret $SECRET_NAME -o jsonpath='{.data.admin-password}' | base64 -d)"