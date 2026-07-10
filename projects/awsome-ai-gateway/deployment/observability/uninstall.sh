#!/usr/bin/env bash
# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ============================================================================
# Observability 스택 제거 (kube-prometheus-stack + prometheus-adapter + OTel)
# ----------------------------------------------------------------------------
# ⚠️ 제거 시 HPA 가 `<unknown>` 으로 돌아가 모든 Deployment 가 minReplicas 고정됨.
# ⚠️ Prometheus 데이터는 emptyDir 이라 uninstall 시 소실 — 필요한 메트릭은 사전 export.
# ============================================================================
set -euo pipefail

cd "$(dirname "$0")"

NS="observability"

read -p "Observability 스택 제거 (HPA 영향 있음). 계속? (y/N): " confirm
if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
  echo "Aborted."
  exit 0
fi

# OTel Collector 제거
kubectl delete -f otel-collector/deployment.yaml --ignore-not-found
kubectl delete -f otel-collector/service.yaml --ignore-not-found
kubectl delete -f otel-collector/configmap.yaml --ignore-not-found
kubectl delete -f otel-collector/serviceaccount.yaml --ignore-not-found

# prometheus-adapter (metrics.k8s.io 제공자) 제거
helm uninstall prometheus-adapter -n "$NS" --wait || true

# kube-prometheus-stack 제거
helm uninstall kps -n "$NS" --wait || true

# namespace (scheduled 리소스 남아있으면 Terminating 상태로 머무를 수 있음)
kubectl delete namespace "$NS" --wait=false || true

echo "✓ Observability 스택 제거 완료"