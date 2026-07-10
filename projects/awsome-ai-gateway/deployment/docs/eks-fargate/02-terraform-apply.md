# 02. Terraform Apply — VPC · EKS · Aurora · ElastiCache 프로비저닝

**목적**: AWS 인프라 전체를 Terraform으로 한 번에 생성.
**소요**: 45~60분 (apply 대기 시간 포함)

---

## 1. tfstate 백엔드 준비 (최초 1회만)

Terraform이 자기 state를 S3에 저장하려면 S3 버킷과 DynamoDB 테이블이 먼저 있어야 합니다.

### 1.1 bootstrap 스크립트 실행

```bash
cd /path/to/LLM-Gateway-Vanilla
./deployment/scripts/bootstrap-tfstate.sh
```

✅ 아래와 같이 출력되면 성공:
```
==> Bootstrapping Terraform state backend
✓ AWS Account: 123456789012
✓ S3 bucket llm-gateway-tfstate 이미 존재
✓ Versioning 활성화
✓ SSE-S3 암호화 설정
✓ Public access 차단
✓ 90일 지난 이전 버전 자동 삭제
✓ DynamoDB table llm-gateway-tflock 이미 존재

✅ Bootstrap 완료.
```

🐛 `aws s3api create-bucket` 실패 — 이미 다른 계정에서 쓰는 버킷 이름이면 `TFSTATE_BUCKET` 환경변수로 다른 이름 지정:
```bash
TFSTATE_BUCKET=my-org-gateway-tfstate ./deployment/scripts/bootstrap-tfstate.sh
```

이렇게 바꾸면 `deployment/terraform/environments/*/backend.tf` 에서도 bucket 이름을 동일하게 수정해야 합니다.

---

## 2. 환경별 Terraform 디렉토리 진입

```bash
cd deployment/terraform/environments/$ENV   # dev 또는 prod
```

---

## 3. `terraform.tfvars` 설정

실제 환경 값을 담는 파일을 만듭니다.

### 3.1 예시 파일 복사

```bash
cp terraform.tfvars.example terraform.tfvars
```

### 3.2 편집 필수 항목

```bash
vim terraform.tfvars  # 또는 kiro terraform.tfvars or code terraform.tfvars
```

**반드시 수정해야 할 값**:

| 변수 | 설명 | 예시 |
|------|-----|------|
| **그 외** | | |
| `bedrock_allowed_model_arns` | 실제 승인받은 Bedrock 모델 ARN | (tfvars.example의 Claude 목록 그대로 사용 가능) |
| `eks_access_entries.*.principal_arn` | 관리자 IAM 역할 ARN | `"arn:aws:iam::123456789012:role/Admin"` |
| **Cognito (OIDC IDP)** | | |
| `cognito_groups` | OIDC 자동 팀 매핑 그룹 목록. `Claude_<team>` 패턴이 매칭 대상. `ClaudeAdmin` 은 admin role 부트스트랩. | (tfvars.example default 사용 또는 운영 팀 구성에 맞게 추가) |
| `cognito_domain_suffix` (옵션) | Hosted UI 도메인 suffix. 최종: `{project}-{env}-{suffix}.auth.{region}.amazoncognito.com` (전 세계 unique) | `"auth"` (default) |

⚠️ **`ACCOUNT_ID` placeholder**: `arn:aws:iam::ACCOUNT_ID:role/...` 형식을 **실제 AWS Account ID** 로 바꾸세요. Account ID는 `aws sts get-caller-identity --query Account --output text`.

### 3.3 이메일 발송 설정

알림 이메일은 **고객 내부 메일 API** 또는 **SMTP**를 통해 발송됩니다. Terraform 에서 별도로 설정할 항목은 없으며, Helm values 에서 `notificationWorker.email` 섹션을 구성합니다 (04-helm-install.md 참조).

### 3.4 RDS Proxy — dev/prod 공통 default

Aurora 앞단에 **RDS Proxy** 를 배치해 connection pool 을 제공합니다. 수천 Pod 합산 연결이 Aurora `max_connections` 한계를 상회해도 Proxy 가 풀링. **dev/prod 모두 `enable_rds_proxy=true` 가 기본값** — 별도 설정 없이 `terraform apply` 만 실행하면 Proxy, gateway user Secret, 관련 IAM/SG 전부 자동 생성.

핵심 장점:
- Terraform 이 gateway user 비밀번호(`random_password`) + Secrets Manager `{username, password}` secret 까지 자동 관리 → operator 는 03-secrets.md 의 DB secret 수동 생성 단계 **스킵**
- Helm values 의 `database.external.host` 는 `application_db_endpoint` output 하나만 주입하면 Proxy on/off 에 관계없이 올바른 호스트 선택
- `install-eks.sh` 가 `application_db_endpoint` 를 자동 추출해 `helm install` 에 주입

