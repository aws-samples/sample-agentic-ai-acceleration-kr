#!/usr/bin/env bash
# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ==============================================================================
# install-eks.sh — AWS EKS Fargate 환경에 LLM Gateway 설치/업그레이드
# ------------------------------------------------------------------------------
# Terraform outputs 에서 IRSA role ARN, Aurora/Redis 엔드포인트를 자동 추출해
# Helm install 의 --set 으로 주입합니다.
#
# 사용법:
#   ./install-eks.sh <env>
#     env: dev | prod
#
# 전제:
#   - terraform/environments/llm-gateway-<env> 에 terraform apply 가 성공적으로 완료됨
#   - aws cli 인증 (aws sts get-caller-identity 통과)
#   - helm, kubectl, terraform, jq 설치
# ==============================================================================

set -euo pipefail

# ---- 리전 확정 ----
# 이 스크립트의 aws 호출(secretsmanager describe-secret 등)은 --region 을 명시하지 않아
# 셸 기본 리전을 따른다. 리전이 안 잡힌 셸에서 실행하면 Secrets Manager preflight 가
# 엉뚱한 리전을 뒤져 "Secret 없음" 으로 오판하므로, 추측하지 말고 즉시 중단한다.
# 우선순위: AWS_REGION > AWS_DEFAULT_REGION. 그 뒤 둘을 통일.
: "${AWS_REGION:=${AWS_DEFAULT_REGION:-}}"
if [ -z "$AWS_REGION" ]; then
    echo "ERROR: region is not set. export AWS_DEFAULT_REGION=<region> (e.g. ap-northeast-2) and retry." >&2
    exit 1
fi
export AWS_REGION AWS_DEFAULT_REGION="$AWS_REGION"

# ---- 인자 ----
ENV="${1:-}"
if [ -z "$ENV" ] || { [ "$ENV" != "dev" ] && [ "$ENV" != "prod" ]; }; then
    echo "Usage: $0 <dev|prod>"
    exit 1
fi

# ---- 경로 설정 ----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TF_DIR="$DEPLOY_DIR/terraform/environments/llm-gateway-$ENV"
CHART_DIR="$DEPLOY_DIR/charts/llm-gateway"
VALUES_FILE="$CHART_DIR/values-eks-fargate-$ENV.yaml"
RELEASE_NAME="llm-gateway"
NAMESPACE="llm-gateway"

# ---- 색 출력 ----
R='\033[0;31m'; G='\033[0;32m'; Y='\033[1;33m'; B='\033[0;34m'; N='\033[0m'
info()  { echo -e "${B}ℹ${N}  $*"; }
ok()    { echo -e "${G}✓${N}  $*"; }
warn()  { echo -e "${Y}⚠${N}  $*"; }
err()   { echo -e "${R}✗${N}  $*" >&2; }

