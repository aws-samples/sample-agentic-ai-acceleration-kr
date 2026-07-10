#!/usr/bin/env bash
# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ==============================================================================
# install-onprem.sh — 고객 on-prem K8s 설치 스크립트
# ------------------------------------------------------------------------------
# 고객이 이 스크립트로 원클릭 설치 가능. 사전 준비:
#   1. kubectl 이 고객 사내 K8s 에 연결되어 있어야 함
#   2. 사내 PostgreSQL/Redis/SMTP 엔드포인트 정보
#   3. Helm 3.8+
#
# 사용법:
#   ./install-onprem.sh
#
# 환경 변수 override (선택):
#   NAMESPACE, RELEASE_NAME, VALUES_FILE
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CHART_DIR="$DEPLOY_DIR/charts/llm-gateway"

: "${NAMESPACE:=llm-gateway}"
: "${RELEASE_NAME:=llm-gateway}"
: "${VALUES_FILE:=$CHART_DIR/values-onprem-prod.yaml}"

R='\033[0;31m'; G='\033[0;32m'; Y='\033[1;33m'; B='\033[0;34m'; N='\033[0m'
info()  { echo -e "${B}ℹ${N}  $*"; }
ok()    { echo -e "${G}✓${N}  $*"; }
warn()  { echo -e "${Y}⚠${N}  $*"; }
err()   { echo -e "${R}✗${N}  $*" >&2; }

# ---- 전제 확인 ----
check_prereqs() {
    local missing=()
    for cmd in kubectl helm; do
        if ! command -v "$cmd" >/dev/null 2>&1; then
            missing+=("$cmd")
        fi
    done
    if [ ${#missing[@]} -gt 0 ]; then
        err "필요한 도구 누락: ${missing[*]}"
        exit 1
    fi

    if ! kubectl cluster-info >/dev/null 2>&1; then
        err "kubectl 이 K8s 클러스터에 연결되지 않았습니다."
        echo "    kubectl config current-context 로 현재 컨텍스트를 확인하세요."
        exit 1
    fi

    if [ ! -f "$VALUES_FILE" ]; then
        err "Values 파일 없음: $VALUES_FILE"
        exit 1
    fi

    # Helm 3.8 이상
    local helm_version
    helm_version=$(helm version --short | sed 's/^v//' | cut -d. -f1-2)
    if ! awk -v v="$helm_version" 'BEGIN {exit !(v >= 3.8)}'; then
        err "Helm 3.8 이상 필요 (현재 $helm_version)"
        exit 1
    fi
}

# ---- 네임스페이스 ----
ensure_namespace() {
    if kubectl get namespace "$NAMESPACE" >/dev/null 2>&1; then
        ok "Namespace '$NAMESPACE' 이미 존재"
    else
        info "Namespace '$NAMESPACE' 생성"
        kubectl create namespace "$NAMESPACE"
    fi
}

# ---- Secret 검증 ----
check_secrets() {
    info "K8s Secret 존재 확인"
    local missing=()
    for secret in llm-gateway-db llm-gateway-app; do
        if ! kubectl get secret "$secret" -n "$NAMESPACE" >/dev/null 2>&1; then
            missing+=("$secret")
        fi
    done

    # redis AUTH secret 은 values-onprem-prod.yaml 설정에 따라 필요할 수도
    # SMTP credentials 도 마찬가지

    if [ ${#missing[@]} -gt 0 ]; then
        err "필수 Secret 누락 (namespace: $NAMESPACE):"
        for s in "${missing[@]}"; do echo "       - $s"; done
        echo ""
        echo "  아래 형식으로 생성하세요 (예시):"
        echo ""
        cat <<EOF
  kubectl create secret generic llm-gateway-db -n $NAMESPACE \\
    --from-literal=password='<gateway 유저 비번>' \\
    --from-literal=notification_worker_password='<notification_worker_user 비번>'

  kubectl create secret generic llm-gateway-app -n $NAMESPACE \\
    --from-literal=virtual_key_encryption_key="\$(openssl rand -hex 32)" \\
    --from-literal=nextauth_secret="\$(openssl rand -hex 32)" \\
    --from-literal=jwt_jwks_cache_key="\$(openssl rand -hex 32)"

  # Redis AUTH (선택, values-onprem-prod.yaml에서 passwordSecretName 설정 시)
  kubectl create secret generic llm-gateway-redis -n $NAMESPACE \\
    --from-literal=password='<redis AUTH 토큰>'

  # SMTP (선택, provider=smtp 일 때)
  kubectl create secret generic llm-gateway-smtp -n $NAMESPACE \\
    --from-literal=username='<SMTP 유저>' \\
    --from-literal=password='<SMTP 비번>'

  # TLS 인증서 (Ingress TLS 시)
  kubectl create secret tls llm-gateway-tls-gateway -n $NAMESPACE \\
    --cert=./gateway.crt --key=./gateway.key
  # admin-ui / admin-api 각각 동일하게

  # Image pull secret (private registry)
  kubectl create secret docker-registry harbor-registry -n $NAMESPACE \\
    --docker-server=harbor.customer.internal \\
    --docker-username='<username>' \\
    --docker-password='<password>'
EOF
        echo ""
        exit 1
    fi

    ok "Secret 확인 완료"
}

# ---- DB/Redis 연결 pre-flight ----
preflight_check() {
    info "DB/Redis 연결 검증 (사전 테스트 Pod 실행)"
    warn "  사내 DB/Redis 엔드포인트가 values 파일에 올바르게 설정됐는지 수동 확인 필요"
    echo "     - database.external.host = $(grep -A2 'external:' "$VALUES_FILE" | grep 'host:' | head -1)"
    echo "     - redis.external.host = $(grep -A5 'redis:' "$VALUES_FILE" | grep 'host:' | head -1)"
    echo ""
    read -p "  위 엔드포인트가 정확합니까? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        err "values 파일을 편집한 뒤 다시 실행하세요."
        exit 1
    fi
}

# ---- Helm install / upgrade ----
helm_install() {
    local action
    if helm status "$RELEASE_NAME" -n "$NAMESPACE" >/dev/null 2>&1; then
        action="upgrade"
        info "기존 릴리즈 감지 → upgrade 모드"
    else
        action="install"
        info "신규 설치 모드"
    fi

    helm "$action" "$RELEASE_NAME" "$CHART_DIR" \
        --namespace "$NAMESPACE" \
        --values "$VALUES_FILE" \
        --atomic \
        --cleanup-on-fail \
        --timeout 15m \
        --wait

    ok "Helm $action 완료"
}

verify_deployment() {
    info "Pod 상태 확인"
    kubectl get pods -n "$NAMESPACE" -l "app.kubernetes.io/instance=${RELEASE_NAME}"
    echo ""
    info "Ingress 확인"
    kubectl get ingress -n "$NAMESPACE" -l "app.kubernetes.io/instance=${RELEASE_NAME}"
}

main() {
    echo "=============================================================="
    echo "  LLM Gateway — On-prem 설치"
    echo "  namespace : $NAMESPACE"
    echo "  values    : $VALUES_FILE"
    echo "=============================================================="

    check_prereqs
    ensure_namespace
    check_secrets
    preflight_check
    helm_install
    verify_deployment

    echo ""
    ok "설치 완료"
    echo ""
    echo "다음 단계:"
    echo "    ./scripts/smoke-test.sh --onprem"
}

main "$@"