#### 3.4.1 Pinning 회피 — 이미 values 파일에 반영됨

RDS Proxy PostgreSQL 은 prepared statement 사용 시 세션이 Proxy 연결 하나에 pin 되어 pooling 효과 감소. 4개 서비스(`gateway-proxy`/`admin-api`/`cost-recorder-worker`/`notification-worker`) 는 `DB_STATEMENT_CACHE_SIZE` 환경변수로 asyncpg `connect_args.statement_cache_size` 를 토글하도록 구현됨. 렌더링 경로:

```
values.database.external.statementCacheSize  →  ConfigMap.DB_STATEMENT_CACHE_SIZE  →  Pod env  →  asyncpg
```

| 환경 | values 파일 내 값 | 효과 |
|------|---|---|
| dev (`values-eks-fargate-dev.yaml`) | `statementCacheSize: 0` | Proxy pinning 회피 |
| prod (`values-eks-fargate-prod.yaml`) | `statementCacheSize: 0` | 동일 |
| 로컬 finch (Postgres 직접 연결) | 미설정 → base values 기본 `100` | prepared statement 캐시 성능 이득 |

⚠️ **Proxy 를 끄는 경우** (`-var enable_rds_proxy=false`): values 파일의 `statementCacheSize` 를 `100` 또는 해당 라인 삭제 후 `helm upgrade`.

#### 3.4.2 Terraform output 구조

```hcl
rds_proxy_enabled        # bool — Proxy 활성 여부
rds_proxy_endpoint       # string|null — Proxy endpoint (비활성 시 null)
application_db_endpoint  # string — Proxy on 이면 proxy_endpoint, off 면 aurora_endpoint
gateway_user_secret_arn  # string|null — Proxy auth 용 gateway user secret (sensitive)
db_secret_arn            # string|null — Helm ExternalSecret 이 참조하는 /db secret (sensitive)
```

`install-eks.sh` 가 `application_db_endpoint` 를 자동 사용하므로 operator 가 수동 조작할 일은 없음.

#### 3.4.3 Proxy 를 굳이 꺼야 하는 경우

다음 상황에서만 `-var enable_rds_proxy=false` 고려:

- 로컬 테스트용 최소 비용 (~$15/월 절약)
- Proxy 관련 디버깅 격리 (Proxy 자체가 원인인지 검증)
- 일부 PostgreSQL 기능(LISTEN/NOTIFY 등) 필요

이 경우:
```bash
cd deployment/terraform/environments/<env>
terraform apply -var enable_rds_proxy=false

# values 파일에서 statementCacheSize 라인 제거 또는 100으로 변경 후 helm upgrade
```

---

### 3.5 `.gitignore` 확인

```bash
cat ../../.gitignore | grep tfvars
# terraform.tfvars    이 줄 있어야 함
```

✅ 있으면 OK. 없으면 수동 추가.

---

## 4. `terraform init`

Provider + module 다운로드.

```bash
terraform init
```

✅ 마지막에 `Terraform has been successfully initialized!` 가 나와야 함.

🐛 `Failed to get existing workspaces` 에러 — backend 접근 권한 문제. IAM 권한 확인.

🐛 `Initializing modules...` 에서 멈춤 — 네트워크 문제. 재시도.

---

## 5. `terraform plan`

실제 적용 전, 무엇이 만들어질지 미리보기.

```bash
terraform plan -out=tfplan
```

**출력 해석**:
```
Plan: 145 to add, 0 to change, 0 to destroy.
```

✅ `to add`: 신규 리소스 개수. dev는 약 130~150개, prod는 160~200개 정도가 정상.

⚠️ `to destroy` 가 0이 아니면 기존 리소스가 삭제된다는 뜻. 첫 배포에서는 반드시 0이어야 함.

---

## 6. `terraform apply` — 인프라 생성

### 6.1 실행

```bash
terraform apply tfplan
```

### 6.2 예상 소요시간

약 **45분~60분**. 내부적으로 이런 순서로 진행됩니다:

```
  1. VPC + Subnets (3분)
  2. NAT Gateway (3분, EIP 할당 대기)
  3. KMS keys (1분)
  4. Aurora cluster (15~20분, 가장 오래 걸림)
  5. ElastiCache replication group (10~15분)
  6. EKS cluster (15분)
  7. Fargate Profiles × 3 (3분 × 3)
  8. IAM/IRSA Roles × 3 (2분)
  9. ALB Controller Helm 설치 (2분)
  10. External Secrets Operator Helm 설치 (2분)
```

### 6.3 진행 중 터미널 안 꺼도 됨

중간에 Ctrl+C 하면 안 되지만, 터미널이 끊어져도 Terraform은 내부 backend lock으로 state 안정적으로 유지. 재접속 후 `terraform apply` 재실행하면 이어서 진행.

### 6.4 성공 출력

