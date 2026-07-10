#!/usr/bin/env bash
# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ============================================================================
# Observability 스택 일괄 설치 (kube-prometheus-stack + prometheus-adapter + OTel)
# ----------------------------------------------------------------------------
# 개별 설치 스크립트의 편의 wrapper. install-eks.sh 의 ensure_observability 가
# 각 sub-script 를 개별 호출하므로, 본 스크립트는 **수동 설치 시에만** 사용.
# ============================================================================
set -euo pipefail

cd "$(dirname "$0")"

R='\033[0;31m'; G='\033[0;32m'; Y='\033[1;33m'; B='\033[0;34m'; N='\033[0m'
info() { echo -e "${B}ℹ${N}  $*"; }
ok()   { echo -e "${G}✓${N}  $*"; }

# ─── 1. kube-prometheus-stack ───────────────────────────────────────
info "[1/3] kube-prometheus-stack 설치"
bash kube-prometheus-stack/install.sh

# ─── 2. prometheus-adapter (HPA 소스) ──────────────────────────────
info "[2/3] prometheus-adapter 설치 (metrics.k8s.io)"
bash prometheus-adapter/install.sh

# ─── 3. OTel Collector ─────────────────────────────────────────────
info "[3/3] OTel Collector 적용"
kubectl apply -f namespace.yaml
kubectl apply -f otel-collector/serviceaccount.yaml
kubectl apply -f otel-collector/configmap.yaml
kubectl apply -f otel-collector/service.yaml
kubectl apply -f otel-collector/deployment.yaml
kubectl -n observability rollout status deployment/otel-collector --timeout=3m
ok "OTel Collector ready"

# ─── 4. Gateway rollout restart (OTel endpoint 재연결, 선택) ───────
GW_NS="llm-gateway"
if [ "${SKIP_GATEWAY_RESTART:-false}" != "true" ] \
   && kubectl -n "$GW_NS" get deployment llm-gateway-gateway-proxy >/dev/null 2>&1; then
  info "Gateway pods rollout restart (OTel endpoint 연결 refresh)..."
  kubectl -n "$GW_NS" rollout restart \
    deployment/llm-gateway-gateway-proxy \
    deployment/llm-gateway-admin-api \
    deployment/llm-gateway-cost-recorder-worker || true
  kubectl -n "$GW_NS" rollout status deployment/llm-gateway-gateway-proxy --timeout=5m
fi

# ─── 5. 접속 안내 ─────────────────────────────────────────────────
NS="observability"
cat <<EOF

============================================================================
설치 완료.

Grafana:
  kubectl port-forward -n $NS svc/kps-grafana 3000:80
  → http://localhost:3000
    ID: admin
    PW: $(kubectl -n $NS get secret kps-grafana-admin -o jsonpath='{.data.admin-password}' | base64 -d)

Prometheus UI:
  kubectl port-forward -n $NS svc/kps-prometheus 9090:9090

HPA metrics API 검증:
  kubectl top pod -n llm-gateway
  kubectl -n llm-gateway get hpa
============================================================================
EOF