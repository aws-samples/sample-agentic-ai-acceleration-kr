# 다른 AWS 계정에 배포하기 (Multi-Account Portability)

이 terraform 은 **계정 비종속**으로 작성돼 있습니다. account ID·region·도메인 등
계정 고유 값은 전부 변수(`terraform.tfvars`) + backend partial config 로 주입하고,
전역 unique 가 필요한 리소스명(Cognito 도메인)은 `account_id` 로 자동 생성합니다.

신규 계정에 처음 심을 때 순서:

## 1. 자격증명 / 사전 요건
```bash
aws sts get-caller-identity            # 대상 계정으로 로그인됐는지 확인
# Bedrock Model Access: 콘솔에서 Claude (opus/sonnet/haiku) 승인받아 둘 것
```

## 2. tfstate 백엔드 부트스트랩 (계정당 1회)
state 버킷 + 락 테이블을 먼저 만든다(이 리소스는 terraform 밖에서 생성).
```bash
cd deployment
AWS_REGION=<your-region> \
TFSTATE_BUCKET=llm-gateway-tfstate-<account_id> \
TFLOCK_TABLE=llm-gateway-tflock \
  ./scripts/bootstrap-tfstate.sh
```

## 3. tfvars 작성 (계정 고유 값)
```bash
cd terraform/environments/llm-gateway-dev    # 또는 .../llm-gateway-prod
cp terraform.tfvars.example terraform.tfvars
# terraform.tfvars 편집 — 채워야 할 항목:
#   aws_region                : 대상 region
#   eks_access_entries        : 본인 IAM 유저/역할 ARN (arn:aws:iam::<account_id>:...)
#   cognito_groups            : 팀/부서 그룹 (Claude_<team> / Claude_<dept>_<team> / ClaudeAdmin)
#   bedrock_allowed_model_arns: region 에 맞게 (예시는 ap-northeast-2)
#   tags                      : 비용/소유 태그
#   (cognito_domain_suffix    : 비워두면 vanilla-auth-<account_id> 자동 생성 — 보통 그대로 둠)
#   (vpc_cidr 등              : 기존 VPC 와 겹치면 조정)
```

## 4. init (backend 주입) + plan + apply
```bash
terraform init \
  -backend-config="bucket=llm-gateway-tfstate-<account_id>" \
  -backend-config="dynamodb_table=llm-gateway-tflock"
terraform plan -out tf.plan        # ⚠️ 반드시 plan 으로 add/change/destroy 확인
terraform apply tf.plan
```

## 5. chat-agent (BI 분석 에이전트) 포함 여부 — ⚠️ 함정
`admin-chat-agent` 인프라(ECR/S3/IAM/Lambda)는 기본 **비활성**입니다:
```hcl
enable_chat_agent    = false   # ECR + S3 staging + IAM + KMS
enable_chat_db_tools = false   # query_db/get_schema Lambda + reader secret
```
- BI 챗을 쓸 계정이면 tfvars 에 `enable_chat_agent = true`(+ DB 도구 쓰면
  `enable_chat_db_tools = true`)를 **명시**해야 한다.
- **이미 chat-agent 가 배포된 환경에서 이 값을 false 로 두고 apply 하면 운영 중인
  chat-agent 리소스(ECR/S3/Lambda 등 ~24개)를 전부 파괴**한다. plan 에
  `N to destroy` 가 보이면 멈추고 이 플래그부터 확인할 것. (AgentCore Runtime 자체는
  terraform 이 아니라 `/tmp/deploy_agent.sh` 로 별도 배포 — §32.4 참조.)

## 무엇이 계정 비종속인가 (확인용)
- account_id: `data.aws_caller_identity` 동적 조회 — 하드코딩 없음.
- region: `var.aws_region` + backend `-backend-config` 주입.
- Cognito 도메인: `cognito_domain_suffix` 빈 값이면 `vanilla-auth-<account_id>` 자동
  (전 세계 unique 보장 — main.tf locals).
- IAM/ARN/태그: 전부 tfvars. backend: partial config(계정별 bucket/table).
- 모듈(vpc/eks/aurora/elasticache/cognito/agentcore/irsa/external-secrets/alb): 재사용,
  `var.environment`(dev/prod)로 HA·비용 프로파일 자동 선택.