✅ 마지막에 아래와 같이 나오면 성공:

```
Apply complete! Resources: 145 added, 0 changed, 0 destroyed.

Outputs:

admin_api_role_arn = "arn:aws:iam::123456789012:role/llm-gateway-dev-admin-api"
aurora_endpoint = "llm-gateway-dev.cluster-xxx.ap-northeast-2.rds.amazonaws.com"
cluster_endpoint = "https://XXX.gr7.ap-northeast-2.eks.amazonaws.com"
cluster_name = "llm-gateway-dev"
elasticache_endpoint = "llm-gateway-dev.xxx.cache.amazonaws.com"
gateway_proxy_role_arn = "arn:aws:iam::123456789012:role/llm-gateway-dev-gateway-proxy-bedrock"
...
```

이 값들은 다음 단계에서 사용됩니다.

### 6.5 Outputs 저장 (선택)

```bash
terraform output -json > /tmp/tf-outputs-$ENV.json
```

`install-eks.sh` 가 자동으로 읽으므로 필수는 아닙니다.

---

## 7. kubectl 컨텍스트 설정

EKS에 kubectl로 접근하려면 kubeconfig 업데이트가 필요합니다.

```bash
aws eks update-kubeconfig \
  --region "$AWS_REGION" \
  --name "$(terraform output -raw cluster_name)"
```

✅ 확인:
```bash
kubectl get nodes
```

**Fargate에서는 "노드"가 none으로 나옴** — 각 Pod가 자체 micro-VM에서 돌기 때문에 정상:
```
No resources found
```

대신 아래로 확인:
```bash
kubectl get pods -A
```

✅ `kube-system` 네임스페이스에 coredns Pod가 Running 상태여야 함.

🐛 `connection refused` / `Unauthorized` — [troubleshooting.md의 "kubectl 인증 실패"](./troubleshooting.md#kubectl-인증-실패) 참조.

---

## 8. Terraform apply 검증

### 8.1 EKS Fargate Profile 확인

```bash
aws eks list-fargate-profiles \
  --cluster-name "$(terraform output -raw cluster_name)" \
  --region "$AWS_REGION"
```

✅ 3개 profile (`kube-system`, `application`, `platform`) 보여야 함.

### 8.2 Aurora 확인

```bash
aws rds describe-db-clusters \
  --db-cluster-identifier "llm-gateway-$ENV" \
  --query 'DBClusters[0].Status' --output text
```

✅ `available` 이어야 함.

### 8.3 IRSA Role 확인

```bash
terraform output all_role_arns
```

✅ 3개 role ARN 보여야 함:
- gateway_proxy
- admin_api
- external_secrets

### 8.4 ALB Controller 확인

```bash
kubectl get deployment aws-load-balancer-controller -n kube-system
```

✅ `READY 1/1` 또는 `2/2` (prod) 여야 함.

### 8.5 External Secrets Operator 확인

```bash
kubectl get deployment -n external-secrets
```

✅ 세 디플로이먼트가 `READY 1/1` 이어야 함:
- `external-secrets`
- `external-secrets-cert-controller`
- `external-secrets-webhook`

> **ClusterSecretStore 는 이 단계에서 없는 게 정상**. `kubectl get clustersecretstore` → `No resources found` 가 맞음. Terraform + Helm 의 CRD chicken-and-egg 문제를 피하려고 `ClusterSecretStore` 생성은 `04-helm-install.md` 의 `install-eks.sh` 로 분리했습니다. 04 단계 실행 후 자동 생성됩니다.

---

## 9. 체크리스트

- [ ] `terraform apply` 성공 (신규 배포면 `130~150 added, 0 changed, 0 destroyed`)
- [ ] `kubectl get pods -A` 에서 coredns 2개 **Running** (Pending 이면 [troubleshooting — coredns Pending](./troubleshooting.md#증상-coredns-pending--fargate-taint-untolerated))
- [ ] Aurora `status: available`
- [ ] `all_role_arns` 출력에 4개 ARN (gateway_proxy, admin_api, notification_worker, external_secrets)
- [ ] `aws-load-balancer-controller` READY
- [ ] `external-secrets` / `external-secrets-cert-controller` / `external-secrets-webhook` 3개 deployment READY
- [ ] **ClusterSecretStore 는 이 단계에서는 없음이 정상** (04 단계에서 생성됨)

---

## 문제가 생기면

각자 상황에 맞는 troubleshooting 섹션을 참조하세요:

- [Terraform apply 실패](./troubleshooting.md#terraform-apply-실패)
- [Aurora 프로비저닝 지연](./troubleshooting.md#aurora-프로비저닝-지연)
- [EKS 클러스터 접근 불가](./troubleshooting.md#kubectl-인증-실패)

---

[👈 01-prerequisites.md](./01-prerequisites.md) | [다음: 03-secrets.md 👉](./03-secrets.md)
