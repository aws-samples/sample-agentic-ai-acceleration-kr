# 04. Helm Install — 애플리케이션 배포

**목적**: EKS 클러스터에 LLM Gateway 7개 서비스를 설치.
**소요**: 15분 (이미지 빌드/푸시 포함 시 30분)

---

## 1. Docker 이미지 빌드 + ECR 푸시

애플리케이션 이미지를 ECR(또는 다른 registry)에 올려둬야 Pod가 pull해서 쓸 수 있습니다.

### 1.1 ECR 레지스트리 생성

리포에 포함된 Terraform이 ECR 리포지토리를 **자동 생성하지 않습니다** (의도적 — 고객 환경마다 registry가 다르므로). 수동 생성:

```bash
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_BASE="$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/llm-gateway"

for svc in gateway-proxy admin-api admin-ui scheduler notification-worker cost-recorder-worker migration; do
  aws ecr create-repository \
    --repository-name "llm-gateway/$svc" \
    --image-scanning-configuration scanOnPush=true \
    --region "$AWS_REGION" \
    --encryption-configuration encryptionType=AES256 \
    2>/dev/null || echo "✓ $svc repository exists"
done
```

✅ 7개 repository가 생겼다고 나오거나 "exists" 메시지가 보여야 합니다.

### 1.2 ECR 로그인

```bash
aws ecr get-login-password --region "$AWS_REGION" | \
  docker login --username AWS --password-stdin "$ECR_BASE"
```

✅ `Login Succeeded`

🐛 `unable to get image` — docker daemon 실행 확인 (`docker info`).

### 1.3 이미지 빌드 + 푸시

**리포 루트**에서 실행:

```bash
cd /path/to/LLM-Gateway-Vanilla

# Chart.yaml 의 appVersion 과 일치시킬 것 (Helm 이 기본 태그로 참조).
VERSION=$(grep '^appVersion:' ./deployment/charts/llm-gateway/Chart.yaml | awk -F'"' '{print $2}')
echo "Building version: $VERSION"

for svc_dir in gateway-proxy admin-api admin-ui notification-worker cost-recorder-worker; do
  echo "==> Building ${svc_dir}:${VERSION}"
  docker build \
    --platform linux/amd64 \
    -t "${ECR_BASE}/${svc_dir}:${VERSION}" \
    "./${svc_dir}"

  docker push "${ECR_BASE}/${svc_dir}:${VERSION}"
done

# scheduler 는 admin-api 이미지 재사용 (CMD만 다름)
docker tag  "${ECR_BASE}/admin-api:${VERSION}" "${ECR_BASE}/scheduler:${VERSION}"
docker push "${ECR_BASE}/scheduler:${VERSION}"

# migration 은 ./db 를 build context 로 사용 (리포 루트에서 그대로 실행, cd 불필요)
docker build --platform linux/amd64 \
  -t "${ECR_BASE}/migration:${VERSION}" \
  ./db
docker push "${ECR_BASE}/migration:${VERSION}"
```

⚠️ **`latest` 태그 금지**: 환경 간 의도치 않은 이미지 덮어쓰기 위험 — dev 에서 새로 push 하면 prod Pod 가 재시작 시 예상 못한 이미지를 pull. Chart.yaml `appVersion` 을 bump 하고 그 버전 태그만 push. `helm upgrade` 시 Chart.yaml 의 appVersion 이 자동으로 태그로 참조됨.

⚠️ **`--platform linux/amd64`** 필수 — Mac M1/M2에서 빌드해도 EKS(x86_64)에서 동작해야 함.

⚠️ **변수는 반드시 `${var}` 중괄호 형태로 사용**. zsh (macOS 기본 셸) 는 콜론 뒤 문자를 parameter modifier 로 해석해 변수 이름이 망가집니다. bash 는 문제 없지만 zsh 호환을 위해 중괄호를 써야 합니다.

✅ 확인:
```bash
aws ecr list-images --repository-name llm-gateway/gateway-proxy \
  --region "$AWS_REGION" --query 'imageIds[*].imageTag' --output table
```

각 repository에 appVersion에 해당하는 태그 (예: `1.0.37`)가 보여야 합니다.

**예상 시간**: 7개 이미지 × 빌드 2~3분 + 푸시 1~2분 = **약 20~30분**.

---

## 2. `install-eks.sh` 실행

드디어 애플리케이션 배포.

```bash
cd /path/to/LLM-Gateway-Vanilla   # 리포 루트
./deployment/scripts/install-eks.sh $ENV
```

