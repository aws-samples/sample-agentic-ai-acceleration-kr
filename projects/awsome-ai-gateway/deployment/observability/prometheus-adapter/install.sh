#!/usr/bin/env bash
# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ============================================================================
# prometheus-adapter 설치 — metrics.k8s.io API 를 Prometheus 기반으로 서빙
# ----------------------------------------------------------------------------
# 전제: kube-prometheus-stack 이 observability ns 에 먼저 설치돼 있어야 함.
# 설치 후 HPA 의 cpu: <unknown>/65% 가 실제 수치로 바뀜.
# ============================================================================
set -euo pipefail

cd "$(dirname "$0")"

CHART_VERSION="${CHART_VERSION:-4.11.0}"
NS="${NS:-observability}"
RELEASE="${RELEASE:-prometheus-adapter}"

R='\033[0;31m'; G='\033[0;32m'; Y='\033[1;33m'; B='\033[0;34m'; N='\033[0m'
info()  { echo -e "${B}ℹ${N}  $*"; }
ok()    { echo -e "${G}✓${N}  $*"; }
warn()  { echo -e "${Y}⚠${N}  $*"; }

# ─── 0. Prometheus Service 존재 확인 ──────────────────────────────────
info "Prometheus Service 존재 확인 (kps-prometheus)"
if ! kubectl -n "$NS" get svc kps-prometheus >/dev/null 2>&1; then
    warn "kps-prometheus Service 없음. kube-prometheus-stack 먼저 설치하세요."
    exit 1
fi
ok "Prometheus Service 확인"

# ─── 0.5. metrics-server 충돌 방지 ────────────────────────────────────
# prometheus-adapter 가 apiService metrics.k8s.io/v1beta1 을 소유해야 하므로
# 기존 metrics-server 가 등록돼 있으면 충돌. 경고만 하고 진행 (helm 이 덮어씀).
if kubectl get apiservice v1beta1.metrics.k8s.io -o jsonpath='{.spec.service.name}' 2>/dev/null | grep -q metrics-server; then
    warn "기존 metrics-server 가 metrics.k8s.io APIService 를 소유 중. helm uninstall 권장:"
    warn "  helm uninstall metrics-server -n kube-system"
fi

# ─── 1. Helm repo ─────────────────────────────────────────────────────
info "Helm repo 추가 (idempotent)"
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update prometheus-community >/dev/null

# ─── 2. Install / Upgrade ─────────────────────────────────────────────
if helm status "$RELEASE" -n "$NS" >/dev/null 2>&1; then
    info "기존 릴리즈 감지 → upgrade 모드"
else
    info "신규 설치 모드"
fi

helm upgrade --install "$RELEASE" prometheus-community/prometheus-adapter \
    --namespace "$NS" \
    --version "$CHART_VERSION" \
    -f values.yaml \
    --timeout 5m \
    --wait

# ─── 3. 검증 ──────────────────────────────────────────────────────────
info "metrics.k8s.io API 가용 확인 (최대 2 분)"
deadline=$((SECONDS + 120))
while [ $SECONDS -lt $deadline ]; do
    if kubectl get --raw '/apis/metrics.k8s.io/v1beta1/pods' >/dev/null 2>&1; then
        ok "metrics.k8s.io API 응답 정상"
        break
    fi
    sleep 5
done

if ! kubectl get --raw '/apis/metrics.k8s.io/v1beta1/pods' >/dev/null 2>&1; then
    warn "metrics API 가 2 분 안에 ready 되지 않음."
    warn "  kubectl logs -n $NS -l app.kubernetes.io/name=prometheus-adapter --tail=30"
    exit 1
fi

# 초기 수집 대기 후 sanity
sleep 20
if kubectl top pod -n llm-gateway --no-headers >/dev/null 2>&1; then
    ok "kubectl top pod -n llm-gateway 정상 동작"
else
    warn "kubectl top 아직 데이터 없음 (collect window 3m 대기 필요할 수 있음)."
fi

ok "prometheus-adapter 설치 완료 (release=$RELEASE, ns=$NS, chart=$CHART_VERSION)"