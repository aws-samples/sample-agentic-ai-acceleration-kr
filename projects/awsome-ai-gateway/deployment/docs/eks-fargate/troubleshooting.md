# EKS Fargate Troubleshooting

⚠️ **이 문서는 실제 배포하면서 만나는 이슈가 쌓여 완성됩니다.** 현재는 일반적인 예상 이슈만 담겨 있고, 실배포 중 새 이슈가 나오면 즉시 추가합니다.

---

## 목차

- [terraform apply 실패](#terraform-apply-실패)
- [Aurora 프로비저닝 지연](#aurora-프로비저닝-지연)
- [kubectl 인증 실패](#kubectl-인증-실패)
- [이미지 pull 실패](#이미지-pull-실패)
- [ExternalSecret 동기화 실패](#externalsecret-동기화-실패)
- [Fargate Pod Pending 장기화](#fargate-pod-pending-장기화)
- [ALB 503 응답](#alb-503-응답)
- [Bedrock 403 AccessDenied](#bedrock-403-accessdenied)
- [Bedrock `global.*` vs `anthropic.*` ARN 불일치](#bedrock-global-vs-anthropic-arn-불일치)
- [이메일 발송 실패 (Internal API / SMTP)](#이메일-발송-실패-internal-api--smtp)
- [EKS minor version downgrade 금지](#증상-eks-minor-version-downgrade-금지)
- [Stale tfplan — DBClusterAlreadyExistsFault](#증상-stale-tfplan--dbclusteralreadyexistsfault)
- [ESO Helm install 중 ALB Controller webhook endpoint 없음](#증상-eso-helm-install-중-alb-controller-webhook-endpoint-없음)
- [Aurora AZ drift — cluster replace 트리거](#증상-aurora-az-drift--cluster-replace-트리거)
- [coredns Pending — fargate taint untolerated](#증상-coredns-pending--fargate-taint-untolerated)
- [helm/kubernetes provider `system:anonymous` 거부 — EKS 토큰 만료](#증상-helmkubernetes-provider-systemanonymous-거부--eks-토큰-만료)
- [`kubectl run` timeout — default namespace 에 Fargate Profile 없음](#증상-kubectl-run-timeout--default-namespace-에-fargate-profile-없음)
- [zsh `$var:latest` 가 `:l` modifier 로 해석되어 repo 이름 망가짐](#증상-zsh-varlatest-가-l-modifier-로-해석되어-repo-이름-망가짐)
- [ESO webhook TLS SAN 불일치 — ClusterSecretStore 생성 실패](#증상-eso-webhook-tls-san-불일치--clustersecretstore-생성-실패)
- [`helm install` pre-install hook 에서 ExternalSecret 검증 webhook 실패](#증상-helm-install-pre-install-hook-에서-externalsecret-검증-webhook-실패)
- [ExternalSecret `SecretSyncedError: secret already exists` — stale K8s Secret owner 충돌](#증상-externalsecret-secretsyncederror-secret-already-exists--stale-k8s-secret-owner-충돌)
- [`helm install` 에 `--cleanup-on-fail` 불가](#증상-helm-install-에---cleanup-on-fail-불가)
- [이미지 경로 중복 — `llm-gateway/llm-gateway/<svc>` ImagePullBackOff](#증상-이미지-경로-중복--llm-gatewayllm-gatewaysvc-imagepullbackoff)
- [migration Job `secret not found` — ExternalSecret sync 전 실행](#증상-migration-job-secret-not-found--externalsecret-sync-전-실행)
- [migration `permission denied for schema public` — PG 15+ 권한 변경](#증상-migration-permission-denied-for-schema-public--pg-15-권한-변경)
- [HPA 가 `<unknown>/65%` 로 멈춰있음](#hpa-가-unknown65-로-멈춰있음)

---

## terraform apply 실패

### 증상 1: `InvalidParameterException: Subnets specified in different VPCs`

```
Error: creating EKS Fargate Profile: InvalidParameterException: ...
```

**원인**: `private_subnet_ids` 와 `vpc_id` 가 일치하지 않음.

**해결**: `terraform destroy` 후 다시 `apply`. VPC 모듈 output을 참조하고 있는지 확인.

### 증상 2: `AddressLimitExceeded: The maximum number of addresses has been reached`

**원인**: NAT Gateway용 EIP가 region limit에 걸림.

**해결**:
```bash
# 할당된 EIP 목록
aws ec2 describe-addresses --region "$AWS_REGION"
# 사용 중 아닌 것 반환
aws ec2 release-address --allocation-id eipalloc-XXX --region "$AWS_REGION"
```
또는 AWS Support에 limit 증설 요청.

### 증상 3: `Bucket already exists`

**원인**: S3 tfstate 버킷 이름이 전역 고유여야 하는데 누가 먼저 선점.

**해결**: `TFSTATE_BUCKET` 환경변수로 다른 이름 지정:
```bash
TFSTATE_BUCKET=myorg-llm-gateway-tfstate ./deployment/scripts/bootstrap-tfstate.sh
```
그리고 `deployment/terraform/environments/*/backend.tf` 의 bucket 이름도 같이 수정.

### 증상: `DBClusterParameterGroupNotFound`

```
Error: creating RDS Cluster: DBClusterParameterGroupNotFound: DBClusterParameterGroup not found: llm-gateway-dev-cluster-pg
```

**원인**: Aurora 모듈에 `db_cluster_parameter_group_name` 은 지정했는데 `create_db_cluster_parameter_group = true` 가 없어 모듈이 "이미 있는 PG 참조" 모드로 동작. 존재하지 않는 PG 를 찾아 404.

**해결**: `modules/aurora-postgresql/main.tf` 에 `create_db_cluster_parameter_group = true` 와 `create_db_parameter_group = true` 명시. 우리 리포는 2026-04-23 이후 수정 완료.

---

### 증상: `helm_release aws-load-balancer-controller ... context deadline exceeded`

```
Error: release aws-load-balancer-controller failed, and has been uninstalled due to atomic being set: context deadline exceeded
module.alb_controller.helm_release.alb_controller: Still creating... [10m00s elapsed]
```

**원인**: Fargate Profile 의 `kube-system` selector 가 `labels.k8s-app = kube-dns` 같은 특정 라벨로 제한되면, ALB Controller 처럼 다른 라벨을 가진 kube-system Pod 는 **어느 Fargate Profile 에도 매칭 안 됨 → 영원히 Pending** → 10분 timeout.

```bash
# Pending 상태 확인
kubectl get pods -n kube-system
kubectl describe pod <alb-controller-pod> -n kube-system | grep -A5 Events
# "0/0 nodes are available" 또는 "no Fargate profile matches Pod selectors"
```

**해결**: `modules/eks-fargate/main.tf` 의 `kube-system` Fargate Profile selector 에서 `labels` 절 제거 → namespace 전체 매칭:

```hcl
kube-system = {
  name = "kube-system"
  selectors = [
    { namespace = "kube-system" }   # labels 제거
  ]
}
```

우리 리포는 수정 완료.

---

### 증상: `no matches for kind "ClusterSecretStore" in version "external-secrets.io/v1beta1"`

```
Error: unable to build kubernetes objects from release manifest:
resource mapping not found for name: "aws-secrets-manager" namespace: ""
no matches for kind "ClusterSecretStore" in version "external-secrets.io/v1beta1"
```

**원인**: Helm chart 가 **CRD 등록과 CR 생성을 같은 apply 로 시도**. CRD 가 아직 K8s API 에 반영되지 않은 상태에서 CR(`ClusterSecretStore`) 생성 시도 → 실패. Helm 의 알려진 한계.

**해결**: ClusterSecretStore 생성을 **Helm chart 와 분리**:
- Terraform 은 ESO(Helm chart) 만 설치
- `install-eks.sh` 의 `ensure_cluster_secret_store()` 가 ESO Pod Ready 대기 후 `kubectl apply` 로 ClusterSecretStore 생성

우리 리포는 2026-04-23 이후 수정 완료.

---

### 증상: `Failed to construct REST client: no client config`

```
Error: Failed to construct REST client
  with module.external_secrets.kubernetes_manifest.cluster_secret_store
  cannot create REST client: no client config
```

**원인**: Terraform + Kubernetes provider 의 chicken-and-egg 문제. `kubernetes_manifest` 리소스는 **plan 단계에서 실제 K8s API 서버에 연결**해야 하는데, EKS 가 아직 안 만들어져서 연결할 곳이 없음.

**해결**: `kubernetes_manifest` 대신 **Helm chart 의 `extraObjects` values** 로 K8s 리소스 생성. Helm 은 chart 설치 시점에 API 를 호출하므로 chicken-and-egg 없음.

우리 리포는 2026-04-22 이후 버전에서 ClusterSecretStore 를 `helm_release.external_secrets.values.extraObjects` 로 이전 완료. 이전 버전이라면 `modules/external-secrets/main.tf` 를 리포의 현재 버전과 동기화하세요.

**주의**: `kubernetes_namespace` 도 비슷한 패턴. 우리 리포는 application namespace 생성을 Terraform 에서 빼고 `install-eks.sh` / `helm install --create-namespace` 로 이전.

---

### 증상: `ACCOUNT_ID:role/YOUR_ROLE invalid ARN`

```
Error: "principal_arn" (arn:aws:iam::ACCOUNT_ID:role/YOUR_ROLE) is an invalid ARN
```

**원인**: `terraform.tfvars.example` 을 복사만 하고 placeholder 를 실제 값으로 안 바꿈.

**해결 (가장 쉬움)**: `terraform.tfvars` 에서 `eks_access_entries = {}` 로 비우기. EKS module v20.x 는 apply 실행자를 자동으로 cluster-admin 으로 등록하므로 본인 계정은 접근 가능.

추가 사용자에게 권한 부여 시 실제 ARN 으로 교체:
```bash
aws sts get-caller-identity --query Arn --output text
# SSO 쓰는 경우 aws iam list-roles --query "Roles[?contains(RoleName, 'AWSReservedSSO')]"
```

---

### 증상: `description doesn't comply with restrictions`

```
Error: "description" doesn't comply with restrictions ("^[0-9A-Za-z_ .:/()#,@\\[\\]+=&;{}!$*-]*$"):
  "Aurora ← EKS Fargate"
```

**원인**: AWS Security Group / IAM description 필드는 ASCII 문자만 허용. 유니코드 화살표(`←`, `→`), em-dash(`—`) 등 금지.

**해결**: description 필드의 유니코드를 ASCII로 교체 (`←` → `<-`, `—` → `-`). 우리 리포는 2026-04-22 이후 버전에서 이미 수정됨. 이전 버전이면 다음 파일들 확인:
- `modules/aurora-postgresql/main.tf` — `security_group_rules.eks_ingress.description`
- `modules/elasticache-valkey/main.tf` — `aws_security_group.this.description`
- `modules/irsa/main.tf` — `role_description`, `description`

**주의**: Terraform `variable description` / `output description` 은 AWS로 전달되지 않으므로 유니코드 OK.

---

### 증상: `cluster_security_group_additional_rules.alb_ingress` Missing required argument

```
Error: Missing required argument
  "cidr_blocks": one of `cidr_blocks,ipv6_cidr_blocks,...` must be specified
```

**원인**: 구버전 모듈 문법(`source_cluster_security_group = true`)이 EKS module 20.x에서 제거됨.

**해결**: 해당 rule 을 아예 제거. AWS Load Balancer Controller (target-type=ip) 가 Pod SG에 ALB 접근 규칙을 자동 추가하므로 수동 규칙 불필요. 우리 리포는 이미 수정됨.

---

### 증상: EKS minor version downgrade 금지

```
Error: updating EKS Cluster (llm-gateway-dev) version:
InvalidParameterException: Unsupported Kubernetes minor version update from 1.30 to 1.29
```

**원인**: 이미 생성된 EKS 클러스터가 `1.30` 인데 `variables.tf` 의 `eks_cluster_version` 기본값이 `"1.29"` 로 남아있음. AWS EKS 는 minor version **downgrade 를 허용하지 않음** (upgrade only).

**왜 1.30이 먼저 생성됐나**: 과거 apply 때 변수를 비웠거나, EKS 모듈이 자동으로 더 최신 버전을 선택했거나, 이후 AWS auto-upgrade 가 발생했을 수 있음.

**해결**: 기존 버전 이상으로 맞춤:
```hcl
# deployment/terraform/environments/{dev,prod}/variables.tf
variable "eks_cluster_version" {
  type    = string
  default = "1.30"   # 1.29 → 1.30 으로 업
}
```

**확인**:
```bash
aws eks describe-cluster --name llm-gateway-dev \
  --query 'cluster.version' --output text --region "$AWS_REGION"
# → 1.30
```

우리 리포는 2026-04-23 이후 버전에서 `1.30` 으로 기본값 상향 완료.

---

### 증상: Stale tfplan — DBClusterAlreadyExistsFault

```
Error: creating RDS Cluster (llm-gateway-dev):
  DBClusterAlreadyExistsFault: DB Cluster already exists
  with module.aurora.module.aurora.aws_rds_cluster.this[0]
```

**원인**: `tfplan` 파일은 **plan 생성 시점의 state** 를 기준으로 "create" 명령을 기록함. 이전 apply 가 중간에 실패 → state 에는 이미 Aurora cluster 가 있지만, 오래된 `tfplan` 은 "아직 없으니 create 해야 한다" 라고 기록되어 있음 → apply 시점에 AWS 가 "이미 존재" 라며 거부.

**확인**:
```bash
# State 에는 있나?
terraform state list | grep aws_rds_cluster
# AWS 에는 있나?
aws rds describe-db-clusters --db-cluster-identifier llm-gateway-dev \
  --query 'DBClusters[0].Status' --output text --region "$AWS_REGION"
# 둘 다 있으면 stale tfplan 케이스
```

**해결**: `tfplan` 을 폐기하고 **새로 생성**:
```bash
rm tfplan
terraform plan -out=tfplan    # state + AWS 기준으로 다시 생성
terraform apply tfplan
```

새 plan 에서 Aurora cluster 는 create 대상이 아닌 "no change" 또는 "update in-place" 로 표시됨. 반드시 확인 후 apply.

---

### 증상: ESO Helm install 중 ALB Controller webhook endpoint 없음

```
Error: release external-secrets failed:
  Internal error occurred: failed calling webhook "mservice.elbv2.k8s.aws":
  failed to call webhook:
  Post "https://aws-load-balancer-webhook-service.kube-system.svc:443/mutate-v1-service?timeout=10s":
  no endpoints available for service "aws-load-balancer-webhook-service"
```

**원인**: AWS Load Balancer Controller v2.4+ 는 **모든 namespace 의 Service 생성 이벤트에 Mutating Webhook** 을 붙임 (LoadBalancerClass 자동 부여용). ESO Helm install 이 Service 를 만드는 순간 ALB Controller 의 webhook 이 호출되는데, ALB Controller Pod 가 **Fargate 에서 아직 Ready 되지 않아** webhook endpoint 가 없음 → ESO 실패.

Fargate 에서 특히 자주 발생 — Pod schedule + ENI 할당 + 이미지 pull 이 EC2 보다 수십 초~1분 정도 오래 걸리기 때문.

**해결 (근본 차단 + 순서 강제 이중)**:

1. **`enableServiceMutatorWebhook=false`** — 우리는 ALB Ingress 만 쓰고 `type=LoadBalancer` Service 는 쓰지 않으므로 이 webhook 자체가 불필요. 끄면 ESO 가 Service 를 만들어도 webhook 호출 안 됨.

   `modules/alb-controller/main.tf` 에:
   ```hcl
   set {
     name  = "enableServiceMutatorWebhook"
     value = "false"
   }
   wait          = true
   wait_for_jobs = true
   ```

2. **설치 순서 강제** — `environments/*/main.tf` 의 `module "external_secrets"` 에:
   ```hcl
   depends_on = [module.eks, module.alb_controller]
   ```

**이미 설치된 상태라면**: ALB Controller Helm values 만 갱신:
```bash
helm upgrade aws-load-balancer-controller \
  eks/aws-load-balancer-controller -n kube-system \
  --reuse-values --set enableServiceMutatorWebhook=false
```

우리 리포는 2026-04-23 이후 버전에서 위 설정 반영 완료.

---

### 증상: Aurora AZ drift — cluster replace 트리거

```
# module.aurora.module.aurora.aws_rds_cluster.this[0] must be replaced
  ~ availability_zones = [ # forces replacement
      - "ap-northeast-2b",
    ]
```

**원인**: Aurora 모듈이 `availability_zones = var.azs` 로 AZ 를 **명시적으로 전달**하는데, AWS 는 DBSubnetGroup 에 있는 다른 AZ (b) 에도 자동으로 instance 를 분산시킴. Terraform config (2 AZ) vs 실제 AWS 상태 (3 AZ) 불일치 → drift 감지 → `availability_zones` 는 **create-only 속성** 이라 destroy+create 외엔 방법 없음 → cluster 전체 replace (데이터 손실 위험).

**근본 원인**: `availability_zones` 는 대부분 불필요한 제약임. DBSubnetGroup 이 이미 "어느 AZ 에 배포 가능한지" 를 결정하므로 중복 지정이 drift 를 유발.

**해결**: Aurora 모듈의 `availability_zones` 속성 전달을 제거:

```hcl
# modules/aurora-postgresql/main.tf
# availability_zones = var.availability_zones   # 주석 처리. AWS 가 subnet group AZ 에서 자동 선택
```

Provider 는 `availability_zones` 를 Optional+Computed 로 취급하므로, 넘기지 않으면 drift 감지 대상에서 제외됨.

**확인**:
```bash
terraform plan
# Aurora 관련 리소스가 "no changes" 또는 "update in-place" 로 나와야 정상
# "must be replaced" 가 뜨면 다른 원인 조사
```

**신규 계정 배포 영향**: 이 수정은 신규 클린 배포에도 안전. Aurora 는 DBSubnetGroup AZ 중 AWS 가 자동 선택 → 사용자가 명시할 필요 없음.

우리 리포는 2026-04-23 이후 버전에서 `availability_zones` 전달 제거 완료.

---

### 증상: coredns Pending — fargate taint untolerated

```
kubectl get pods -n kube-system
NAME                READY   STATUS    RESTARTS   AGE
coredns-XXX-aaaa    0/1     Pending   0          49m
coredns-XXX-bbbb    0/1     Pending   0          49m

kubectl describe pod -n kube-system coredns-XXX-aaaa
Events:
  Warning  FailedScheduling  0/1 nodes are available:
    1 node(s) had untolerated taint {eks.amazonaws.com/compute-type: fargate}
```

**원인**: Fargate 노드에는 `eks.amazonaws.com/compute-type=fargate:NoSchedule` **taint** 가 붙어있음. coredns 가 Fargate 에서 돌려면 Deployment pod template 에 이 taint 에 대한 **toleration** 이 있어야 함.

**중요한 발견 (2026-04-23)**: `configuration_values.computeType=Fargate` 를 설정해도 AWS EKS 가 **Deployment 에 toleration 을 자동 추가하지 않음**. `resolve_conflicts_on_update=OVERWRITE` 를 켜고 addon 을 완전히 replace 해도 마찬가지. 결국 `configuration_values` 에 **`tolerations` 을 명시적으로 직접 전달**해야 Deployment spec 에 반영됨.

특히 EKS minor version 업그레이드 (1.29 → 1.30) + Fargate Profile replacement 또는 clean install 직후에도 동일하게 발생.

**확인**:
```bash
# 1. Addon config (AWS EKS 관점)
aws eks describe-addon --cluster-name llm-gateway-$ENV \
  --addon-name coredns --region "$AWS_REGION" \
  --query 'addon.configurationValues'
# → {"computeType":"Fargate",...} 이어야 함

# 2. 실제 Deployment 의 toleration (K8s 관점)
kubectl get deployment coredns -n kube-system -o jsonpath='{.spec.template.spec.tolerations}'
# → eks.amazonaws.com/compute-type=fargate 관련 toleration 없으면 문제
```

**즉시 복구 (가장 빠름)**: Deployment 에 직접 toleration patch
```bash
kubectl patch deployment coredns -n kube-system --patch '
spec:
  template:
    spec:
      tolerations:
      - key: CriticalAddonsOnly
        operator: Exists
      - key: node-role.kubernetes.io/control-plane
        effect: NoSchedule
      - key: eks.amazonaws.com/compute-type
        operator: Equal
        value: fargate
        effect: NoSchedule
'
# 1~2분 후
kubectl get pods -n kube-system -l k8s-app=kube-dns
# → Running 1/1 이어야 함
```

**근본 수정 (Terraform 모듈)**: `configuration_values` 에 `tolerations` 을 **명시적으로** 포함해야 함. `computeType=Fargate` + `resolve_conflicts_on_*=OVERWRITE` 만으로는 Deployment spec 에 toleration 이 들어가지 않음.

```hcl
# modules/eks-fargate/main.tf
coredns = {
  addon_version               = var.addon_versions.coredns
  resolve_conflicts_on_update = "OVERWRITE"
  resolve_conflicts_on_create = "OVERWRITE"
  configuration_values = jsonencode({
    computeType  = "Fargate"
    replicaCount = 2
    # ★ Fargate taint 를 tolerate 하기 위해 명시 필수
    tolerations = [
      { key = "CriticalAddonsOnly", operator = "Exists" },
      { key = "node-role.kubernetes.io/control-plane", effect = "NoSchedule" },
      { key = "eks.amazonaws.com/compute-type", operator = "Equal", value = "fargate", effect = "NoSchedule" },
    ]
    resources = { ... }
  })
}
```

우리 리포는 2026-04-23 이후 버전에서 `tolerations` 명시 완료.

---

### 증상: helm/kubernetes provider `system:anonymous` 거부 — EKS 토큰 만료

```
Error: query: failed to query with labels:
  secrets is forbidden: User "system:anonymous" cannot list resource "secrets"
  in API group "" in the namespace "kube-system"
  with module.alb_controller.helm_release.alb_controller
```

**원인**: `providers.tf` 에서 Helm/Kubernetes provider 의 토큰을 `data "aws_eks_cluster_auth"` 로 가져왔을 때, 이 data source 는 **plan 시점에 토큰을 1회 fetch 해서 고정**함. 하지만 EKS 토큰은 **15분만 유효**.

발생 시나리오:
- 이전 apply 후 한참 지나 재apply
- apply 자체가 오래 걸려 apply 중간에 토큰 만료
- `-replace` 나 `-target` 으로 중간에 개입할 때

토큰이 만료되면 Helm provider 는 `Authorization` 헤더 없이 (또는 만료된 토큰으로) 호출 → EKS API 가 `system:anonymous` 로 처리 → 거부.

**해결**: `providers.tf` 에서 `data "aws_eks_cluster_auth"` 대신 `exec` 블록으로 매 호출 시마다 `aws eks get-token` 을 실행:

```hcl
provider "kubernetes" {
  host                   = module.eks.cluster_endpoint
  cluster_ca_certificate = base64decode(module.eks.cluster_certificate_authority_data)

  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", module.eks.cluster_name, "--region", var.aws_region]
  }
}

provider "helm" {
  kubernetes {
    host                   = module.eks.cluster_endpoint
    cluster_ca_certificate = base64decode(module.eks.cluster_certificate_authority_data)

    exec {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args        = ["eks", "get-token", "--cluster-name", module.eks.cluster_name, "--region", var.aws_region]
    }
  }
}
```

**전제**: `aws` CLI 가 PATH 에 있어야 하고 AWS credentials 가 준비되어 있어야 함 (이미 terraform apply 가 aws provider 에서 쓰는 credential 이므로 자동 충족).

우리 리포는 2026-04-23 이후 버전에서 dev/prod 모두 `exec` 방식으로 전환.

---

### 증상: `kubectl run` timeout — default namespace 에 Fargate Profile 없음

```
kubectl run psql-temp --rm -it --restart=Never \
  --image=postgres:16-alpine \
  -- psql ...
pod "psql-temp" deleted from default namespace
error: timed out waiting for the condition
```

**원인**: `kubectl run` 은 `-n` 옵션이 없으면 **`default` namespace** 에 Pod 생성. 우리 Fargate Profile 은 `kube-system`, `llm-gateway`, `external-secrets`, `observability` 만 매칭하므로 `default` 에는 **Pod 를 schedule 할 Fargate 가 존재하지 않음** → 무한 Pending → `--rm` 의 timeout.

EKS Fargate 특성: 사용자가 Fargate Profile selector 에 명시한 namespace 만 Fargate 로 가능. EC2 와 달리 "노드 아무 곳에나" 뜰 수 없음.

**해결**: 매칭되는 namespace 에서 실행.
```bash
# llm-gateway namespace 가 없으면 생성
kubectl create namespace llm-gateway 2>/dev/null || true

# ★ 반드시 -n llm-gateway
kubectl run psql-temp -n llm-gateway --rm -it --restart=Never \
  --image=postgres:16-alpine \
  --env="PGPASSWORD=$PW" \
  -- psql -h "$AURORA_HOST" -U postgres_admin -d gateway
```

또는 `kube-system` 도 가능 (CoreDNS 등과 같은 profile). 하지만 운영 맥락상 llm-gateway 권장.

**고객/신규 계정 배포 시**: 첫 psql 진입 시 이 이슈가 재현됨. 문서 03-secrets.md 2.3 단계에 `-n llm-gateway` 명시 완료.

#### 보너스: heredoc 과 `-it` 충돌

```
kubectl run psql-temp -n llm-gateway --rm -it --restart=Never ... <<EOF
...
EOF

Unable to use a TTY - input is not a terminal or the right kind of file
warning: couldn't attach to pod/psql-temp, falling back to streaming logs: ...
psql (16.13, server 16.4)
SSL connection ...
pod "psql-temp" deleted
error: timed out waiting for the condition
```

**원인**: `-it` 의 `-t` (TTY) 옵션이 heredoc 의 non-TTY stdin 과 충돌. K8s 가 TTY attach 를 포기하고 stream logs 로 fallback 하는 과정에서 SQL 이 pod 의 psql 에 전달 안 됨 → psql 은 interactive mode 에서 입력 대기 → timeout.

**해결**: `-it` → `-i` 로 변경. heredoc 쓸 땐 항상 `-i` 만:
```bash
kubectl run psql-temp -n llm-gateway --rm -i --restart=Never \
  --image=postgres:16-alpine \
  --env="PGPASSWORD=$PW" \
  -- psql ... <<EOF
...
EOF
```

일반 interactive shell (`-- bash`) 은 `-it` 가 맞지만, **stdin 파일/heredoc 주입 시엔 `-i` 만**.

---

### 증상: zsh `$var:latest` 가 `:l` modifier 로 해석되어 repo 이름 망가짐

```
FATA[0000] image "....amazonaws.com/llm-gateway/admin-uiatest:latest": not found
                                                   ^^^^^^^^^^^^ admin-ui:latest 이어야 함
```

**원인**: zsh 는 `$var:X` 에서 `X` 가 특정 문자로 시작하면 **history-style parameter modifier** 로 해석합니다:
- `$svc:l` → `$svc` 을 lowercase 변환
- `$svc:u` → uppercase
- `$svc:r` → remove suffix
- `$svc:t` / `:h` / `:e` → 파일 경로 조작

`"$svc_dir:latest"` 의 경우 zsh 는 `:l` 을 modifier 로 파싱하고 뒤의 `atest` 는 리터럴로 붙여버림 → `admin-uiatest`.

**재현**:
```bash
svc=admin-ui
echo "$svc:latest"      # zsh → admin-uiatest   (bash → admin-ui:latest)
echo "${svc}:latest"    # zsh → admin-ui:latest (안전)
```

**해결**: 변수를 **중괄호 `${var}` 로 감싸서 경계 명시**. 다음 중 하나라도:
```bash
"${ECR_BASE}/${svc}:${VERSION}"   # 권장
"${svc}:latest"                   # 중괄호만 써도 됨
```

**배포 스크립트 원칙**: shell 이 zsh 든 bash 든 dash 든 안전하도록 **모든 변수는 `${}` 로 작성**. 04-helm-install.md 는 2026-04-23 이후 모든 이미지 태그에 중괄호 적용.

---

### 증상: ESO webhook TLS SAN 불일치 — ClusterSecretStore 생성 실패

```
Error from server (InternalError): error when creating "STDIN":
  Internal error occurred: failed calling webhook "validate.clustersecretstore.external-secrets.io":
  tls: failed to verify certificate:
    x509: certificate is valid for ip-10-20-2-154.ap-northeast-2.compute.internal,
          not external-secrets-webhook.external-secrets.svc
```

**원인**: ESO v0.10.x 의 known issue 로 추정. cert-controller 가 관리하는 `external-secrets-webhook` Secret 안의 TLS cert SAN 은 `DNS:external-secrets-webhook.external-secrets.svc` 로 올바르게 발급되지만, **webhook Pod 가 실제 TLS handshake 시 제공하는 cert 는 Pod IP 기반**으로 발급된 것만 응답. cert-controller 와 certwatcher 간 race condition 으로 Secret 의 올바른 cert 가 반영 안 됨.

Fargate 특히 자주 재현. Pod IP 가 매번 바뀌는데 cert 관리 주기와 어긋남.

**확인**:
```bash
# Secret 안의 cert SAN (올바른지)
kubectl get secret external-secrets-webhook -n external-secrets \
  -o jsonpath='{.data.tls\.crt}' | base64 -d | openssl x509 -text -noout | grep -A2 "Alternative"
# → DNS:external-secrets-webhook.external-secrets.svc 있어야 함

# 실제 Pod 가 제공하는 cert SAN (불일치 여부)
# (Pod 내부에 openssl 없어서 외부에서 확인 어려움 — 에러 메시지의 "valid for X" 가 진짜 응답)
```

**빠른 해결 — webhook failurePolicy 를 Ignore 로 patch**:
```bash
for vwc in secretstore-validate externalsecret-validate; do
  count=$(kubectl get validatingwebhookconfiguration "$vwc" \
    -o jsonpath='{range .webhooks[*]}{.name}{"\n"}{end}' | wc -l | tr -d ' ')
  for i in $(seq 0 $((count-1))); do
    kubectl patch validatingwebhookconfiguration "$vwc" --type='json' \
      -p="[{\"op\":\"replace\",\"path\":\"/webhooks/$i/failurePolicy\",\"value\":\"Ignore\"}]"
  done
done

# 그 다음 ClusterSecretStore 생성 (webhook 실패 허용)
kubectl apply -f - <<EOF
apiVersion: external-secrets.io/v1beta1
kind: ClusterSecretStore
metadata: { name: aws-secrets-manager }
spec:
  provider:
    aws:
      service: SecretsManager
      region: ${AWS_REGION}
      auth:
        jwt:
          serviceAccountRef: { name: external-secrets, namespace: external-secrets }
EOF
```

검증 webhook 이 꺼진 상태라도 ClusterSecretStore 객체 자체는 정상 동작 (syncing 에 영향 없음). security 측면에서는 webhook 이 안정화된 후 `failurePolicy=Fail` 로 복귀 권장.

**근본 해결 옵션**:
1. ESO 재설치: `terraform apply -replace='module.external_secrets.helm_release.external_secrets'`
2. ESO 버전 업그레이드: `chart_version` 을 최신(v0.11+)으로
3. cert-manager 통합 활성화: values 에 `webhook.certManager.enabled=true`

**우리 리포 대응**: `install-eks.sh` 의 `ensure_cluster_secret_store()` 가 webhook 호출 실패 시 자동으로 `failurePolicy=Ignore` 로 완화 후 재시도 (2026-04-23 이후 버전). 추가로 **`helm install` 직전**에도 ExternalSecret/SecretStore validating webhook 을 선제적으로 비활성 (2026-04-24 이후) — 아래 섹션 참조.

---

### 증상: `helm install` pre-install hook 에서 ExternalSecret 검증 webhook 실패

```
Error: INSTALLATION FAILED: failed pre-install:
  Hook pre-install llm-gateway/templates/common/secret.yaml failed:
  Internal error occurred: failed calling webhook "validate.externalsecret.external-secrets.io":
  tls: failed to verify certificate: x509: certificate is valid for
    ip-10-10-2-216.ap-northeast-2.compute.internal,
  not external-secrets-webhook.external-secrets.svc
```

**원인**: 위 "ESO webhook TLS SAN 불일치" 와 동일한 cert-controller race condition. 이번엔 `ClusterSecretStore` 생성이 아니라 **Helm 이 chart 의 `ExternalSecret` 리소스를 apply 할 때** 터짐.

Fargate 첫 배포에서 매우 자주 재현. 증상 메시지의 Pod IP 만 매 배포마다 바뀜.

**빠른 해결 — validating webhook 삭제 후 `helm install` 재시도**:

```bash
# 1) validating webhook 완전 삭제 (failurePolicy=Ignore 로도 안 되면 이 경로)
kubectl delete validatingwebhookconfiguration \
  externalsecret-validate secretstore-validate 2>&1

# 2) 이전 실패 release 정리 (pre-install 실패도 release name 을 점유함)
helm -n "$NAMESPACE" uninstall llm-gateway --no-hooks 2>&1 || true

# 3) helm install 재시도
./deployment/scripts/install-eks.sh "$ENV"
```

**왜 webhook 을 삭제해도 안전한가**:
- webhook 은 ExternalSecret/SecretStore **생성 시점 YAML schema 검증** 용도
- 런타임 동기화 (Secrets Manager → K8s Secret) 는 webhook 과 무관
- 우리 chart 가 생성하는 ExternalSecret 은 Helm 렌더링 결과이므로 이미 올바른 YAML
- ESO cert-controller 가 몇 분 후 webhook 을 자동 재생성 (기본 동작)

**우리 리포 대응 (2026-04-24)**: `install-eks.sh` 가 helm install 직전에 `pre_install_disable_es_webhooks()` 를 호출해 두 webhook 을 선제적으로 삭제 — 매 배포마다 재발하는 이슈를 자동 우회.

**근본 해결**: 위 "ESO webhook TLS SAN 불일치" 섹션의 3가지 옵션 (ESO 재설치 / 버전 업그레이드 / cert-manager 통합) 중 선택.

---

### 증상: ExternalSecret `SecretSyncedError: secret already exists` — stale K8s Secret owner 충돌

```
kubectl -n llm-gateway get externalsecret llm-gateway-redis
NAME                   STATUS              READY
llm-gateway-redis   SecretSyncedError   False

kubectl -n llm-gateway describe externalsecret llm-gateway-redis
  Events:
    Warning  UpdateFailed   external-secrets  secrets "llm-gateway-redis" already exists
```

**원인**: 이전 `helm install` 실패 / `helm uninstall --no-hooks` 후 **K8s Secret 은 남고 ExternalSecret 만 지워진 상태**. 다음 `helm install` 에서 새 ExternalSecret 이 만들어지지만 기존 Secret 의 `ownerReferences` 가 **옛 ExternalSecret UID** 를 가리켜 update 거부.

우리 chart 의 ExternalSecret `target` 에 `deletionPolicy: Retain` 이 기본이라 Secret 은 고아로 남음.

**빠른 해결**:
```bash
# 해당 K8s Secret 삭제 → ExternalSecret 이 30초 내 재생성
kubectl -n llm-gateway delete secret llm-gateway-redis
sleep 15
kubectl -n llm-gateway get externalsecret llm-gateway-redis
# STATUS=SecretSynced, READY=True 확인

# Redis 인증 필요한 Pod 들 재시작 (env 캐시 갱신)
for deploy in admin-api cost-recorder-worker notification-worker; do
  kubectl -n llm-gateway rollout restart deploy/llm-gateway-$deploy
done
```

같은 증상이 `llm-gateway-db`, `llm-gateway-app` 에서도 발생 가능 — 동일 패턴으로 처리.

**예방**: `helm uninstall` 시 `--no-hooks` 를 꼭 쓰되, ExternalSecret 이 만든 K8s Secret 은 별도로 정리:
```bash
helm -n "$NAMESPACE" uninstall llm-gateway --no-hooks
kubectl -n "$NAMESPACE" delete secret \
  llm-gateway-app llm-gateway-db llm-gateway-redis --ignore-not-found
```

**우리 리포 대응 (2026-04-24)**: `install-eks.sh` 가 helm install 직전에 기존 ExternalSecret-관리 Secret 들을 정리 (orphan K8s Secret 자동 감지).

---

### 증상: `helm install` 에 `--cleanup-on-fail` 불가

```
ℹ  Helm install 실행 중
Error: unknown flag: --cleanup-on-fail
```

**원인**: `--cleanup-on-fail` 플래그는 `helm upgrade` 전용. `helm install` 에서는 지원하지 않음.

**해결**: `helm upgrade --install` 로 통합. release 가 없으면 install, 있으면 upgrade 로 자동 동작하며 `--cleanup-on-fail` 도 항상 사용 가능.

```bash
helm upgrade --install "$RELEASE_NAME" "$CHART_DIR" \
    --namespace "$NAMESPACE" \
    --values "$VALUES_FILE" \
    ...set_args... \
    --atomic \
    --cleanup-on-fail \
    --timeout 15m \
    --wait
```

우리 리포는 2026-04-23 이후 버전에서 `install-eks.sh` 를 `helm upgrade --install` 로 통합.

---

### 증상: 이미지 경로 중복 — `llm-gateway/llm-gateway/<svc>` ImagePullBackOff

```
Failed to pull image "123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/llm-gateway/llm-gateway/migration:1.0.0":
  not found
  → llm-gateway 가 경로에 두 번 반복
```

**원인**: helm chart 의 `values.yaml` 에서 각 서비스 `image.repository` 가 이미 prefix 포함 (`llm-gateway/<service>`). 그런데 `install-eks.sh` 가 `global.imageRegistry` 에 `.../llm-gateway` 로 **registry + prefix** 를 모두 넣으면 chart template 이 조립할 때 **prefix 가 두 번 붙음**.

기대하는 조합:
- `global.imageRegistry` = **순수 registry URL** (e.g. `123456789012.dkr.ecr.ap-northeast-2.amazonaws.com`)
- `image.repository` = `llm-gateway/<service>` (prefix 포함)
- 조립 결과: `<registry>/llm-gateway/<service>:<tag>` — ECR 에 push 된 경로와 일치

잘못된 조합:
- `global.imageRegistry` = `<registry>/llm-gateway` ← ❌
- 조립 결과: `<registry>/llm-gateway/llm-gateway/<service>:<tag>` — 존재하지 않음

**해결**: `install-eks.sh` 의 `ecr_registry` 변수에서 `llm-gateway` prefix 제거. 우리 리포는 2026-04-23 이후 버전에서 수정 완료.

```bash
# install-eks.sh
local ecr_registry="${aws_account_id}.dkr.ecr.${AWS_REGION}.amazonaws.com"
#                                                                     ^^ /llm-gateway 빼기
```

**주의**: 04-helm-install.md 의 `ECR_BASE` 는 docker push 용으로 `.../llm-gateway` 까지 포함해야 함 (`${ECR_BASE}/<svc>:<tag>` 형태로 push). Chart 에 전달하는 값과는 별개 개념. Script 가 내부에서 `aws_account_id` 로 다시 조립하므로 환경변수로 공유하지 않음.

---

### 증상: migration Job `secret not found` — ExternalSecret sync 전 실행

```
Error: release llm-gateway failed ... failed pre-install:
  resource Job/.../migration not ready. status: Failed
  ... Error: secret "llm-gateway-db" not found
```

**원인**: chart 구조상 chicken-and-egg:
- `migration` Job = **`pre-install` hook** — helm install 의 첫 단계
- `llm-gateway-db` K8s Secret = **ExternalSecret → ESO sync 후** 생성 (일반 resource, pre-install 보다 나중)
- 결과: migration Pod 이 시작할 때 Secret 없음 → `secretKeyRef` fail

**해결 (2026-04-23 chart 수정 완료)**: hook weight + initContainer 패턴으로 chicken-and-egg 해소. helm install 한 번에 완결.

1. `templates/common/secret.yaml` — **ExternalSecret 들을 pre-install hook** 으로 승격 (weight **-30**). migration(-5) 보다 먼저 생성되어 ESO 가 sync 시작.
   ```yaml
   metadata:
     annotations:
       "helm.sh/hook": "pre-install,pre-upgrade"
       "helm.sh/hook-weight": "-30"
       "helm.sh/hook-delete-policy": "before-hook-creation"
   ```

2. `templates/jobs/migration.yaml` — **initContainer `wait-for-secret`** 로 Secret 이 실제로 생길 때까지 최대 5분 대기 (ESO sync 는 async 라 hook 순서만으론 부족).
   ```yaml
   initContainers:
     - name: wait-for-secret
       image: bitnami/kubectl:1.30
       command: ["sh","-c","until kubectl get secret $SECRET -n $NS >/dev/null 2>&1; do sleep 3; done"]
   ```

3. 동일 파일에 **Role + RoleBinding** 추가 — initContainer 가 Secret/ExternalSecret 을 `get/list/watch` 하도록 제한된 RBAC 부여.

순서 보장:
```
pre-install hook:
  -30  ExternalSecret x3       → ESO reconcile → K8s Secret 생성 (async)
  -10  migration SA + Role + RoleBinding
   -5  migration Job
         └─ initContainer(wait-for-secret)  ← Secret 생성 확인
         └─ main container (alembic upgrade head)
일반 resource:
   Deployment, Service, Ingress, HPA, PDB 등
```

고객/신규 계정은 `install-eks.sh` 한 번 실행으로 완료. 수동 migration 불필요.

**만약 여전히 이 에러가 난다면** (환경 특이사항):
- ExternalSecret sync 가 느린 경우 → initContainer timeout 을 300s 이상으로 상향
- ESO Pod 가 Ready 안 된 경우 → terraform apply 단계부터 점검
- Secret 은 생겼는데 key 이름이 안 맞는 경우 → Secrets Manager 의 JSON key 이름 (`password`, `notification_worker_password` 등) 과 chart values 의 `passwordSecretKey` 일치 확인

---

### 증상: migration `permission denied for schema public` — PG 15+ 권한 변경

```
sqlalchemy.exc.ProgrammingError: (asyncpg.exceptions.InsufficientPrivilegeError)
  permission denied for schema public
[SQL: CREATE TABLE alembic_version (...)]
```

**원인**: PostgreSQL 15 부터 `public` schema 에 일반 유저가 기본 CREATE 권한이 없어짐 (CVE-2023-0007 보안 강화). Aurora PostgreSQL 16 동일. `GRANT USAGE ON SCHEMA public` 만으로는 테이블/시퀀스 **읽기/쓰기**만 가능하고 **DDL (CREATE TABLE/INDEX)** 는 불가능.

Alembic 은 첫 실행 시 `alembic_version` 테이블을 생성하므로 CREATE 권한 필수.

**해결**: 마스터 유저로 `gateway` 유저에 `CREATE` 권한 부여:
```bash
kubectl run psql-grant -n llm-gateway --rm -i --restart=Never \
  --image=postgres:16-alpine \
  --env="PGPASSWORD=$AURORA_MASTER_PASSWORD" \
  -- psql -h "$AURORA_HOST" -U postgres_admin -d gateway <<EOF
GRANT CREATE ON SCHEMA public TO gateway;
EOF
```

**근본 수정**: 03-secrets.md 의 2.3 단계 SQL 에 `GRANT CREATE ON SCHEMA public TO gateway;` 추가 (2026-04-23 이후 버전).

**대안**: `public` schema 의 owner 를 `gateway` 로 변경 (`ALTER SCHEMA public OWNER TO gateway;`) — 이러면 모든 권한 자동 부여. 단 `public` 은 특수 schema 이므로 owner 변경이 운영상 리스크 유발 가능. 일반 GRANT 권장.

**왜 신규 계정에서도 재현**: PostgreSQL 15+ 의 기본 동작이므로 모든 신규 배포에서 발생. 03-secrets.md 수정으로 회피.

---

### 증상 4: Lock 남아있음

```
Error: Error locking state: ConditionalCheckFailedException
```

**원인**: 이전 apply가 비정상 종료로 DynamoDB lock이 남음.

**해결**:
```bash
terraform force-unlock <lock-id>
# lock-id 는 에러 메시지에 표시됨
```

---

## Aurora 프로비저닝 지연

### 증상: `terraform apply` 가 Aurora에서 20분 이상 멈춤

**원인**: 정상. Aurora는 기본 15~20분 걸림. **backup window** 설정(17:00-19:00 UTC) 시점과 겹치면 더 길어질 수 있음.

**확인**:
```bash
aws rds describe-db-clusters \
  --db-cluster-identifier "llm-gateway-$ENV" \
  --query 'DBClusters[0].{status:Status,endpoint:Endpoint}' \
  --output json
```

`"status": "creating"` 이면 대기. `"status": "failed"` 면 CloudWatch 로그 확인.

---

## kubectl 인증 실패

### 증상: `error: You must be logged in to the server (Unauthorized)`

**원인 1**: kubeconfig 가 오래된 토큰.

**해결**:
```bash
aws eks update-kubeconfig --region "$AWS_REGION" --name "$CLUSTER_NAME"
```

**원인 2**: IAM 유저가 EKS access entries 에 없음.

**해결**: `terraform.tfvars` 에 추가 후 `terraform apply`:
```hcl
eks_access_entries = {
  me = {
    principal_arn = "arn:aws:iam::ACCOUNT:user/my-name"
    policy_associations = {
      admin = {
        policy_arn = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
        access_scope = { type = "cluster" }
      }
    }
  }
}
```

---

## 이미지 pull 실패

### 증상: Pod 상태 `ImagePullBackOff` 또는 `ErrImagePull`

```bash
kubectl describe pod <pod-name> -n llm-gateway | grep -A3 Events
```

### 원인 1: ECR 리포지토리 없음 or 이미지 미푸시

```bash
aws ecr describe-images \
  --repository-name llm-gateway/gateway-proxy \
  --region "$AWS_REGION" \
  --query 'imageDetails[].imageTags' --output json
```

비어 있으면 [04-helm-install.md의 1.3](./04-helm-install.md#13-이미지-빌드--푸시) 재실행.

### 원인 2: Fargate가 ECR 접근 권한 없음

기본 Fargate Pod execution role 에 `AmazonECRReadOnlyAccess` 가 포함되어 있어야 합니다. Terraform 모듈이 이 역할을 자동 생성하므로 보통 문제 없음. 확인:

```bash
aws eks describe-fargate-profile \
  --cluster-name "$CLUSTER_NAME" \
  --fargate-profile-name application \
  --query 'fargateProfile.podExecutionRoleArn' --output text
```

Role에 연결된 정책:
```bash
aws iam list-attached-role-policies --role-name <role-name>
```

`AmazonEKSFargatePodExecutionRolePolicy` 가 있어야 함 (ECR 접근 포함).

### 원인 3: 이미지 tag 잘못

values 파일의 `image.tag` 와 ECR에 푸시된 태그가 일치하는지 확인:

```bash
grep 'tag:' deployment/charts/llm-gateway/values-eks-fargate-$ENV.yaml
aws ecr describe-images --repository-name llm-gateway/gateway-proxy \
  --query 'imageDetails[].imageTags' --output json
```

---

## ExternalSecret 동기화 실패

### 증상: `kubectl get externalsecret` `READY: False`

```bash
kubectl describe externalsecret llm-gateway-app -n llm-gateway
```

### 원인 1: Secrets Manager 경로에 값 없음

```bash
aws secretsmanager describe-secret --secret-id /llm-gateway/$ENV/app
```

없으면 [03-secrets.md](./03-secrets.md) 재실행.

### 원인 2: JSON 키 이름 불일치

`03-secrets.md` 에서 만든 JSON의 키와 ExternalSecret의 `remoteRef.property` 가 정확히 일치해야 합니다. 예:

```bash
aws secretsmanager get-secret-value \
  --secret-id /llm-gateway/$ENV/app \
  --query SecretString --output text | jq keys
```

출력:
```json
["jwt_jwks_cache_key", "nextauth_secret", "virtual_key_encryption_key"]
```

이 3개 key가 `deployment/charts/llm-gateway/templates/common/secret.yaml` 의 ExternalSecret data 에 언급된 것과 일치해야 합니다.

### 원인 3: ESO IRSA 권한 부족

```bash
kubectl logs -n external-secrets deploy/external-secrets --tail=50
```

`AccessDeniedException` 메시지가 나오면 IRSA Role 정책 확인:
```bash
aws iam list-attached-role-policies \
  --role-name llm-gateway-$ENV-external-secrets
```

`secretsmanager:GetSecretValue` 권한이 있는 정책이 붙어있어야 함. 없으면 `terraform/modules/irsa/main.tf` 의 `external_secrets` 정책 확인.

---

## Fargate Pod Pending 장기화

### 증상: `kubectl get pods` 에서 5분 넘게 `Pending`

```bash
kubectl describe pod <pod-name> -n llm-gateway | tail -20
```

### 원인 1: Fargate Profile selector 불일치

Pod의 namespace/label 이 Fargate Profile 의 selector 와 매칭되지 않음.

Fargate Profile selector 확인:
```bash
aws eks describe-fargate-profile \
  --cluster-name "$CLUSTER_NAME" \
  --fargate-profile-name application
```

selectors 에 `namespace=llm-gateway` 가 있어야 함.

### 원인 2: 리소스 요청이 Fargate 하한 미달

Fargate는 최소 0.25 vCPU / 0.5 GB 독방 단위. `requests.cpu: 50m` 같은 값도 0.25 vCPU로 반올림되지만, Pod spec이 `100m / 128Mi` 같이 너무 작으면 거부될 수 있음.

### 원인 3: Private subnet에 사용 가능 IP 부족

```bash
aws ec2 describe-subnets --subnet-ids $(terraform output -json private_subnet_ids | jq -r '.[]') \
  --query 'Subnets[].{id:SubnetId,available:AvailableIpAddressCount}'
```

available_ip 가 0 이면 subnet CIDR 확장 필요. Terraform `vpc_cidr` + `private_subnet_cidrs` 수정 후 apply. **단, VPC replace 위험** — 별도 계획 필요.

---

## ALB 503 응답

### 증상: `curl http://$ALB_DNS/health` → `503 Service Unavailable`

### 원인 1: Target Group healthy 상태 아님

```bash
# ALB 이름 추출
kubectl get ingress llm-gateway-gateway -n llm-gateway \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}'
# elb.amazonaws.com 앞의 k8s-XXX 가 ALB 이름

# Target Group 상태
TG_ARN=$(aws elbv2 describe-target-groups \
  --query "TargetGroups[?contains(TargetGroupName, 'k8s-XXX')].TargetGroupArn" \
  --output text)

aws elbv2 describe-target-health --target-group-arn "$TG_ARN"
```

**state: unhealthy** 상태 — Pod가 `/health` 에 200 응답 안 함.

**해결**: Pod 로그 확인
```bash
kubectl logs -l app.kubernetes.io/component=gateway-proxy -n llm-gateway --tail=50
```

### 원인 2: Pod target 이 등록 안 됨

```bash
aws elbv2 describe-target-health --target-group-arn "$TG_ARN"
```

`targets` 배열이 비어있음 → ALB Controller가 Pod를 target으로 등록 못함. ALB Controller 로그 확인:

```bash
kubectl logs -n kube-system deploy/aws-load-balancer-controller --tail=100
```

`failed deploying model due to ...` 같은 에러 찾기.

---

## Bedrock 403 AccessDenied

### 증상: `/v1/messages` 요청 → 500 에러, 로그에 `AccessDeniedException`

### 원인 1: IRSA Role에 Bedrock 권한 없음

```bash
# gateway-proxy Pod 의 SA annotation 확인
kubectl get sa gateway-proxy -n llm-gateway -o yaml | grep role-arn
```

Role ARN 이 있어야 함. 없으면 values 파일 또는 install-eks.sh 의 `--set` 인자 재확인.

Role 에 붙은 정책:
```bash
ROLE_NAME="llm-gateway-$ENV-gateway-proxy-bedrock"
aws iam list-attached-role-policies --role-name "$ROLE_NAME"
```

`llm-gateway-$ENV-gateway-proxy-bedrock` 정책이 붙어있어야 함. 내용 확인:
```bash
aws iam get-policy-version \
  --policy-arn "arn:aws:iam::ACCOUNT:policy/llm-gateway-$ENV-gateway-proxy-bedrock" \
  --version-id v1
```

`bedrock:InvokeModel` 이 있고 `resources` 가 실제 호출하려는 모델 ARN 을 포함해야 함.

### 원인 2: 모델이 Bedrock Access 승인 안 됨

```bash
aws bedrock list-foundation-models --region "$AWS_REGION" \
  --query "modelSummaries[?modelId=='anthropic.claude-sonnet-4-20250514-v1:0'].modelLifecycle"
```

`status: ACTIVE` 여야 하고, Bedrock Console의 **Model Access** 에서 "Access granted" 상태여야 함.

### 원인 3: 리전 불일치

Bedrock은 region-scoped. `AWS_REGION` 과 모델 ARN의 region이 일치해야 함. 예: `us-east-1` 에서 `ap-northeast-2` 의 inference profile 호출 불가.

---

## Bedrock `global.*` vs `anthropic.*` ARN 불일치

### 증상: `/v1/messages` 요청 → `AccessDeniedException` — 모델 ID 는 맞는데 403

애플리케이션 로그 예시:
```
AccessDeniedException: User: arn:aws:sts::ACCT:assumed-role/.../gateway-proxy
  is not authorized to perform: bedrock:InvokeModel
  on resource: arn:aws:bedrock:ap-northeast-2:ACCT:inference-profile/global.anthropic.claude-sonnet-4-6
```

**원인**: 애플리케이션은 **cross-region inference profile** 을 호출함 (`global.anthropic.claude-sonnet-4-6`). 이 경우 Bedrock 은 내부적으로 2개 리소스를 사용:

1. `inference-profile/global.anthropic.*` — 라우팅 진입점 (application 이 직접 호출)
2. `foundation-model/anthropic.*` — 실제 추론이 실행되는 region 의 base model

**IAM Policy 에 2개 모두 허용되어야 한다**. Foundation model ARN 만 허용하면 inference-profile 호출 단계에서 403.

### 구분

| 애플리케이션이 쓰는 모델 ID | ARN 종류 | 형식 |
|---|---|---|
| `global.anthropic.claude-sonnet-4-6` | Inference Profile (cross-region) | `arn:aws:bedrock:*::inference-profile/global.anthropic.claude-*` 또는 account-scoped `arn:aws:bedrock:*:ACCT:inference-profile/global.anthropic.claude-*` |
| `apac.anthropic.claude-sonnet-4-20250514-v1:0` | Inference Profile (APAC) | `arn:aws:bedrock:ap-northeast-2::inference-profile/apac.anthropic.claude-*` |
| `anthropic.claude-sonnet-4-6` | Foundation Model | `arn:aws:bedrock:ap-northeast-2::foundation-model/anthropic.claude-sonnet-4-*` |

### 해결

`terraform.tfvars` / `variables.tf` 의 `bedrock_allowed_model_arns` 에 **foundation-model + inference-profile 양쪽** 포함:

```hcl
bedrock_allowed_model_arns = [
  # Foundation models (실제 추론 실행)
  "arn:aws:bedrock:ap-northeast-2::foundation-model/anthropic.claude-opus-4-*",
  "arn:aws:bedrock:ap-northeast-2::foundation-model/anthropic.claude-sonnet-4-*",
  "arn:aws:bedrock:ap-northeast-2::foundation-model/anthropic.claude-haiku-4-*",

  # Global cross-region inference profiles (application 진입점)
  "arn:aws:bedrock:*::inference-profile/global.anthropic.claude-*",
  "arn:aws:bedrock:*:*:inference-profile/global.anthropic.claude-*",

  # APAC cross-region inference profile (예비)
  "arn:aws:bedrock:ap-northeast-2::inference-profile/apac.anthropic.claude-*",
]
```

> account-scoped 와 accountless 두 형태 모두 넣는 이유: system-defined inference profile (`global.*`, `us.*`, `apac.*`) 은 일부 API 경로에서 accountless ARN 으로, 다른 경로에서는 account-scoped ARN 으로 평가됨. 양쪽 허용하면 안전.

**확인**:
```bash
kubectl exec -n llm-gateway deploy/gateway-proxy -- \
  sh -c 'aws bedrock-runtime invoke-model \
    --model-id global.anthropic.claude-haiku-4-5-20251001-v1:0 \
    --body "{\"anthropic_version\":\"bedrock-2023-05-31\",\"max_tokens\":8,\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}" \
    --cli-binary-format raw-in-base64-out /tmp/out.json && cat /tmp/out.json'
```

우리 리포는 2026-04-23 이후 버전에서 양쪽 ARN 허용하도록 기본값 변경 완료.

---

## 이메일 발송 실패 (Internal API / SMTP)

### 증상: notification-worker 로그에 `Could not send email`

**원인 1 (Internal API)**: `EMAIL_API_URL` 이 설정되지 않았거나 고객 내부 메일 API에 접근 불가.

확인:
```bash
kubectl exec deploy/llm-gateway-notification-worker -n llm-gateway -- \
  env | grep -i EMAIL
```

`EMAIL_API_URL` 이 올바른 내부 메일 API 주소인지, 네트워크 접근이 가능한지 확인.

**원인 2 (SMTP)**: SMTP 호스트/포트/인증 정보가 올바르지 않음.

```bash
kubectl exec deploy/llm-gateway-notification-worker -n llm-gateway -- \
  env | grep -i SMTP
```

`SMTP_HOST`, `SMTP_PORT` 값과 SMTP 인증 Secret 이 올바른지 확인.

---

## 일반 진단 명령어

### Pod 전체 상태
```bash
kubectl get pods -n llm-gateway -o wide
```

### 특정 Pod 상세
```bash
kubectl describe pod <pod-name> -n llm-gateway
```

### Pod 로그 (실시간)
```bash
kubectl logs -f <pod-name> -n llm-gateway
```

### 컴포넌트 전체 로그
```bash
kubectl logs -l app.kubernetes.io/component=gateway-proxy -n llm-gateway --tail=100
```

### 이벤트 (최근 발생한 일)
```bash
kubectl get events -n llm-gateway --sort-by='.lastTimestamp' | tail -20
```

### helm 릴리즈 상태
```bash
helm status llm-gateway -n llm-gateway
helm get values llm-gateway -n llm-gateway
helm history llm-gateway -n llm-gateway
```

### 현재 렌더 결과 확인
```bash
helm get manifest llm-gateway -n llm-gateway > /tmp/current.yaml
```

---

## 그 외 이슈

**이 섹션은 실제 배포하면서 추가됩니다.** 막히는 부분 있으면 바로 알려주세요 — 해결 과정을 그대로 이 문서에 박습니다.

---

## HPA 가 `<unknown>/65%` 로 멈춰있음

### 증상

```
$ kubectl -n llm-gateway get hpa
NAME                          TARGETS                         REPLICAS
llm-gateway-gateway-proxy  cpu: <unknown>/65%              3
```

모든 Deployment 가 `minReplicas` 에 고정되어 부하가 와도 scale-out 안 됨.
`kubectl top pod` 도 `Metrics API not available` 반환.

### 원인

EKS Fargate 에선 일반 `metrics-server` 가 동작하지 않음 (kubelet 의 webhook
authz 가 외부 ServiceAccount 를 차단). 본 프로젝트는 `prometheus-adapter` 가
`metrics.k8s.io` 를 대신 서빙하는데, 이 스택이 없으면 HPA 가 입력을 못 받음.

### 해결

1. **Observability 스택 설치 확인**
    ```bash
    kubectl -n observability get pods
    # kps-prometheus-kps-prometheus-0, kps-grafana-*, prometheus-adapter-* 모두 Running
    ```
   없으면 수동 설치:
    ```bash
    cd deployment/observability
    bash kube-prometheus-stack/install.sh
    bash prometheus-adapter/install.sh
    ```
    또는 `install-eks.sh $ENV` 재실행 (idempotent, 기존 자원 건드리지 않음).

2. **APIService 소유권 확인**
    ```bash
    kubectl get apiservice v1beta1.metrics.k8s.io -o jsonpath='{.spec.service.name}'
    # prometheus-adapter 여야 함. metrics-server 라면 충돌 → uninstall 후 재설치
    ```

3. **prometheus-adapter 로그**
    ```bash
    kubectl -n observability logs -l app.kubernetes.io/name=prometheus-adapter --tail=30
    # `unable to fetch metrics` 경고가 연속이면 Prometheus 쿼리 규칙 확인
    ```

4. **collect window 대기**: 설치 직후 약 3 ~ 5 분간은 `<unknown>` 상태 유지 가능
   (Prometheus scrape + prometheus-adapter 의 `window: 3m` 합산). 5 분 후에도
   `<unknown>` 이면 위 1 ~ 3 순서로 진단.

---

[👈 README](./README.md)
