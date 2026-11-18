#!/usr/bin/env bash
# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ==============================================================================
# smoke-test.sh — 배포 후 기본 E2E 검증
# ------------------------------------------------------------------------------
# 확인 항목:
#   1. 모든 Pod가 Ready
#   2. 각 Service health endpoint 응답
#   3. /v1/models 엔드포인트 응답
#   4. (선택) Bedrock 실호출 — IRSA/STS/VPC endpoint/IAM/모델접근 + (dev 만) 앱 경로
#   5. Ingress
#
# 사용법:
#   ./smoke-test.sh [--namespace NS] [--env dev|prod] [--context CTX] [--with-bedrock]
#                   [--model-id ID]
#
# --context 를 주지 않으면 (기존과 동일하게) kubectl 의 현재 활성 컨텍스트를 쓴다.
# ==============================================================================

set -euo pipefail

NAMESPACE="${NAMESPACE:-llm-gateway}"
RELEASE_NAME="${RELEASE_NAME:-llm-gateway}"
WITH_BEDROCK=0
KUBE_CONTEXT="${KUBE_CONTEXT:-}"

# --with-bedrock 에서 실제로 호출할 Bedrock 모델. dev/prod 양쪽 model.model_aliases 에서
# ACTIVE 이고 BEDROCK(native) 중 가장 싼 것 = Haiku 4.5 (input $1.00/1M).
# dev·prod alias 테이블은 동일하다(2026-09-09 실측: 양쪽 20 행, alias/provider_model_id 일치).
BEDROCK_MODEL_ID="${BEDROCK_SMOKE_MODEL_ID:-global.anthropic.claude-haiku-4-5-20251001-v1:0}"
# 앱 경로(/v1/messages)로 보낼 alias(= provider_model_id 가 아니라 게이트웨이 alias).
BEDROCK_MODEL_ALIAS="${BEDROCK_SMOKE_MODEL_ALIAS:-claude-haiku-4-5-20251001}"
BEDROCK_EXEC_TIMEOUT="${BEDROCK_SMOKE_EXEC_TIMEOUT:-180s}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --namespace|-n) NAMESPACE="$2"; shift 2 ;;
        --env)          shift 2 ;;  # 외부에서만 쓰는 인자
        --context)      KUBE_CONTEXT="$2"; shift 2 ;;
        --model-id)     BEDROCK_MODEL_ID="$2"; shift 2 ;;
        --with-bedrock) WITH_BEDROCK=1; shift ;;
        *) echo "Unknown flag: $1"; exit 1 ;;
    esac
done

# kubectl 래퍼. KUBE_CONTEXT 가 비어 있으면 그대로 `kubectl ...` = 기존 동작 그대로
# (활성 컨텍스트 사용). --context 를 명시했을 때만 인자가 붙는다.
# NOTE: helm 은 --kube-context, kubectl 은 --context 다.
kc() { kubectl ${KUBE_CONTEXT:+--context "$KUBE_CONTEXT"} "$@"; }

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
        pods_json=$(kc get pods -n "$NAMESPACE" \
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
            kc get pods -n "$NAMESPACE" -l "app.kubernetes.io/component=${c}" -o wide
        fi
    done
}