이 스크립트가 하는 일 (자동):
1. Terraform outputs에서 IRSA Role ARN, Aurora/Redis 엔드포인트 추출
2. `aws eks update-kubeconfig` 실행
3. Namespace `llm-gateway` 생성 (없으면)
4. **Observability 스택 설치/업그레이드** (`kube-prometheus-stack` + `prometheus-adapter` + `otel-collector`) — HPA 의 `metrics.k8s.io` API 와 관측성 제공. EKS Fargate 에선 metrics-server 가 kubelet authz 제약으로 동작하지 않으므로 `prometheus-adapter` 가 이를 대체 (AWS 공식 권장). 자세한 배경: [`deployment/observability/README.md`](../../observability/README.md).
5. Secrets Manager 3개 경로 존재 확인 (03에서 만든 것)
6. `helm install` 에 `--set` 플래그로 Terraform output 주입
7. `--rollback-on-failure --cleanup-on-fail --wait --timeout 15m` 으로 실패 시 자동 롤백

---

🐛 **`helm install` 이 pre-install hook 에서 webhook cert 에러로 실패**하는 경우 — Fargate 첫 배포 시 자주 발생하는 ESO cert-controller race condition. 2026-04-24 이후 `install-eks.sh` 가 helm install 직전 `preinstall_cleanup_es_artifacts()` 로 자동 우회하지만, 여전히 막히면 [troubleshooting.md — helm install pre-install hook 에서 ExternalSecret 검증 webhook 실패](./troubleshooting.md#증상-helm-install-pre-install-hook-에서-externalsecret-검증-webhook-실패) 참조.

🐛 **`SecretSyncedError: secret already exists`** — 재배포 시 이전 실패로 남은 orphan K8s Secret 이 충돌하는 경우. `install-eks.sh` 가 owner UID 비교해 자동 정리하지만, 수동 helm install 했다면 [troubleshooting.md — stale K8s Secret owner 충돌](./troubleshooting.md#증상-externalsecret-secretsyncederror-secret-already-exists--stale-k8s-secret-owner-충돌) 참조.

## 3. 실행 중 보이는 출력

```
==============================================================
  LLM Gateway — EKS Fargate dev 배포
==============================================================
ℹ  신규 설치 모드
ℹ  Terraform outputs 추출 중 (.../environments/dev)
✓  Terraform outputs 추출 완료
    cluster_name                : llm-gateway-dev
    gateway_proxy_role_arn      : arn:aws:iam::123456789012:role/...
    ...
ℹ  kubectl context 설정
✓  kubectl context = llm-gateway-dev
✓  Namespace 'llm-gateway' 이미 존재
ℹ  Secrets Manager 경로 확인 (/llm-gateway/dev/...)
✓  Secrets Manager 경로 존재 확인 완료
ℹ  Helm install 실행 중
```

그 다음 Helm의 진행 출력:

```
Release "llm-gateway" does not exist. Installing it now.
NAME: llm-gateway
LAST DEPLOYED: Mon Apr 22 15:30:00 2026
NAMESPACE: llm-gateway
STATUS: deployed
REVISION: 1
```

### 3.1 helm install 실행 시 동작 순서

**Pre-install hooks (순서 보장):**

1. **ExternalSecret CR** (hook-weight -30):
   - ESO가 Secrets Manager에서 값을 읽어 K8s Secret 생성 (~30초)
2. **ServiceAccount + Role + RoleBinding** (hook-weight -10)
3. **Migration Job** (hook-weight -5, 최대 10분):
   - initContainer가 K8s Secret 존재를 대기 (최대 5분)
   - `migration` 이미지로 Alembic 마이그레이션 실행
   - 성공 시 자동 삭제

**Hook 완료 후 (동시 생성):**

4. **Deployment × 6** 생성 (gateway-proxy, admin-api, admin-ui, scheduler, notification-worker, cost-recorder-worker)
5. **K8s Scheduler** 가 Fargate Pod 배치 시작:
   - 각 Pod마다 micro-VM 프로비저닝 **1~2분**
   - 이미지 pull (~30초)
   - startupProbe 통과 대기 (최대 2분)
6. **Ingress × 3** 생성 → ALB Controller가 ALB 3개 자동 생성 (2~5분)

### 3.1a OIDC (Cognito) 자동 주입

terraform 단계에서 Cognito User Pool 이 만들어졌으면, `install-eks.sh` 가 다음을 자동 주입합니다 (실행 로그에서 `cognito_*` 라인으로 확인):

```
cognito_issuer_url          : https://cognito-idp.ap-northeast-2.amazonaws.com/ap-northeast-2_xxx
cognito_client_id           : 1a2b3c4d5e6f7g8h
cognito_hosted_ui_domain    : llm-gateway-dev-auth.auth.ap-northeast-2.amazoncognito.com
```

→ admin-api 의 `OIDC_ISSUER_URL`, `OIDC_PROVIDER_NAME=oidc:cognito`, `OIDC_GROUPS_CLAIM=cognito:groups` 가 자동 설정됨. 별도 수동 작업 불필요.

⚠️ **`cognito_issuer_url` 출력이 비어있으면**: terraform 의 cognito 모듈이 적용 안 된 상태. `terraform apply` 다시 실행 필요.

OIDC 가 활성화되면 사용자 등록 / 첫 admin 부트스트랩이 필요합니다. **다음 단계 [05-smoke-test.md](./05-smoke-test.md) 진행 후 [07-cognito-onboarding.md](./07-cognito-onboarding.md)** 를 보세요.

### 3.2 전체 소요시간

약 **10~15분**.

✅ 성공 시 마지막 출력:
```
✓ Helm install 완료
...
✓ 배포 완료

다음 단계:
    cd deployment
    ./scripts/smoke-test.sh --env dev
```

---

## 4. 설치 후 상태 확인

### 4.1 모든 Pod Running

```bash
kubectl get pods -n llm-gateway
```

예상 출력 (dev):
```
NAME                                                    READY   STATUS    AGE
llm-gateway-gateway-proxy-xxx                        1/1     Running   3m
llm-gateway-admin-api-xxx                            1/1     Running   3m
llm-gateway-admin-ui-xxx                             1/1     Running   3m
llm-gateway-scheduler-xxx                            1/1     Running   3m
llm-gateway-notification-worker-xxx                  1/1     Running   3m
llm-gateway-cost-recorder-worker-xxx                 1/1     Running   3m
```

⚠️ `Pending` 상태 — Fargate 프로비저닝 대기 중 (정상, 2분 내 Running).
⚠️ `ImagePullBackOff` — [troubleshooting.md의 "이미지 pull 실패"](./troubleshooting.md#이미지-pull-실패) 참조.
⚠️ `CrashLoopBackOff` — 로그 확인:
```bash
kubectl logs <pod-name> -n llm-gateway --tail=50
```

### 4.2 Ingress (ALB) 생성

```bash
kubectl get ingress -n llm-gateway
```

```
NAME                               CLASS   HOSTS                                       ADDRESS                                    AGE
llm-gateway-gateway             alb     gateway-dev.llm-gateway.example.com              k8s-...elb.amazonaws.com                  5m
llm-gateway-admin-ui            alb     admin-dev.llm-gateway.example.com                k8s-...elb.amazonaws.com                  5m
llm-gateway-admin-api           alb     admin-api-dev.llm-gateway.example.com            k8s-...elb.amazonaws.com                  5m
```

✅ `ADDRESS` 에 ALB DNS 이름이 나와야 함. 비어있으면 2~3분 더 대기.

### 4.3 ExternalSecret 동기화

```bash
kubectl get externalsecret -n llm-gateway
```

```
NAME                       STORE                 REFRESH INTERVAL   STATUS         READY
llm-gateway-app         aws-secrets-manager   1h                 SecretSynced   True
llm-gateway-db          aws-secrets-manager   1h                 SecretSynced   True
```

✅ `READY: True` 여야 함.

🐛 `READY: False` 또는 `SecretSyncedError` — [troubleshooting.md의 "ExternalSecret 동기화 실패"](./troubleshooting.md#externalsecret-동기화-실패) 참조.

### 4.4 ALB 동작 확인 (Health check)

```bash
ALB_DNS=$(kubectl get ingress llm-gateway-gateway -n llm-gateway \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')
curl -i "http://$ALB_DNS/health"
```

✅ `HTTP/1.1 200 OK` + body `{"status":"ok"}` 또는 유사.

🐛 `503 Service Unavailable` — Target Group이 아직 healthy 상태가 아님 (2~3분 더 대기).

---

## 5. 접근 방식 — 방식 A (도메인 없음) vs 방식 B (도메인 있음)

`values-eks-fargate-dev.yaml` 상단의 Ingress 섹션이 두 방식으로 나뉘어 있습니다. 배포 환경에 맞는 쪽을 골라야 합니다.

### 방식 A: 도메인 없음 (dev 테스트 기본)

📝 **values 파일 기본값이 이미 방식 A** 이므로 수정 불필요.

ALB DNS 주소로 직접 접근:

```bash
GATEWAY_ALB=$(kubectl get ingress llm-gateway-gateway -n llm-gateway \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')
ADMIN_UI_ALB=$(kubectl get ingress llm-gateway-admin-ui -n llm-gateway \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')
ADMIN_API_ALB=$(kubectl get ingress llm-gateway-admin-api -n llm-gateway \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')

echo "Gateway  : http://$GATEWAY_ALB"
echo "Admin UI : http://$ADMIN_UI_ALB"
echo "Admin API: http://$ADMIN_API_ALB"
```

✅ HTTP 확인:
```bash
curl -i "http://$GATEWAY_ALB/health"
```

⚠️ **방식 A 제약**:
- HTTP 평문 전송 — 사용자에게 공개 금지, 내부 검증 전용
- 매 apply 마다 ALB DNS가 바뀔 수 있음 (기본적으론 유지되지만 재생성 시 변경)
- NextAuth cookie 의 `secure` 제약으로 admin UI 로그인이 일부 동작 안 할 수 있음 → 브라우저 시크릿 모드로 우회

### 방식 B: 도메인 있음

사전 준비:
1. **Route53 Hosted Zone** 또는 외부 DNS 에 등록된 도메인 확인
2. **ACM 인증서 발급 + 검증** (ap-northeast-2 리전):
   ```bash
   # gateway-dev.<domain>, admin-dev.<domain>, admin-api-dev.<domain> 전부 커버
   aws acm request-certificate \
     --region "$AWS_REGION" \
     --domain-name "*.llm-gateway.mycompany.com" \
     --validation-method DNS \
     --subject-alternative-names "llm-gateway.mycompany.com"
   ```
   콘솔(ACM → 생성된 인증서)에서 **Create records in Route 53** 버튼으로 검증 DNS 자동 추가.
3. `values-eks-fargate-dev.yaml` 에서:
   - **방식 A 블록 전체 주석 처리**
   - **방식 B 블록 주석 해제**
   - `certificate-arn` 값을 위에서 받은 ACM ARN 으로 교체
   - `host` 3개를 실제 도메인으로 교체

#### helm upgrade 로 방식 전환

```bash
helm upgrade llm-gateway ./deployment/charts/llm-gateway \
  --namespace llm-gateway \
  -f ./deployment/charts/llm-gateway/values-eks-fargate-dev.yaml \
  --wait
```

#### Route53 CNAME 등록 (방식 B 에서만)

```bash
HOSTED_ZONE_ID="Z123..."                       # route53_zone_id 값
ALB_DNS=$(kubectl get ingress llm-gateway-gateway -n llm-gateway \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')

for host in gateway-dev admin-dev admin-api-dev; do
  aws route53 change-resource-record-sets \
    --hosted-zone-id "$HOSTED_ZONE_ID" \
    --change-batch "{\"Changes\":[{\"Action\":\"UPSERT\",\"ResourceRecordSet\":{
      \"Name\":\"$host.llm-gateway.mycompany.com\",
      \"Type\":\"CNAME\",\"TTL\":300,
      \"ResourceRecords\":[{\"Value\":\"$ALB_DNS\"}]}}]}"
done
```

✅ HTTPS 확인 (DNS 전파 대기 2~5분):
```bash
curl -i https://gateway-dev.llm-gateway.mycompany.com/health
```

---

## 6. 체크리스트

- [ ] ECR repository 7개 생성 (또는 이미 존재)
- [ ] 모든 이미지 ECR 푸시 완료
- [ ] `install-eks.sh` 성공 (`✓ 배포 완료`)
- [ ] `kubectl get pods -n llm-gateway` 에서 6개 Deployment의 Pod 전부 Running
- [ ] `kubectl get ingress -n llm-gateway` 에서 ALB DNS 확보
- [ ] `kubectl get externalsecret -n llm-gateway` `READY: True`
- [ ] `curl http://$ALB_DNS/health` → 200 OK

---

## 7. 다음 단계

`helm install` 이 끝나면 **migration Job 이 모든 DB 준비 (schemas, tables, seed data, gateway user, GRANT) 를 자동 수행**한 상태입니다. 별도 후속 작업 불필요.

**바로 [05-smoke-test.md](./05-smoke-test.md) 진행** — 핵심 기능 E2E 확인 (VK 발급, Claude API 호출, Admin UI 접속).

---

[👈 03-secrets.md](./03-secrets.md) | [다음: 05-smoke-test.md 👉](./05-smoke-test.md)