# ---- 전제 도구 확인 ----
check_prereqs() {
    local missing=()
    for cmd in aws kubectl helm terraform jq; do
        if ! command -v "$cmd" >/dev/null 2>&1; then
            missing+=("$cmd")
        fi
    done
    if [ ${#missing[@]} -gt 0 ]; then
        err "필요한 도구 누락: ${missing[*]}"
        echo "  macOS: brew install ${missing[*]/terraform/hashicorp/tap/terraform}"
        exit 1
    fi

    if ! aws sts get-caller-identity >/dev/null 2>&1; then
        err "AWS 인증 실패. aws configure / aws sso login 먼저 실행하세요."
        exit 1
    fi

    if [ ! -d "$TF_DIR" ]; then
        err "Terraform 환경 디렉토리 없음: $TF_DIR"
        exit 1
    fi

    if [ ! -f "$VALUES_FILE" ]; then
        err "Values 파일 없음: $VALUES_FILE"
        exit 1
    fi
}

# ---- Terraform outputs 추출 ----
read_tf_outputs() {
    info "Terraform outputs 추출 중 ($TF_DIR)"
    pushd "$TF_DIR" >/dev/null

    if [ ! -d ".terraform" ]; then
        err "terraform init 가 안 된 것으로 보입니다. cd $TF_DIR && terraform init 먼저 실행."
        exit 1
    fi

    # terraform apply 가 완료됐는지 확인
    if ! terraform output -json >/dev/null 2>&1; then
        err "terraform output 실패. terraform apply 를 먼저 성공시켜야 합니다."
        exit 1
    fi

    local outputs
    outputs=$(terraform output -json)

    CLUSTER_NAME=$(echo "$outputs" | jq -r '.cluster_name.value')
    AWS_REGION=$(echo "$outputs" | jq -r '.cluster_endpoint.value' | awk -F. '{print $3}')
    GATEWAY_ROLE_ARN=$(echo "$outputs" | jq -r '.gateway_proxy_role_arn.value')
    ADMIN_API_ROLE_ARN=$(echo "$outputs" | jq -r '.admin_api_role_arn.value')
    # RDS Proxy on 이면 proxy_endpoint, off 면 aurora_endpoint 를 반환 (modules/aurora-postgresql outputs).
    # dev 기본 off → aurora_endpoint, prod 기본 on → proxy_endpoint. 환경에 관계없이 올바른 값 선택.
    AURORA_HOST=$(echo "$outputs" | jq -r '.application_db_endpoint.value // .aurora_endpoint.value')
    AURORA_DB_NAME=$(echo "$outputs" | jq -r '.aurora_database_name.value // "gateway"')
    AURORA_SECRET_ARN=$(echo "$outputs" | jq -r '.aurora_master_user_secret_arn.value')
    REDIS_HOST=$(echo "$outputs" | jq -r '.elasticache_endpoint.value')
    REDIS_AUTH_SECRET_ARN=$(echo "$outputs" | jq -r '.elasticache_auth_token_secret_arn.value')

    # Cognito (OIDC IDP) outputs — terraform 에서 만들어진 경우만 존재
    COGNITO_ISSUER_URL=$(echo "$outputs" | jq -r '.cognito_issuer_url.value // empty')
    COGNITO_CLIENT_ID=$(echo "$outputs" | jq -r '.cognito_client_id.value // empty')
    COGNITO_HOSTED_UI=$(echo "$outputs" | jq -r '.cognito_hosted_ui_domain.value // empty')

    popd >/dev/null

    # 필수 값 검증
    for var in CLUSTER_NAME GATEWAY_ROLE_ARN ADMIN_API_ROLE_ARN AURORA_HOST REDIS_HOST; do
        if [ -z "${!var}" ] || [ "${!var}" = "null" ]; then
            err "Terraform output 누락: $var"
            exit 1
        fi
    done

    ok "Terraform outputs 추출 완료"
    echo "    cluster_name           : $CLUSTER_NAME"
    echo "    gateway_proxy_role_arn : $GATEWAY_ROLE_ARN"
    echo "    admin_api_role_arn     : $ADMIN_API_ROLE_ARN"
    echo "    aurora_endpoint        : $AURORA_HOST"
    echo "    elasticache_endpoint   : $REDIS_HOST"
    echo "    cognito_issuer_url     : ${COGNITO_ISSUER_URL:-'(없음 — OIDC 비활성)'}"
    echo "    cognito_client_id      : ${COGNITO_CLIENT_ID:-'(없음)'}"
    echo "    cognito_hosted_ui_domain: ${COGNITO_HOSTED_UI:-'(없음)'}"
}

# ---- kubectl context 설정 ----
setup_kubeconfig() {
    info "kubectl context 설정"
    aws eks update-kubeconfig \
        --region "$AWS_REGION" \
        --name "$CLUSTER_NAME" \
        --alias "$CLUSTER_NAME"
    ok "kubectl context = $CLUSTER_NAME"
}

# ---- 네임스페이스 준비 ----
ensure_namespace() {
    if kubectl get namespace "$NAMESPACE" >/dev/null 2>&1; then
        ok "Namespace '$NAMESPACE' 이미 존재"
    else
        info "Namespace '$NAMESPACE' 생성"
        kubectl create namespace "$NAMESPACE"
        ok "Namespace 생성 완료"
    fi
}

# ---- ClusterSecretStore 생성 (ESO 설치 직후 한 번만 필요) ----
ensure_cluster_secret_store() {
    info "ClusterSecretStore 'aws-secrets-manager' 확인"

    # ESO Pod Ready 대기
    if ! kubectl wait --for=condition=Ready pods \
           -l app.kubernetes.io/name=external-secrets \
           -n external-secrets --timeout=120s >/dev/null 2>&1; then
        warn "External Secrets Operator Pod 가 아직 Ready 아닙니다. 대기 후 재시도 하세요."
    fi

    if kubectl get clustersecretstore aws-secrets-manager >/dev/null 2>&1; then
        ok "ClusterSecretStore 이미 존재"
        return
    fi

    info "ClusterSecretStore 생성"
    if ! kubectl apply -f - <<EOF 2>&1 | grep -v "failed calling webhook"
apiVersion: external-secrets.io/v1beta1
kind: ClusterSecretStore
metadata:
  name: aws-secrets-manager
spec:
  provider:
    aws:
      service: SecretsManager
      region: ${AWS_REGION}
      auth:
        jwt:
          serviceAccountRef:
            name: external-secrets
            namespace: external-secrets
EOF
    then
        # ESO v0.10.x 에서 webhook TLS cert SAN 이 Pod IP 로 발급되어 검증 실패하는
        # 알려진 이슈. validating webhook 을 failurePolicy=Ignore 로 완화 후 재시도.
        # 이후 webhook 이 안정화되면 수동으로 failurePolicy=Fail 로 되돌릴 수 있음.
        warn "validating webhook 호출 실패. failurePolicy=Ignore 로 우회 후 재시도"
        for vwc in secretstore-validate externalsecret-validate; do
            if kubectl get validatingwebhookconfiguration "$vwc" >/dev/null 2>&1; then
                count=$(kubectl get validatingwebhookconfiguration "$vwc" \
                    -o jsonpath='{range .webhooks[*]}{.name}{"\n"}{end}' | wc -l | tr -d ' ')
                for i in $(seq 0 $((count-1))); do
                    kubectl patch validatingwebhookconfiguration "$vwc" --type='json' \
                        -p="[{\"op\":\"replace\",\"path\":\"/webhooks/$i/failurePolicy\",\"value\":\"Ignore\"}]" \
                        >/dev/null 2>&1 || true
                done
            fi
        done
        kubectl apply -f - <<EOF
apiVersion: external-secrets.io/v1beta1
kind: ClusterSecretStore
metadata:
  name: aws-secrets-manager
spec:
  provider:
    aws:
      service: SecretsManager
      region: ${AWS_REGION}
      auth:
        jwt:
          serviceAccountRef:
            name: external-secrets
            namespace: external-secrets
EOF
    fi
    ok "ClusterSecretStore 생성 완료"
}

# ---- helm install 직전 ExternalSecret 관련 잔존물 정리 ----
# 두 가지 이슈를 선제적으로 방어 (troubleshooting.md 참조):
#   1) ESO webhook cert SAN 불일치 → helm pre-install hook 의 ExternalSecret 검증 실패
#   2) 이전 failed install 에서 남은 K8s Secret 이 새 ExternalSecret 과 owner UID 충돌
# 한 번 해결되면 재발 적지만, Fargate 첫 배포 / 재배포 사이클에서 자주 재현.
preinstall_cleanup_es_artifacts() {
    info "helm install 직전 ExternalSecret 잔존물 정리"

    # (a) ESO validating webhook 을 임시 삭제 — cert SAN race 회피.
    #     webhook 은 ExternalSecret/SecretStore CR 생성 시점 schema 검증 용도.
    #     런타임 sync 와 무관하므로 제거해도 안전. ESO cert-controller 가 몇 분 내 재생성.
    for vwc in externalsecret-validate secretstore-validate; do
        if kubectl get validatingwebhookconfiguration "$vwc" >/dev/null 2>&1; then
            kubectl delete validatingwebhookconfiguration "$vwc" >/dev/null 2>&1 && \
                ok "validating webhook '$vwc' 임시 삭제 (cert-controller 가 재생성 예정)"
        fi
    done

    # (b) 이전 release 가 남긴 orphan K8s Secret 정리.
    #     ExternalSecret 이 재생성되면 새 UID 로 owner 가 바뀌어야 하는데
    #     stale Secret 의 ownerReferences 가 옛 UID 를 가리켜 "already exists" 에러.
    #     삭제하면 새 ExternalSecret 이 다시 만듦.
    for secret_name in llm-gateway-app llm-gateway-db llm-gateway-redis; do
        # ownerReferences 에 ExternalSecret 이 있는 경우만 삭제 (우리가 만든 것)
        local owner_kind
        owner_kind=$(kubectl -n "$NAMESPACE" get secret "$secret_name" \
            -o jsonpath='{.metadata.ownerReferences[0].kind}' 2>/dev/null || echo "")
        if [ "$owner_kind" = "ExternalSecret" ]; then
            # 현재 ExternalSecret 이 있는지 확인 — 있다면 UID 비교해서 stale 인지 판단
            local current_es_uid stale_owner_uid
            current_es_uid=$(kubectl -n "$NAMESPACE" get externalsecret "$secret_name" \
                -o jsonpath='{.metadata.uid}' 2>/dev/null || echo "")
            stale_owner_uid=$(kubectl -n "$NAMESPACE" get secret "$secret_name" \
                -o jsonpath='{.metadata.ownerReferences[0].uid}' 2>/dev/null || echo "")
            if [ -z "$current_es_uid" ] || [ "$current_es_uid" != "$stale_owner_uid" ]; then
                kubectl -n "$NAMESPACE" delete secret "$secret_name" >/dev/null 2>&1 && \
                    ok "orphan K8s Secret '$secret_name' 삭제 (ExternalSecret 이 재생성)"
            fi
        fi
    done
}

# ---- Secrets Manager 값을 ExternalSecret 이 자동으로 가져오도록 secret path 확인 ----
check_secrets_manager() {
    info "Secrets Manager 경로 확인 (/llm-gateway/${ENV}/...)"
    local missing=()
    for path in "app" "db" "redis"; do
        local secret_path="/llm-gateway/${ENV}/${path}"
        if ! aws secretsmanager describe-secret --secret-id "$secret_path" >/dev/null 2>&1; then
            missing+=("$secret_path")
        fi
    done

    if [ ${#missing[@]} -gt 0 ]; then
        warn "Secrets Manager에 아래 경로의 Secret이 없습니다:"
        for s in "${missing[@]}"; do echo "    - $s"; done
        echo ""
        echo "    다음 중 한 가지로 생성하세요:"
        echo ""
        echo "    1) DB 비번 (Aurora가 이미 저장한 값을 복제):"
        echo "       aws secretsmanager create-secret --name /llm-gateway/${ENV}/db \\"
        echo "         --secret-string '{\"password\":\"\$(aws secretsmanager get-secret-value --secret-id $AURORA_SECRET_ARN --query SecretString --output text | jq -r .password)\",\"notification_worker_password\":\"CHANGE_ME\"}'"
        echo ""
        echo "    2) Redis AUTH (ElastiCache가 이미 저장):"
        echo "       aws secretsmanager create-secret --name /llm-gateway/${ENV}/redis \\"
        echo "         --secret-string '{\"password\":\"\$(aws secretsmanager get-secret-value --secret-id $REDIS_AUTH_SECRET_ARN --query SecretString --output text)\"}'"
        echo ""
        echo "    3) App secrets (직접 생성):"
        echo "       aws secretsmanager create-secret --name /llm-gateway/${ENV}/app \\"
        echo "         --secret-string '{\"virtual_key_encryption_key\":\"\$(openssl rand -hex 32)\",\"nextauth_secret\":\"\$(openssl rand -hex 32)\",\"jwt_jwks_cache_key\":\"\$(openssl rand -hex 32)\"}'"
        echo ""
        read -p "    계속 진행 (y) / 중단 (N)? " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            exit 1
        fi
    else
        ok "Secrets Manager 경로 존재 확인 완료"
    fi
}

# ---- Observability 스택 (HPA + 관측) ----
# EKS Fargate 에선 metrics-server 가 kubelet authz 제약으로 동작하지 않음.
# 따라서 prometheus-adapter 가 metrics.k8s.io API 를 서빙하며, 이는 사전에
# kube-prometheus-stack 이 pod CPU/memory 를 수집하고 있어야 함.
# 순서: 1) kube-prometheus-stack → 2) prometheus-adapter.
# 자세한 배경은 deployment/observability/README.md 참조.
ensure_observability() {
    info "Observability 스택 확인/설치 (Fargate HPA + 관측 공통)"
    bash "$DEPLOY_DIR/observability/kube-prometheus-stack/install.sh"
    bash "$DEPLOY_DIR/observability/prometheus-adapter/install.sh"
    bash "$DEPLOY_DIR/observability/otel-collector/install.sh"
}

# ---- Helm install / upgrade ----
# SET_ARGS 는 helm_install 이 조립하고 patch_nextauth_url 이 재사용하므로 전역.
declare -a SET_ARGS=()

# ---- 과거 patch_nextauth_url 이 남긴 field ownership 정리 ----
# 증상: helm upgrade 가 이 에러로 실패.
#   conflict with "helm" using apps/v1:
#     .spec.template.spec.containers[name="admin-ui"].env[name="NEXTAUTH_URL"].value
#
# 원인: k8s server-side apply 의 소유권 키는 manager *이름* 이 아니라
# (이름, operation) 쌍이다. `kubectl set env` 는 operation=Update 로 기록되고,
# helm 은 operation=Apply 로 기록한다. 따라서 과거 코드의 `--field-manager=helm` 은
# 소유권을 helm 에 넘겨준 게 아니라, 이름만 같고 operation 이 다른 *두 번째* 소유자를
# 만들었다. 에러 메시지의 `using apps/v1` 접미사가 그 증거 —
# k8s 는 충돌 상대가 Update 소유자일 때만 이 접미사를 붙인다.
#
# 조치: 그 Update 항목만 managedFields 에서 제거해 helm(Apply) 단독 소유로 되돌린다.
# `--force-conflicts` 를 쓰지 않는 이유: 그건 릴리즈 전체에 적용돼서, HPA 가 소유한
# .spec.replicas 까지 helm 이 덮어써 스케일이 되돌아간다 (이 클러스터는 HPA 사용).
prune_stale_field_managers() {
    local deploy="${RELEASE_NAME}-admin-ui"
    kubectl get deployment "$deploy" -n "$NAMESPACE" >/dev/null 2>&1 || return 0

    # operation=Update 이면서 manager 가 helm / kubectl-set 인 항목의 인덱스.
    # 뒤에서부터 지워야 앞쪽 인덱스가 밀리지 않는다.
    local idxs
    idxs=$(kubectl get deployment "$deploy" -n "$NAMESPACE" -o json 2>/dev/null \
        | jq -r '[.metadata.managedFields | to_entries[]
                  | select(.value.operation == "Update")
                  | select(.value.manager == "helm" or .value.manager == "kubectl-set")
                  | .key] | reverse | .[]' 2>/dev/null || true)
    [ -z "$idxs" ] && return 0

    warn "admin-ui 에 과거 'kubectl set env' 소유권 잔존 → 정리 (helm 충돌 예방)"
    local i
    for i in $idxs; do
        kubectl patch deployment "$deploy" -n "$NAMESPACE" --type=json \
            -p="[{\"op\":\"remove\",\"path\":\"/metadata/managedFields/$i\"}]" \
            >/dev/null 2>&1 || true
    done
    ok "admin-ui field ownership 정리 완료"
}

helm_install() {
    info "Helm ${HELM_ACTION} 실행 중"

    # ECR 레지스트리 주소 — *순수 registry URL* (계정.dkr.ecr.region.amazonaws.com)
    # chart values.yaml 의 image.repository 가 이미 "llm-gateway/<service>" prefix 를
    # 포함하므로, registry 에는 llm-gateway 를 넣으면 안 됨. 넣으면 pull 경로가
    # .../llm-gateway/llm-gateway/<service>:<tag> 로 중복되어 ImagePullBackOff.
    local aws_account_id
    aws_account_id=$(aws sts get-caller-identity --query Account --output text)
    local ecr_registry="${aws_account_id}.dkr.ecr.${AWS_REGION}.amazonaws.com"

    # --set 명령 조립
    SET_ARGS=(
        --set "global.imageRegistry=${ecr_registry}"
        --set "aws.region=${AWS_REGION}"
        --set "database.external.host=${AURORA_HOST}"
        --set "database.external.name=${AURORA_DB_NAME}"
        --set "redis.external.host=${REDIS_HOST}"
        --set "redis.external.tls=true"
        --set-string "gatewayProxy.serviceAccount.annotations.eks\.amazonaws\.com/role-arn=${GATEWAY_ROLE_ARN}"
        --set-string "adminApi.serviceAccount.annotations.eks\.amazonaws\.com/role-arn=${ADMIN_API_ROLE_ARN}"
    )

    # OIDC (Cognito) — terraform 에서 만든 경우 자동 주입
    if [ -n "${COGNITO_ISSUER_URL}" ]; then
        SET_ARGS+=(
            --set "adminApi.oidc.enabled=true"
            --set "adminApi.oidc.issuerUrl=${COGNITO_ISSUER_URL}"
            --set "adminApi.oidc.providerName=oidc:cognito"
            --set "adminApi.oidc.groupsClaim=cognito:groups"
        )
        # 다시 if 로 닫지 않고 dummy true (아래 fi 와 짝 맞춤 위해)
        true
    fi

    # Migration Job 은 pre-install hook 으로 실행됨:
    #   1. ExternalSecret pre-install hook weight -30 → ESO 가 K8s Secret sync
    #   2. migration ServiceAccount + Role + RoleBinding pre-install weight -10
    #   3. migration Job pre-install weight -5
    #      - initContainer 가 Secret 존재 여부를 최대 5분 대기
    #      - Secret 생기면 main container (alembic upgrade) 실행
    # 전체가 helm install 한 번에 끝남. 비활성화 하려면 MIGRATION_ENABLED=false env 사용.
    if [ "${MIGRATION_ENABLED:-true}" != "true" ]; then
        warn "Migration Job 비활성화 (MIGRATION_ENABLED=false). 별도 수동 실행 필요"
        SET_ARGS+=(--set "migration.enabled=false")
    fi

    # helm upgrade --install 로 통합: release 없으면 install, 있으면 upgrade.
    # `--cleanup-on-fail` 은 upgrade 경로에서만 유효한 플래그라서, install 모드에서
    # 별도 helm install 명령을 쓰면 에러. upgrade --install 로 통일하면 항상 OK.
    # rollback 플래그는 Helm 메이저 버전에 따라 다름:
    #   v3 = --atomic (--rollback-on-failure 는 unknown flag), v4 = --rollback-on-failure.
    # 사전 요구사항이 Helm >=3.14 이므로 런타임에 감지해 맞는 플래그를 쓴다.
    HELM_MAJOR=$(helm version --template '{{.Version}}' 2>/dev/null | sed -E 's/^v([0-9]+).*/\1/')
    if [ "${HELM_MAJOR:-3}" -ge 4 ]; then
        ROLLBACK_FLAG="--rollback-on-failure"
    else
        ROLLBACK_FLAG="--atomic"
    fi
    #
    # DEBUG_MODE=true 로 실행하면 실패 시 rollback/cleanup 을 하지 않아 실패한 Pod 와
    # Deployment 가 그대로 남음 → `kubectl logs <pod> --previous` 로 crash 원인 조사 가능.
    if [ "${DEBUG_MODE:-false}" = "true" ]; then
        warn "DEBUG_MODE=true → 실패 시 rollback 하지 않음 (리소스 유지, logs 조사 가능)"
        helm upgrade --install "$RELEASE_NAME" "$CHART_DIR" \
            --namespace "$NAMESPACE" \
            --values "$VALUES_FILE" \
            "${SET_ARGS[@]}" \
            --timeout 15m \
            --wait
    else
        if [ "${FORCE_CONFLICTS:-false}" = "true" ]; then
            warn "FORCE_CONFLICTS=true → server-side apply 충돌을 helm 이 takeover"
            helm upgrade --install "$RELEASE_NAME" "$CHART_DIR" \
                --namespace "$NAMESPACE" \
                --values "$VALUES_FILE" \
                "${SET_ARGS[@]}" \
                --force-conflicts \
                "$ROLLBACK_FLAG" \
                --cleanup-on-fail \
                --timeout 15m \
                --wait
        else
            helm upgrade --install "$RELEASE_NAME" "$CHART_DIR" \
                --namespace "$NAMESPACE" \
                --values "$VALUES_FILE" \
                "${SET_ARGS[@]}" \
                "$ROLLBACK_FLAG" \
                --cleanup-on-fail \
                --timeout 15m \
                --wait
        fi
    fi

    ok "Helm ${HELM_ACTION} 완료"
}

# ---- Post-install: admin-ui NEXTAUTH_URL 패치 (방식 A) ----
# 방식 A (도메인 없음) 에선 ALB DNS 가 helm install 후에야 확정됨 → ingress.adminUi.host 가
# 빈 값이라 NEXTAUTH_URL 이 "http://" 로 잘려버림. 여기서 ALB DNS 를 읽어 env 업데이트.
patch_nextauth_url() {
    info "admin-ui NEXTAUTH_URL 패치 (ALB DNS 기반)"

    # 가드: chart 가 ingress.adminUi.host 로부터 NEXTAUTH_URL 을 이미 결정한 경우
    # (도메인 모드, 방식 B) 에는 ALB DNS 패치가 불필요. host 가 비어 있을 때만
    # chart 가 "http://" / "https://" 만 렌더 (host 자리 비어서 잘림) → 그 경우에만 진행.
    # 효과: 두 manager(helm SSA vs kubectl set env)가 같은 필드를 다투는 충돌을 영구 차단.
    local rendered
    rendered=$(helm template "$RELEASE_NAME" "$CHART_DIR" --values "$VALUES_FILE" \
        --show-only templates/admin-ui/deployment.yaml 2>/dev/null \
        | awk '/name: NEXTAUTH_URL/{f=1;next} f && /value:/{sub(/^[[:space:]]*value:[[:space:]]*/,"");gsub(/^"|"$/,"");print;exit}' \
        || true)
    if [[ ! "$rendered" =~ ^https?://$ ]]; then
        ok "NEXTAUTH_URL 이 chart 에서 결정됨 (${rendered}) — post-install 패치 스킵"
        return
    fi

    local alb_dns=""
    local attempt=0
    while [ $attempt -lt 60 ]; do
        alb_dns=$(kubectl get ingress "${RELEASE_NAME}-admin-ui" -n "$NAMESPACE" \
            -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || echo '')
        [ -n "$alb_dns" ] && break
        sleep 3
        attempt=$((attempt + 1))
    done
    if [ -z "$alb_dns" ]; then
        warn "admin-ui ALB DNS 를 3분 내 확보 실패 — NEXTAUTH_URL 수동 패치 필요"
        return
    fi
    local nextauth_url="http://${alb_dns}"
    info "NEXTAUTH_URL=${nextauth_url}"
    # 현재 deployment 의 NEXTAUTH_URL 과 비교해 필요할 때만 업데이트 (idempotent)
    local current
    current=$(kubectl get deployment "${RELEASE_NAME}-admin-ui" -n "$NAMESPACE" \
        -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="NEXTAUTH_URL")].value}' 2>/dev/null || echo '')
    if [ "$current" = "$nextauth_url" ]; then
        ok "NEXTAUTH_URL 이미 올바름 — 스킵"
        return
    fi

    # `kubectl set env` 대신 helm upgrade 로 되돌린다 (--set adminUi.nextauthUrl=...).
    # chart 의 default 표현식이 이 값을 그대로 쓰므로 결과 env 는 동일하고,
    # 소유자는 helm(Apply) 하나로 유지된다 → 다음 배포에서 충돌이 발생하지 않는다.
    # (과거 kubectl set env 방식이 만든 Update 소유자가 바로 이번 배포 실패의 원인.)
    #
    # migration.enabled=false: 방금 같은 실행의 helm_install 이 이미 alembic upgrade 를
    # 돌렸다. 이 두 번째 upgrade 는 env 한 개만 바꾸는 것이므로 pre-upgrade
    # migration hook 을 다시 띄울 이유가 없다 (재실행은 no-op 이지만 배포가 수 분 길어짐).
    # --set 은 이 호출에만 적용되고 릴리즈 values 에 누적되지 않는다.
    helm upgrade "$RELEASE_NAME" "$CHART_DIR" \
        --namespace "$NAMESPACE" \
        --values "$VALUES_FILE" \
        "${SET_ARGS[@]}" \
        --set "adminUi.nextauthUrl=${nextauth_url}" \
        --set "migration.enabled=false" \
        --timeout 5m \
        --wait
    ok "admin-ui NEXTAUTH_URL 패치 완료"
}

# ---- 배포 상태 확인 ----
verify_deployment() {
    info "Deployment 상태 확인"
    kubectl get pods -n "$NAMESPACE" -l "app.kubernetes.io/instance=${RELEASE_NAME}" -o wide
    echo ""
    info "Ingress (ALB 생성에 1~2분 소요)"
    kubectl get ingress -n "$NAMESPACE" -l "app.kubernetes.io/instance=${RELEASE_NAME}"
    echo ""
    ok "모든 Pod가 Ready 상태여야 합니다. 아닌 경우:"
    echo "    kubectl describe pod <pod-name> -n $NAMESPACE"
    echo "    kubectl logs <pod-name> -n $NAMESPACE --tail=50"
}

# ---- main ----
main() {
    echo "=============================================================="
    echo "  LLM Gateway — EKS Fargate ${ENV} 배포"
    echo "=============================================================="

    check_prereqs

    # 이미 설치되어 있으면 upgrade, 없으면 install
    if helm status "$RELEASE_NAME" -n "$NAMESPACE" >/dev/null 2>&1; then
        HELM_ACTION="upgrade"
        info "기존 릴리즈 감지 → upgrade 모드"
    else
        HELM_ACTION="install"
        info "신규 설치 모드"
    fi

    read_tf_outputs
    setup_kubeconfig
    ensure_namespace
    ensure_observability
    ensure_cluster_secret_store
    check_secrets_manager
    preinstall_cleanup_es_artifacts
    prune_stale_field_managers
    helm_install
    patch_nextauth_url
    verify_deployment

    echo ""
    ok "배포 완료"
    echo ""
    echo "다음 단계:"
    echo "    ./scripts/smoke-test.sh --env $ENV"
    echo ""
}

main "$@"