# ---- 2. health endpoint ----
test_health_endpoints() {
    echo ""
    echo "━━━ 2. Health endpoints ━━━"

    # gateway-proxy
    if kc exec -n "$NAMESPACE" -it deploy/${RELEASE_NAME}-gateway-proxy -c gateway-proxy -- \
        python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/health').read().decode())" \
        2>/dev/null | grep -qi '"ok"\|"status":"ok"\|healthy\|alive'; then
        pass "gateway-proxy /health"
    else
        fail "gateway-proxy /health"
    fi

    # admin-api
    if kc exec -n "$NAMESPACE" -it deploy/${RELEASE_NAME}-admin-api -c admin-api -- \
        python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8080/health').read().decode())" \
        2>/dev/null | grep -qi '"ok"\|"status":"ok"\|healthy\|alive'; then
        pass "admin-api /health"
    else
        fail "admin-api /health"
    fi

    # admin-ui
    if kc exec -n "$NAMESPACE" -it deploy/${RELEASE_NAME}-admin-ui -c admin-ui -- \
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
    status=$(kc exec -n "$NAMESPACE" -it deploy/${RELEASE_NAME}-gateway-proxy -c gateway-proxy -- \
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

# ==============================================================================
# 4. (선택) Bedrock 실호출 — --with-bedrock 에서만
# ------------------------------------------------------------------------------
# 왜 "pod 안에서 pod 자신의 IRSA 자격증명으로 boto3" 인가:
#   - env 무관: dev/prod 동일한 절차. 게이트웨이 도메인/VK/로그인 불필요.
#   - 앱 DB 에 아무것도 쓰지 않는다(읽기+추론 호출만).
#   - 한 번에 IRSA(web identity) → STS AssumeRoleWithWebIdentity → VPC interface
#     endpoint → IAM 정책(InvokeModel) → 모델 접근권까지 전 구간을 증명한다.
#
# ⚠️ 비용: 실제 Bedrock 추론을 1회 호출한다(Haiku 4.5, max_tokens=8 → 약 20 토큰,
#    $0.0001 미만). 그래서 반드시 명시적 --with-bedrock 에서만 돈다.
#
# ⚠️ prod 에서 POST /internal/test/issue-key 를 절대 쓰지 않는다:
#    그 엔드포인트는 실 DB 에 user/budget_config/virtual_key 행을 만든다. admin-api 는
#    `/internal/*` 를 개발 환경(APP_ENV = dev/development/local/test)에서만 열고 그 밖에서는
#    403 을 준다(admin-api/src/app/core/environment.py). 예전 가드는 "production" 문자열만
#    봤고 prod 는 APP_ENV="prod" 라 그대로 통과했다 — 그 버전의 이미지가 남아 있을 수 있으므로
#    이 스크립트도 서버 가드에 기대지 않는다. 앱 경로 검사는 "APP_ENV 가 dev 계열"이라는
#    양성(positive) 신호가 있을 때만 추가로 돈다. prod 신호의 부재로 판단하지 않는다.
# ==============================================================================

# pod 안에서 돌 파이썬 프로브. 항상 exit 0 이고, 결과는 `SMOKE|<check>|<STATUS>|<detail>`
# 라인으로만 낸다(호출측이 파싱해 pass/fail/warn 으로 환산). 비정상 종료 = 인프라 문제.
_bedrock_probe_py() {
    cat <<'PYPROBE'
import ipaddress, json, os, socket, sys

def emit(check, status, detail):
    print("SMOKE|%s|%s|%s" % (check, status, detail), flush=True)

# gateway-proxy 의 설정 모듈은 app.config (admin-api 는 app.core.config).
# 이미지가 PYTHONPATH=/app/src 를 주지만, 방어적으로 한 번 더 넣는다.
sys.path.insert(0, "/app/src")

region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or ""
try:
    from app.config import get_settings
    region = region or get_settings().aws_region
except Exception as e:
    emit("config", "WARN", "app.config import 실패(%s) — env 리전으로 대체" % type(e).__name__)
region = region or "ap-northeast-2"
emit("region", "INFO", region)

model_id = os.environ.get("SMOKE_BEDROCK_MODEL_ID", "").strip()
if not model_id:
    emit("model-id", "FAIL", "SMOKE_BEDROCK_MODEL_ID 가 비어 있다")
    sys.exit(0)

try:
    import boto3
    from botocore.config import Config as BotoConfig
except Exception as e:
    emit("boto3", "FAIL", "boto3 import 실패: %s" % e)
    sys.exit(0)

bcfg = BotoConfig(retries={"max_attempts": 2, "mode": "standard"},
                  connect_timeout=5, read_timeout=40)

# --- 4a. IRSA → STS ---
expect_role = os.environ.get("SMOKE_EXPECT_ROLE", "").strip()
try:
    ident = boto3.client("sts", region_name=region, config=bcfg).get_caller_identity()
    arn = ident["Arn"]
    if "assumed-role/" not in arn:
        emit("sts", "FAIL", "IRSA assumed-role 이 아니다: %s" % arn)
    elif expect_role and ("assumed-role/%s/" % expect_role) not in arn:
        emit("sts", "FAIL", "role 불일치: SA annotation=%s, 실제=%s" % (expect_role, arn))
    else:
        emit("sts", "PASS", "%s (acct %s)" % (arn, ident["Account"]))
except Exception as e:
    emit("sts", "FAIL", "get_caller_identity: %s: %s" % (type(e).__name__, e))

# --- 4b. VPC interface endpoint: 호스트명이 사설 IP 로 풀려야 한다 ---
# private DNS 가 붙은 interface endpoint 가 실제로 경로에 있다는 증거.
# 공인 IP 로 풀리면 NAT/IGW 로 나가고 있다는 뜻 → endpoint 미사용.
for svc in ("bedrock-runtime", "sts", "bedrock"):
    host = "%s.%s.amazonaws.com" % (svc, region)
    try:
        ips = sorted({ai[4][0] for ai in socket.getaddrinfo(host, 443, socket.AF_INET)})
    except Exception as e:
        emit("dns:" + svc, "FAIL", "%s 해석 실패 (%s)" % (host, type(e).__name__))
        continue
    if not ips:
        emit("dns:" + svc, "FAIL", "%s → A 레코드 없음" % host)
        continue
    public = [ip for ip in ips if not ipaddress.ip_address(ip).is_private]
    if public:
        emit("dns:" + svc, "FAIL", "%s → %s (공인 IP — VPC endpoint 미경유)" % (host, ips))
    else:
        emit("dns:" + svc, "PASS", "%s → %s (사설)" % (host, ips))

# --- 4c. 실제 InvokeModel (토큰 소모) ---
body = json.dumps({
    "anthropic_version": "bedrock-2023-05-31",
    "max_tokens": 8,
    "messages": [{"role": "user", "content": "Reply with exactly: PONG"}],
})
try:
    rt = boto3.client("bedrock-runtime", region_name=region, config=bcfg)
    resp = rt.invoke_model(modelId=model_id, contentType="application/json",
                          accept="application/json", body=body)
    payload = json.loads(resp["body"].read())
    text = "".join(b.get("text", "") for b in payload.get("content", [])
                   if b.get("type") == "text").strip()
    usage = payload.get("usage") or {}
    it, ot = usage.get("input_tokens"), usage.get("output_tokens")
    if not isinstance(it, int) or not isinstance(ot, int) or ot <= 0:
        emit("invoke", "FAIL", "%s: 200 이지만 usage 토큰이 없다 (usage=%s)" % (model_id, usage))
    elif not text:
        emit("invoke", "FAIL", "%s: 200 이지만 본문 비어 있음 (stop=%s)"
             % (model_id, payload.get("stop_reason")))
    else:
        emit("invoke", "PASS", "%s → %r (in=%d out=%d stop=%s)"
             % (model_id, text, it, ot, payload.get("stop_reason")))
except Exception as e:
    code = getattr(e, "response", None)
    code = (code or {}).get("Error", {}).get("Code", type(e).__name__)
    emit("invoke", "FAIL", "%s: %s: %s" % (model_id, code, e))
PYPROBE
}

# dev 전용: 진짜 앱 경로(VK 발급 → /v1/messages). VK 값은 pod 안에만 머문다(로그 노출 X).
_apppath_probe_py() {
    cat <<'PYAPP'
import json, os, sys, time, urllib.error, urllib.request

def emit(check, status, detail):
    print("SMOKE|%s|%s|%s" % (check, status, detail), flush=True)

admin = os.environ.get("SMOKE_ADMIN_URL", "").strip()
gw = os.environ.get("SMOKE_GW_URL", "http://localhost:8000").strip()
alias = os.environ.get("SMOKE_MODEL_ALIAS", "").strip()
attempts = int(os.environ.get("SMOKE_APP_ATTEMPTS", "3") or "3")
if not admin or not alias:
    emit("setup", "FAIL", "SMOKE_ADMIN_URL / SMOKE_MODEL_ALIAS 미설정")
    sys.exit(0)

def post(url, obj, headers=None, timeout=90):
    req = urllib.request.Request(
        url, data=json.dumps(obj).encode(),
        headers=dict({"Content-Type": "application/json"}, **(headers or {})),
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw or "{}")
        except Exception:
            return e.code, {"_raw": raw[:300]}

# 1) dev 전용 VK 발급 (짧은 TTL). prod 에서는 호출측이 여기까지 오지 않는다.
try:
    st, out = post(admin + "/internal/test/issue-key",
                   {"email": "smoke-test@example.com",
                    "display_name": "Smoke Test",
                    "expires_seconds": 900})
except Exception as e:
    emit("vk", "FAIL", "issue-key 도달 실패: %s: %s" % (type(e).__name__, e))
    sys.exit(0)
if st == 403:
    emit("vk", "WARN", "issue-key 403 (production 가드) — 앱 경로 생략")
    sys.exit(0)
if st != 200 or not out.get("virtual_key"):
    emit("vk", "FAIL", "issue-key HTTP %s (%s)" % (st, json.dumps(out)[:200]))
    sys.exit(0)
vk = out["virtual_key"]
emit("vk", "PASS", "VK 발급 (len=%d user=%s expires=%s)"
     % (len(vk), out.get("email"), out.get("expires_at")))

# 2) 실제 앱 경로. gateway-proxy 는 bedrock_max_attempts=1 (config.py:147) 이라
#    VPC endpoint 가 idle keep-alive 연결을 끊어놓은 경우 첫 호출이 재시도 없이
#    502 provider_error 로 떨어진다(2026-09-09 dev 실측). 여기서는 재시도로 흡수하고,
#    재시도가 필요했다는 사실 자체를 WARN 으로 남긴다.
last = None
for i in range(1, attempts + 1):
    try:
        st, out = post(gw + "/v1/messages",
                       {"model": alias, "max_tokens": 8,
                        "messages": [{"role": "user", "content": "Reply with exactly: PONG"}]},
                       headers={"Authorization": "Bearer " + vk,
                                "anthropic-version": "2023-06-01"})
    except Exception as e:
        last = "%s: %s" % (type(e).__name__, e)
        time.sleep(1)
        continue
    if st == 200:
        usage = out.get("usage") or {}
        it, ot = usage.get("input_tokens"), usage.get("output_tokens")
        text = "".join(b.get("text", "") for b in out.get("content", [])
                       if b.get("type") == "text").strip()
        if not isinstance(it, int) or not isinstance(ot, int) or ot <= 0:
            emit("messages", "FAIL",
                 "200 이지만 usage 토큰 없음 (usage=%s)" % usage)
        else:
            emit("messages", "PASS",
                 "200 %s → %r (in=%d out=%d, %d/%d 번째 시도)"
                 % (alias, text, it, ot, i, attempts))
            if i > 1:
                emit("retry", "WARN",
                     "첫 %d 회 실패 후 성공 — stale keep-alive + bedrock_max_attempts=1 "
                     "의심 (마지막 실패: %s)" % (i - 1, last))
        break
    last = "HTTP %s (%s)" % (st, json.dumps(out)[:200])
    time.sleep(1)
else:
    emit("messages", "FAIL", "%d 회 모두 실패 — 마지막: %s" % (attempts, last))
PYAPP
}

# SMOKE| 라인을 pass/fail/warn 으로 환산. **파이프라인으로 부르면 안 된다**
# (서브셸이라 PASS_COUNT/FAIL_COUNT 증가가 사라진다) → here-string 으로만 먹인다.
_consume_smoke_lines() {
    local raw="$1" label="$2"
    local seen=0 tag check status detail
    while IFS='|' read -r tag check status detail; do
        [ "$tag" = "SMOKE" ] || continue
        seen=$((seen + 1))
        case "$status" in
            PASS) pass "${label}/${check}: ${detail}" ;;
            FAIL) fail "${label}/${check}: ${detail}" ;;
            WARN) warn "${label}/${check}: ${detail}" ;;
            *)    echo "    · ${check}: ${detail}" ;;
        esac
    done <<< "$raw"
    if [ "$seen" -eq 0 ]; then
        fail "${label}: 프로브가 결과를 내지 못했다 (아래 원문)"
        echo "$raw" | tail -n 20 >&2
    fi
}

