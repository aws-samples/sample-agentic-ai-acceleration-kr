#!/usr/bin/env bash
# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ============================================================================
# kube-prometheus-stack 설치 — production 관측성 + HPA 메트릭 소스
# ----------------------------------------------------------------------------
# 역할:
#   1. Prometheus: pod CPU/memory scrape (cAdvisor 경유), 장기 메트릭 보존
#   2. Grafana: 대시보드 / 알람 UI
#   3. kube-state-metrics: K8s 리소스 (Deployment/Pod/HPA) 메타 지표
#   4. **HPA 메트릭 소스**: EKS Fargate 에서 metrics-server 가 작동하지 않으므로
#      prometheus-adapter 가 이 Prometheus 를 읽어 metrics.k8s.io 를 서빙.
#
# 전제: Grafana IRSA role (Terraform 에서 생성됨). GRAFANA_ROLE_ARN env 또는
#       terraform output(prod) 에서 자동 읽음.
# Idempotent: 이미 설치돼 있으면 upgrade, 없으면 install.
# ============================================================================
set -euo pipefail

cd "$(dirname "$0")"

KPS_VERSION="${KPS_VERSION:-60.1.0}"
NS="${NS:-observability}"

R='\033[0;31m'; G='\033[0;32m'; Y='\033[1;33m'; B='\033[0;34m'; N='\033[0m'
info()  { echo -e "${B}ℹ${N}  $*"; }
ok()    { echo -e "${G}✓${N}  $*"; }
warn()  { echo -e "${Y}⚠${N}  $*"; }

# ─── Grafana IRSA role ARN (terraform output 또는 env) ─────────────────
if [ -z "${GRAFANA_ROLE_ARN:-}" ]; then
  info "GRAFANA_ROLE_ARN 미설정 → terraform output 에서 조회"
  if GRAFANA_ROLE_ARN=$(terraform -chdir=../../terraform/environments/${ENV:-prod} output -raw grafana_role_arn 2>/dev/null); then
    ok "GRAFANA_ROLE_ARN = $GRAFANA_ROLE_ARN"
  else
    warn "Grafana IRSA 미설정. CloudWatch 데이터소스를 사용할 수 없습니다 (Prometheus 메트릭은 정상)."
    warn "  필요 시: GRAFANA_ROLE_ARN=<ARN> 환경변수로 전달"
    GRAFANA_ROLE_ARN=""
  fi
fi

# ─── Namespace (observability 는 공용) ─────────────────────────────────
kubectl apply -f ../namespace.yaml

# ─── Grafana admin Secret (최초 1 회만 생성) ───────────────────────────
if ! kubectl -n "$NS" get secret kps-grafana-admin >/dev/null 2>&1; then
  info "Grafana admin Secret 최초 생성"
  bash ../admin-secret.sh
fi

# ─── Helm repo ─────────────────────────────────────────────────────────
info "Helm repo 추가 (idempotent)"
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update prometheus-community >/dev/null

# ─── Install / Upgrade ─────────────────────────────────────────────────
if helm status kps -n "$NS" >/dev/null 2>&1; then
    info "기존 릴리즈 감지 → upgrade 모드"
else
    info "신규 설치 모드"
fi

if [ -n "${GRAFANA_ROLE_ARN}" ]; then
  helm upgrade --install kps prometheus-community/kube-prometheus-stack \
    --namespace "$NS" \
    --version "$KPS_VERSION" \
    -f values.yaml \
    --set "grafana.serviceAccount.annotations.eks\.amazonaws\.com/role-arn=${GRAFANA_ROLE_ARN}" \
    --timeout 10m \
    --wait
else
  helm upgrade --install kps prometheus-community/kube-prometheus-stack \
    --namespace "$NS" \
    --version "$KPS_VERSION" \
    -f values.yaml \
    --timeout 10m \
    --wait
fi

ok "kube-prometheus-stack 설치 완료 (release=kps, ns=$NS, chart=$KPS_VERSION)"