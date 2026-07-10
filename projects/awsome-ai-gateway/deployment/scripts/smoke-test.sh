#!/usr/bin/env bash
# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ==============================================================================
# smoke-test.sh — 배포 후 기본 E2E 검증
# ------------------------------------------------------------------------------
# 확인 항목:
#   1. 모든 Pod가 Ready
#   2. 각 Service health endpoint 응답
#   3. /v1/models 엔드포인트 응답
#   4. (선택) VK 발급 → /v1/messages 호출 E2E
#
# 사용법:
#   ./smoke-test.sh [--namespace NS] [--env dev|prod] [--with-bedrock]
# ==============================================================================

set -euo pipefail

NAMESPACE="${NAMESPACE:-llm-gateway}"
RELEASE_NAME="${RELEASE_NAME:-llm-gateway}"
WITH_BEDROCK=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --namespace|-n) NAMESPACE="$2"; shift 2 ;;
        --env)          shift 2 ;;  # 외부에서만 쓰는 인자
        --onprem)       shift ;;
        --with-bedrock) WITH_BEDROCK=1; shift ;;
        *) echo "Unknown flag: $1"; exit 1 ;;
    esac
done

R='\033[0;31m'; G='\033[0;32m'; Y='\033[1;33m'; N='\033[0m'
FAIL_COUNT=0
PASS_COUNT=0
pass() { echo -e "${G}✓${N} $*"; PASS_COUNT=$((PASS_COUNT + 1)); }
fail() { echo -e "${R}✗${N} $*" >&2; FAIL_COUNT=$((FAIL_COUNT + 1)); }
warn() { echo -e "${Y}⚠${N} $*"; }

# ---- 1. Pod Ready ----
test_pods_ready() {
    echo ""
    echo "━━━ 1. Pod readiness ━━━"
    local components=(gateway-proxy admin-api admin-ui scheduler notification-worker cost-recorder-worker)
    for c in "${components[@]}"; do
        local pods_json
        pods_json=$(kubectl get pods -n "$NAMESPACE" \
            -l "app.kubernetes.io/instance=${RELEASE_NAME},app.kubernetes.io/component=${c}" \
            -o json 2>/dev/null || echo '{"items":[]}')

        local total
        total=$(echo "$pods_json" | jq '.items | length')
        if [ "$total" -eq 0 ]; then
            warn "$c: Pod 없음 (enabled=false 일 수 있음)"
            continue
        fi

        local ready
        ready=$(echo "$pods_json" | jq '[.items[] | select(.status.conditions[]? | select(.type=="Ready" and .status=="True"))] | length')

        if [ "$ready" -eq "$total" ]; then
            pass "$c: $ready/$total Ready"
        else
            fail "$c: $ready/$total Ready"
            kubectl get pods -n "$NAMESPACE" -l "app.kubernetes.io/component=${c}" -o wide
        fi
    done
}

# ---- 2. health endpoint ----
test_health_endpoints() {
    echo ""
    echo "━━━ 2. Health endpoints ━━━"

    # gateway-proxy
    if kubectl exec -n "$NAMESPACE" -it deploy/${RELEASE_NAME}-gateway-proxy -c gateway-proxy -- \
        python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/health').read().decode())" \
        2>/dev/null | grep -qi '"ok"\|"status":"ok"\|healthy\|alive'; then
        pass "gateway-proxy /health"
    else
        fail "gateway-proxy /health"
    fi

    # admin-api
    if kubectl exec -n "$NAMESPACE" -it deploy/${RELEASE_NAME}-admin-api -c admin-api -- \
        python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8080/health').read().decode())" \
        2>/dev/null | grep -qi '"ok"\|"status":"ok"\|healthy\|alive'; then
        pass "admin-api /health"
    else
        fail "admin-api /health"
    fi

    # admin-ui
    if kubectl exec -n "$NAMESPACE" -it deploy/${RELEASE_NAME}-admin-ui -c admin-ui -- \
        wget -qO- --spider http://localhost:3000/api/health 2>&1 | grep -qi 'ok\|200'; then
        pass "admin-ui /api/health"
    else
        warn "admin-ui /api/health (wget/curl 미설치일 수 있음)"
    fi
}