test_bedrock_e2e() {
    if [ "$WITH_BEDROCK" -ne 1 ]; then
        return 0
    fi
    echo ""
    echo "━━━ 4. Bedrock 실호출 (IRSA → STS → VPC endpoint → IAM → InvokeModel) ━━━"
    warn "실제 Bedrock 추론을 호출한다 — 토큰 비용 발생 (${BEDROCK_MODEL_ID}, max_tokens=8, 약 20 토큰)"

    # gateway-proxy 의 ServiceAccount annotation 에서 기대 role 이름을 뽑는다.
    # (env 하드코딩 없이 dev/prod 양쪽에서 동작: llm-gateway-<env>-gateway-proxy-bedrock)
    local sa_name="" role_arn="" expect_role=""
    sa_name=$(kc get deploy -n "$NAMESPACE" "${RELEASE_NAME}-gateway-proxy" \
                 --request-timeout=30s \
                 -o jsonpath='{.spec.template.spec.serviceAccountName}' 2>/dev/null) || sa_name=""
    if [ -n "$sa_name" ]; then
        role_arn=$(kc get sa -n "$NAMESPACE" "$sa_name" --request-timeout=30s \
                     -o jsonpath='{.metadata.annotations.eks\.amazonaws\.com/role-arn}' 2>/dev/null) || role_arn=""
    fi
    if [ -n "$role_arn" ]; then
        expect_role="${role_arn##*/}"
        pass "IRSA annotation: sa/${sa_name} → ${role_arn}"
    else
        warn "ServiceAccount 의 eks.amazonaws.com/role-arn annotation 을 못 읽음 — role 이름 대조 생략"
    fi

    # ---- 4a~4c: pod 안 boto3 프로브 (앱 DB 무기록) ----
    local out=""
    out=$(_bedrock_probe_py \
          | kc exec -i -n "$NAMESPACE" "deploy/${RELEASE_NAME}-gateway-proxy" \
                -c gateway-proxy --request-timeout="$BEDROCK_EXEC_TIMEOUT" \
                -- env "SMOKE_BEDROCK_MODEL_ID=${BEDROCK_MODEL_ID}" \
                       "SMOKE_EXPECT_ROLE=${expect_role}" \
                       python - 2>&1) || true
    _consume_smoke_lines "$out" "bedrock"

    # ---- 4d: (dev 만) 진짜 앱 경로 ----
    test_bedrock_app_path
}

