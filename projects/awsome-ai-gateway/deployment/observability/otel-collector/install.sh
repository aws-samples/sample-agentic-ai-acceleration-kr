#!/usr/bin/env bash
# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ============================================================================
# otel-collector 설치 — gateway-proxy / admin-api 의 OTLP traces & metrics 수신
# ----------------------------------------------------------------------------
# gateway-proxy 가 OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317 으로
# traces & custom metrics 를 push. collector 는 이를 kps Prometheus 로 remote-write.
#
# 없어도 gateway 응답 경로에는 영향 없으나 (fire-and-forget) Grafana 에서
# gateway_request_total 등 custom 메트릭 시리즈가 뜨지 않음.
#
# 단순 kubectl apply 로 배포 (Helm 아님). 4개 YAML: SA / ConfigMap / Deployment / Service.
# Idempotent: 이미 있으면 apply 가 업데이트로 동작.
# ============================================================================
set -euo pipefail

cd "$(dirname "$0")"

NS="${NS:-observability}"

R='\033[0;31m'; G='\033[0;32m'; Y='\033[1;33m'; B='\033[0;34m'; N='\033[0m'
info()  { echo -e "${B}ℹ${N}  $*"; }
ok()    { echo -e "${G}✓${N}  $*"; }
warn()  { echo -e "${Y}⚠${N}  $*"; }

info "otel-collector 설치/갱신 (ns=$NS)"

# Namespace 존재 확인 (kube-prometheus-stack 이 이미 만들어둠)
if ! kubectl get ns "$NS" >/dev/null 2>&1; then
    warn "Namespace '$NS' 없음. kube-prometheus-stack 먼저 설치하세요."
    exit 1
fi

# 4개 매니페스트 순서대로 적용 (SA → ConfigMap → Deployment → Service)
kubectl apply -f serviceaccount.yaml
kubectl apply -f configmap.yaml
kubectl apply -f deployment.yaml
kubectl apply -f service.yaml

# Rollout 대기 (최대 2분)
info "Deployment rollout 대기"
if kubectl -n "$NS" rollout status deployment/otel-collector --timeout=120s >/dev/null 2>&1; then
    ok "otel-collector Ready (ns=$NS)"
else
    warn "rollout timeout — 'kubectl -n $NS describe deployment otel-collector' 로 확인"
    exit 1
fi