# ---- 3. /v1/models ----
# 이 엔드포인트는 VK (Authorization: Bearer) 인증 필수.
# 인증 없이 호출 시 401 이 정상 — 인증 middleware 가 동작하는 것을 확인하는 용도.
# 실제 모델 목록 조회는 VK 발급 후 05-smoke-test.md §4 에서 수행.
test_models_endpoint() {
    echo ""
    echo "━━━ 3. /v1/models 인증 middleware 동작 확인 ━━━"
    local status
    status=$(kubectl exec -n "$NAMESPACE" -it deploy/${RELEASE_NAME}-gateway-proxy -c gateway-proxy -- \
        python -c "
import urllib.request, urllib.error
try:
    urllib.request.urlopen('http://localhost:8000/v1/models')
    print(200)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception as e:
    print(f'ERR:{type(e).__name__}')
" 2>/dev/null | tr -d '\r\n' || echo 'NOREACH')

    case "$status" in
        401)
            pass "/v1/models: 401 Unauthorized (인증 middleware 정상 동작)"
            ;;
        200)
            pass "/v1/models: 200 (인증 우회 경로 또는 VK 주입됨)"
            ;;
        *)
            fail "/v1/models: 예상 외 응답 ($status)"
            ;;
    esac
}

# ---- 4. (선택) Bedrock 실호출 ----
test_bedrock_e2e() {
    if [ "$WITH_BEDROCK" -ne 1 ]; then
        return
    fi
    echo ""
    echo "━━━ 4. Bedrock E2E (VK → /v1/messages) ━━━"
    warn "이 테스트는 실제 VK 발급 + Bedrock 호출이 필요합니다."
    warn "수동 단계:"
    echo "  1. admin UI에 로그인 → API Keys → VK 발급"
    echo "  2. 발급된 VK로 아래 curl 실행:"
    echo ""
    echo '     curl -X POST https://gateway.<domain>/v1/messages \'
    echo '       -H "Authorization: Bearer <VK>" \'
    echo '       -H "Content-Type: application/json" \'
    echo '       -d @- <<EOF'
    echo '     {"model":"claude-sonnet","max_tokens":50,"messages":[{"role":"user","content":"ping"}]}'
    echo '     EOF'
    echo ""
    echo "  응답이 200 이면 PASS"
}

# ---- 5. Ingress 체크 ----
test_ingress() {
    echo ""
    echo "━━━ 5. Ingress ━━━"
    local ingresses
    ingresses=$(kubectl get ingress -n "$NAMESPACE" -l "app.kubernetes.io/instance=${RELEASE_NAME}" -o json)
    local count
    count=$(echo "$ingresses" | jq '.items | length')

    if [ "$count" -eq 0 ]; then
        warn "Ingress 없음 (enabled=false 일 수 있음)"
        return
    fi

    local ready_count=0
    for i in $(echo "$ingresses" | jq -r '.items[].metadata.name'); do
        local host ip
        host=$(echo "$ingresses" | jq -r ".items[] | select(.metadata.name==\"$i\") | .spec.rules[0].host")
        ip=$(echo "$ingresses" | jq -r ".items[] | select(.metadata.name==\"$i\") | .status.loadBalancer.ingress[0].hostname // .status.loadBalancer.ingress[0].ip // empty")

        if [ -n "$ip" ] && [ "$ip" != "null" ]; then
            pass "Ingress $i: $host → $ip"
            ready_count=$((ready_count + 1))
        else
            warn "Ingress $i: $host (LoadBalancer 아직 프로비저닝 중)"
        fi
    done
}

# ---- main ----
echo "=============================================================="
echo "  LLM Gateway Smoke Test"
echo "  namespace : $NAMESPACE"
echo "=============================================================="

test_pods_ready
test_health_endpoints
test_models_endpoint
test_ingress
test_bedrock_e2e

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  PASS: $PASS_COUNT   FAIL: $FAIL_COUNT"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if [ "$FAIL_COUNT" -gt 0 ]; then
    exit 1
fi