test_bedrock_app_path() {
    echo ""
    echo "─── 4d. 앱 경로 (VK → /v1/messages) — dev 에서만 ───"

    if [ "${SMOKE_SKIP_APP_PATH:-0}" = "1" ]; then
        warn "SMOKE_SKIP_APP_PATH=1 → 앱 경로 검사 생략"
        return 0
    fi

    # 배포된 컨테이너의 APP_ENV 를 읽어 **양성 dev 신호**로만 판단한다.
    # ("prod 가 아님" = dev 로 판단하지 않는다 — 예전 이미지의 issue-key 가드는 "production"
    #  문자열만 봐서 APP_ENV="prod" 인 운영 환경에서도 열려 있었다.)
    local app_env=""
    app_env=$(kc get deploy -n "$NAMESPACE" "${RELEASE_NAME}-admin-api" \
                 --request-timeout=30s -o json 2>/dev/null \
              | jq -r '[.spec.template.spec.containers[]? | select(.name=="admin-api")
                        | .env[]? | select(.name=="APP_ENV") | .value] | first // ""' 2>/dev/null) || app_env=""

    case "$app_env" in
        dev|development|local)
            : ;;
        "")
            warn "admin-api 의 APP_ENV 를 읽지 못했다 → 앱 경로 생략 (안전 기본값)"
            return 0 ;;
        *)
            warn "APP_ENV=${app_env} → 앱 경로 생략. /internal/test/issue-key 는 실 DB 에 user/budget/VK 행을 쓴다 (dev 계열에서만 실행)"
            return 0 ;;
    esac

    warn "APP_ENV=${app_env} (dev) → dev DB 에 테스트 user/VK 행이 생성된다 (VK TTL 15분)"

    local admin_url="http://${RELEASE_NAME}-admin-api.${NAMESPACE}.svc.cluster.local:8080"
    local out=""
    out=$(_apppath_probe_py \
          | kc exec -i -n "$NAMESPACE" "deploy/${RELEASE_NAME}-gateway-proxy" \
                -c gateway-proxy --request-timeout="$BEDROCK_EXEC_TIMEOUT" \
                -- env "SMOKE_ADMIN_URL=${admin_url}" \
                       "SMOKE_MODEL_ALIAS=${BEDROCK_MODEL_ALIAS}" \
                       "SMOKE_APP_ATTEMPTS=${SMOKE_APP_ATTEMPTS:-3}" \
                       python - 2>&1) || true
    _consume_smoke_lines "$out" "app-path"
}

# ---- 5. Ingress 체크 ----
test_ingress() {
    echo ""
    echo "━━━ 5. Ingress ━━━"
    local ingresses
    ingresses=$(kc get ingress -n "$NAMESPACE" -l "app.kubernetes.io/instance=${RELEASE_NAME}" -o json)
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
echo "  context   : ${KUBE_CONTEXT:-<active>}"
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