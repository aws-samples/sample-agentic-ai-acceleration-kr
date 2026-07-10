# 06. Upgrade · Rollback

**목적**: 코드 변경을 운영에 반영하는 방법과, 문제 생기면 되돌리는 방법.

---

## 1. 코드만 변경된 경우 (가장 흔한 케이스)

### 1.1 버전 올리기

```bash
cd /path/to/LLM-Gateway-Vanilla
NEW_VERSION="1.0.1"
```

### 1.2 이미지 다시 빌드 + 푸시

```bash
# 변경된 서비스만 빌드
docker build --platform linux/amd64 \
  -t "$ECR_BASE/gateway-proxy:$NEW_VERSION" \
  ./gateway-proxy
docker push "$ECR_BASE/gateway-proxy:$NEW_VERSION"
```

### 1.3 Helm upgrade

방법 A — `install-eks.sh` 재사용 (모든 서비스가 같은 버전 쓸 때):
```bash
# 편집: values-eks-fargate-dev.yaml 에서 image.tag 설정, 또는 --set 직접
helm upgrade llm-gateway ./deployment/charts/llm-gateway \
  --namespace llm-gateway \
  -f ./deployment/charts/llm-gateway/values-eks-fargate-$ENV.yaml \
  --set gatewayProxy.image.tag=$NEW_VERSION \
  --atomic --wait --timeout 15m
```

방법 B — 특정 서비스만 업그레이드:
```bash
helm upgrade llm-gateway ./deployment/charts/llm-gateway \
  --reuse-values \
  --set gatewayProxy.image.tag=$NEW_VERSION \
  --namespace llm-gateway \
  --atomic --wait
```

⚠️ `--reuse-values` 주의: 이전 `--set` 값은 유지되지만 **values 파일 변경은 반영 안 됨**. values 파일 수정 시엔 `-f` 로 다시 줘야 함.

### 1.4 롤링 업데이트 진행 확인

```bash
kubectl rollout status deployment/llm-gateway-gateway-proxy \
  -n llm-gateway --timeout=10m
```

✅ `deployment "llm-gateway-gateway-proxy" successfully rolled out`

Pod 교체 진행 상황:
```bash
kubectl get pods -n llm-gateway -l app.kubernetes.io/component=gateway-proxy -w
```

`Terminating` → 새 `Pending` → `Running` 순서로 하나씩 교체.

---

## 2. values/config 변경 (코드 동일)

예: HPA 최대 replica 증설, 리소스 requests 조정, 환경변수 추가.

### 2.1 values 파일 편집

```bash
vim deployment/charts/llm-gateway/values-eks-fargate-$ENV.yaml
```

### 2.2 Helm upgrade

```bash
helm upgrade llm-gateway ./deployment/charts/llm-gateway \
  -f ./deployment/charts/llm-gateway/values-eks-fargate-$ENV.yaml \
  --namespace llm-gateway \
  --atomic --wait
```

⚠️ **Secret/ConfigMap 변경** → Pod 자동 재시작됨 (template에 `checksum/config` annotation 주입으로).

---

## 3. 인프라 변경 (Terraform)

Aurora 인스턴스 클래스 변경, VPC CIDR 확장 등 **인프라 구조 변경**은 Terraform을 거칩니다.

### 3.1 Terraform plan 으로 변경사항 확인

```bash
cd deployment/terraform/environments/$ENV
vim terraform.tfvars  # 값 수정
terraform plan
```

⚠️ 출력의 **`destroy` 필드** 반드시 확인:
- `aws_rds_cluster_instance` 가 replace되면 **짧은 다운타임**
- `aws_vpc` 나 `aws_subnet` 이 replace되면 **전체 재생성** → ❌ 절대 바로 apply 금지

### 3.2 terraform apply

```bash
terraform apply
```

---

## 4. 롤백

### 4.1 Helm 롤백 (가장 빠름, 추천)

```bash
# 릴리즈 이력 확인
helm history llm-gateway -n llm-gateway
```

```
REVISION   UPDATED                   STATUS       CHART                  APP VERSION   DESCRIPTION
1          Mon Apr 22 15:30:00 2026  superseded   llm-gateway-0.1.0   1.0.0         Install complete
2          Tue Apr 23 10:15:00 2026  deployed     llm-gateway-0.1.0   1.0.1         Upgrade complete   ← 문제 있음
```

revision 1 로 되돌리기:

```bash
helm rollback llm-gateway 1 -n llm-gateway --wait --timeout 10m
```

✅ 1~2분 내 Pod가 이전 이미지로 교체됨.

### 4.2 롤백이 안전하지 않은 경우

- **DB 스키마가 바뀐 경우**: 구 이미지가 새 스키마를 이해 못해 CrashLoop
  - **해결**: 마이그레이션은 반드시 **backward-compatible** 하게. 컬럼 추가는 nullable, 삭제는 2단계 (코드에서 use 중단 → 다음 릴리즈에서 drop)
- **Secret 스키마가 바뀐 경우**: 구 이미지가 parse 못 함
  - **해결**: Secret 키는 추가만 하고 제거는 2단계

### 4.3 강제 롤백 (긴급)

⚠️ **다운타임 발생** — 모든 Pod가 삭제 후 재생성됩니다. 4.1 롤백이 불가할 때만 사용.

```bash
# 현재 릴리즈 바로 삭제 + 재설치
helm uninstall llm-gateway -n llm-gateway
# Secret/PVC 는 "helm.sh/resource-policy: keep" 때문에 보존됨
helm install llm-gateway ./deployment/charts/llm-gateway \
  -f ./deployment/charts/llm-gateway/values-eks-fargate-$ENV.yaml \
  ... --version <old-chart-version>
```

⚠️ 마이그레이션 Job 이 재실행됨. 이미 진행된 migration 은 skip되므로 안전.

---

## 5. 카나리/블루그린 (추가 구성 필요)

기본 chart는 **Rolling Update** 만 지원합니다. 카나리/블루그린을 원하면:
- **Argo Rollouts** 설치 후 `Rollout` 리소스로 전환
- 또는 **Flagger** + Istio/Linkerd 조합

이 가이드 범위 밖이며, 필요 시 별도 구성이 필요합니다.

---

## 6. 일반적 시나리오

### 6.1 긴급 패치 절차

```bash
# 1. 핫픽스 브랜치 생성
git checkout -b hotfix/critical-bug

# 2. 수정 + 빌드 + 푸시
docker build ... -t $ECR_BASE/gateway-proxy:1.0.1-hotfix ./gateway-proxy
docker push ...

# 3. dev 에서 먼저 배포 + 검증
ENV=dev helm upgrade ... --set gatewayProxy.image.tag=1.0.1-hotfix ...
./deployment/scripts/smoke-test.sh --env dev

# 4. prod 배포
ENV=prod helm upgrade ... --set gatewayProxy.image.tag=1.0.1-hotfix ...

# 5. 문제 시 즉시 롤백
helm rollback llm-gateway <prev-revision> -n llm-gateway
```

### 6.2 단계적 릴리즈 (수동 카나리)

replicas 2 → 1개씩 새 버전, 1개는 구 버전으로 유지하고 싶다면:
→ 기본 Helm 으로는 어렵습니다. 위 Argo Rollouts 사용 권장.

임시 방편: `replicaCount=4` 로 올리고 2개는 신규, 2개는 구버전. (서로 다른 Deployment 2개로 운영)

---

---

[👈 05-smoke-test.md](./05-smoke-test.md) | [07-cognito-onboarding.md 👉](./07-cognito-onboarding.md)
