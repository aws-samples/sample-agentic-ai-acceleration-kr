# 01. Prerequisites — AWS 계정 · 도구 · 권한 준비

**목적**: Terraform apply 가능한 상태까지 확인.
**소요**: 15분 (도구가 이미 설치돼 있다면 5분)

---

## 1. AWS 계정 확인

### 1.1 인증 확인

```bash
aws sts get-caller-identity
```

✅ 성공하면 아래처럼 출력됩니다:

```json
{
    "UserId": "XXXXXXXXXXXXXXXXXXXX",
    "Account": "123456789012",
    "Arn": "arn:aws:iam::123456789012:user/your-name"
}
```

🐛 실패 시 — `Unable to locate credentials`:
```bash
# 옵션 A: access key 직접 설정
aws configure

# 옵션 B: AWS SSO 로그인 (조직 계정)
aws sso login --profile your-profile
export AWS_PROFILE=your-profile
```

### 1.2 IAM 권한 요구사항

이 가이드 실행에 필요한 권한:

- `AdministratorAccess` (가장 간단, dev 환경 권장)

또는 아래 서비스별 최소 권한 조합:

- `AmazonVPCFullAccess`
- `AmazonEKSClusterPolicy` + `AmazonEKSServicePolicy`
- `AmazonRDSFullAccess` (Aurora용)
- `AmazonElastiCacheFullAccess`
- `AmazonSESFullAccess`
- `SecretsManagerFullAccess`
- `IAMFullAccess` (IRSA Role 생성)
- `AmazonECRFullAccess` (이미지 업로드)
- `AmazonS3FullAccess` + `AmazonDynamoDBFullAccess` (tfstate 백엔드)

### 1.3 예산 확인 (월 비용 예상)

아래 숫자는 ap-northeast-2 기준 **최소(base) 고정비**. 실제 운영비는 트래픽에 따라 §1.4 의 스케일링 리소스로 증가합니다.

| 리소스 | dev | prod | 비고 |
|-------|-----|------|-----|
| VPC + NAT Gateway | $32 (1 NAT) | $96 (3 NAT) | NAT 데이터 처리비 별도 ($0.045/GB) |
| EKS Control Plane | $73 | $73 | 고정 |
| Fargate (최소 replica) | $50 (6 Pod min) | $250 (14 Pod min, HPA 3→30 확장) | HPA 상한 도달 시 prod 최대 ~$1,500 |
| Aurora PostgreSQL | $44 (Serverless v2 0.5 ACU) | $2,189 (r7g.2xlarge × 2) | dev ACU 최대 4.0 시 ~$350 / storage·PI·backup 별도. prod 는 10k 조직 + 4k SSE 부하 충족 위해 r7g.large → r7g.2xlarge 업그레이드 (max_connections 1706 → 5000) |
| ElastiCache Valkey | $25 (t4g.small × 1) | $994 (r7g.large × 6: 3 shards × 2) | 고정 |
| RDS Proxy | $22 (dev 기본 on) | $22 (prod 기본 on) | Aurora 인스턴스 vCPU 에 비례 |
| ALB × 3 | $60 | $60 | LCU 추가 과금 (RPS/연결/대역폭) |
| CloudWatch Logs | $10 | $30 | Flow Log + EKS/ALB/Aurora 로그. 트래픽 비례 증가 |
| Secrets Manager | $2 | $2 | 고정 |
| **합계 (최소)** | **~$318/월** | **~$3,716/월** | Bedrock 토큰 과금 **불포함** |

⚠️ **prod 비용 주의**: 상시 가동 시 월 ~$2,100 (Bedrock 별도). 부하 테스트 목적이면 테스트 후 `terraform destroy` 권장. Aurora + ElastiCache + Fargate(min) 가 base 의 약 85%.

💡 **RDS Proxy**: Aurora 앞단 connection pool. **dev/prod 모두 기본 활성**(`enable_rds_proxy = true`, `variables.tf`). 애플리케이션은 `aurora` 모듈 output `application_endpoint` 를 쓰면 활성/비활성 여부에 관계없이 올바른 호스트로 연결됩니다. 꼭 비용을 낮춰야 하면 `-var enable_rds_proxy=false` 로 내릴 수 있으나, asyncpg `statementCacheSize` 를 100 으로 복구해야 합니다 (자세한 내용은 [02-terraform-apply.md §3.4](./02-terraform-apply.md#34-rds-proxy-토글) 참조).

### 1.4 트래픽 증가 시 비용이 늘어나는 리소스

위 표는 base 값일 뿐, 아래 리소스는 사용량에 비례해 증가합니다:

| 리소스 | 증가 트리거 | 영향도 | 상한 |
|-------|-----------|-------|------|
| **Bedrock 토큰** | 요청량·모델 선택·cache hit rate | 🔴 | 없음 (인프라비보다 클 수 있음) |
| **Fargate (Pod 수)** | HPA CPU 65% 기준 스케일. prod 풀 스케일 시 gateway-proxy 30 / admin-api 10 / admin-ui 6 / noti 6 / cost-rec 6 | 🔴 | prod 최대 ~$1,200~1,500 |
| **Aurora (dev Serverless v2)** | 부하 비례 0.5 → 4.0 ACU 자동 스케일 | 🟠 | ~$350/월 |
| **NAT Gateway 데이터 처리** | Bedrock / 외부 API 호출량 | 🟠 | $0.045/GB (첫 10TB) |
| **CloudWatch Logs ingest/store** | 로그 양. prod Flow Log 90 일 retention | 🟠 | Ingest $0.50/GB |
| **ALB LCU** | RPS·active connection·new connection·data processed 중 max. SSE 장수명 연결 주의 | 🟡 | LCU 당 $0.008/h |
| **Data Transfer out (인터넷)** | admin-ui 응답·download 트래픽 | 🟡 | $0.126/GB (100GB 이후) |
| **Aurora 스토리지·백업** | 요청 로그 누적 → `usage_logs` 증가. backup retention prod 14일 | 🟡 | $0.10/GB·월 |
| **ElastiCache 샤드** | 수동 (`num_node_groups` 증가) | 🟢 | 수동 설정 |

📌 **변동비 차단 방법**:
- Bedrock: FR-4 Rate Limit / Budget 으로 팀·유저별 CPM/CPH 상한 강제.
- Fargate: HPA `maxReplicas` 를 `values-eks-fargate-prod.yaml` 에서 축소.
- NAT 데이터 처리: Bedrock 을 VPC Endpoint 로 경유 시키면 NAT 경유 트래픽 감소 (향후 최적화 포인트).
- CloudWatch: `flow_log_cloudwatch_log_group_retention_in_days` / 애플리케이션 로그 레벨 조정.

---

## 2. 로컬 도구 설치

### 2.1 필수 도구

다음 도구가 필요합니다:

```bash
aws --version        # aws-cli/2.x
terraform version    # Terraform v1.9.0+
kubectl version --client  # Client Version: v1.29+
helm version         # v3.14+
docker --version     # Docker version 24+ (또는 finch)
jq --version         # jq-1.6+
```

### 2.2 설치 (macOS)

```bash
# AWS CLI
brew install awscli

# Terraform (HashiCorp tap 필수)
brew tap hashicorp/tap
brew install hashicorp/tap/terraform

# kubectl
brew install kubectl

# Helm
brew install helm

# Docker (또는 Finch - AWS 공식 오픈소스 대안)
brew install --cask docker    # Docker Desktop
# or
brew install finch

# jq
brew install jq
```

### 2.3 설치 (Linux)

```bash
# AWS CLI v2
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"
unzip awscliv2.zip && sudo ./aws/install

# Terraform
wget https://releases.hashicorp.com/terraform/1.9.8/terraform_1.9.8_linux_amd64.zip
unzip terraform_1.9.8_linux_amd64.zip && sudo mv terraform /usr/local/bin/

# kubectl
curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
sudo install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl

# Helm
curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash

# jq
sudo apt install jq  # Debian/Ubuntu
sudo yum install jq  # RHEL/Amazon Linux
```

---

## 3. 리포지토리 클론 및 환경 변수

### 3.1 클론

```bash
git clone <repo-url> LLM-Gateway-Vanilla
cd LLM-Gateway-Vanilla
```

### 3.2 환경 변수

```bash
export ENV=dev                         # dev | prod
export AWS_REGION=ap-northeast-2
export AWS_PROFILE=claude-proxy-dev    # 본인 profile 이름
```

✅ 아래 명령이 성공해야 함:
```bash
aws sts get-caller-identity --region "$AWS_REGION" --profile "$AWS_PROFILE"
```

---

## 4. Bedrock 모델 Access 승인

AWS Bedrock은 모델별로 **명시적 승인** 이 필요합니다.

### 4.1 Console 에서 승인

1. https://console.aws.amazon.com/bedrock/ 접속
2. 좌측 메뉴 **Model access** 클릭
3. **Manage model access** 클릭
4. 사용할 모델 체크 (최소):
   - ✅ Claude Sonnet 4
   - ✅ Claude Haiku 4
   - ✅ (prod) Claude Opus 4
5. **Request model access** 클릭
6. 1~2분 대기 후 **Access granted** 확인

🐛 "Access denied" 또는 "Not available in region" 에러 시 — 다른 region 시도

### 4.2 CLI 로 확인

```bash
aws bedrock list-foundation-models --region "$AWS_REGION" \
  --query 'modelSummaries[?contains(modelId, `claude`)].{id:modelId,status:modelLifecycle.status}' \
  --output table
```

✅ `ACTIVE` 상태 모델이 보여야 함.

---

## 5. 도메인 / Route53 (선택)

**HTTPS 사용 시**: 도메인이 필요합니다.

```bash
# 가진 Hosted Zone 확인
aws route53 list-hosted-zones --query 'HostedZones[*].{Name:Name,Id:Id}' --output table
```

Hosted Zone이 없으면:
- Route53에서 도메인 등록 + Hosted Zone 생성, 또는
- 외부 DNS(예: GoDaddy) 사용 → 레코드 수동 생성 (terraform outputs의 ALB 주소를 CNAME으로)

🔧 dev 환경에선 domain 없이도 배포 가능 (ALB 자동 생성된 `*.elb.amazonaws.com` 주소로 접근).

---

## 6. 체크리스트

다음으로 넘어가기 전 모두 체크되어야 합니다:

- [ ] `aws sts get-caller-identity` 성공
- [ ] `terraform version` v1.9+
- [ ] `helm version` v3.14+
- [ ] `kubectl version --client` v1.29+
- [ ] `jq --version` v1.6+
- [ ] Bedrock `list-foundation-models` 에서 Claude 모델 ACTIVE 확인
- [ ] (선택) Route53 Hosted Zone 확인

---

[👈 README](./README.md) | [다음: 02-terraform-apply.md 👉](./02-terraform-apply.md)
