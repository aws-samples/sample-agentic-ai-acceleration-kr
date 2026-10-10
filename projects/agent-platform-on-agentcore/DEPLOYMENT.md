# 배포 가이드

빈 AWS 계정에 이 플랫폼을 올리는 절차입니다. **기반 배포**만으로 로그인, Harness 에이전트,
Registry, Gateway 툴, Knowledge Base, Insights 가 동작합니다. 코드로 만든 Runtime 에이전트,
내장 툴 게이트웨이 같은 나머지는 필요할 때 **추가 배포**로 붙입니다.

**기반 배포** (필수)

| 단계 | 무엇을 | 도구 |
| --- | --- | --- |
| [0](#0-사전-준비) | 도구·권한·모델 접근 확인 | — |
| [1](#1-계정-단위-설정-1회) | Transaction Search | AWS CLI |
| [2](#2-terraform-state-백엔드) | state 버킷·잠금 테이블 | Terraform (`infra/bootstrap`) |
| [3](#3-인프라-1차-apply) | VPC·ALB·ECS·Cognito·DynamoDB·S3·IAM·Lambda 툴 Gateway·Registry·Guardrail | Terraform (`infra/envs/standalone`) |
| [4](#4-이미지-빌드와-서비스-기동) | web/server 이미지 → ECS 기동 | Docker, Terraform |

**추가 배포** (선택, 서로 독립)

| 항목 | 붙는 기능 | 도구 |
| --- | --- | --- |
| [Runtime 에이전트](#runtime-에이전트와-기본-에이전트) | 코드로 만든 Strands 에이전트, **기본 에이전트** | AWS CLI, `deploy.sh` |
| [내장 툴 게이트웨이](#내장-툴-게이트웨이) | Code Interpreter, Browser, Web Search 를 Harness 툴로 | boto3 스크립트 |
| [MCP 서버 연결](#mcp-서버-연결-mcp-apps) | Runtime 에 올린 MCP 서버의 툴, MCP Apps 앱을 채팅 안에 | agentcore CLI, Terraform 변수 |
| [팀과 게이트웨이 정책](#팀과-게이트웨이-정책) | 팀별 실행 역할·허용 모델·툴, Cedar 정책으로 툴 권한 강제 | Terraform 변수 |
| [Entra ID 로그인](#microsoft-entra-id-로그인) | Microsoft 계정 로그인 | Terraform 변수 |
| [데모 사용자](#데모-사용자) | Insights 용 다중 사용자 데이터 | 스크립트 |

기반 배포는 인스톨러 TUI(`./install.sh`)로도 진행할 수 있습니다. 이 문서는 인스톨러가
하는 일을 손으로 하는 절차이기도 합니다. 화면 캡처와 함께 단계별로 따라가는 안내는
[`docs/installer-guide.md`](docs/installer-guide.md) 에 있습니다.

```bash
./install.sh              # 배포 TUI 실행
./install.sh --dry-run    # 실행 없이 조립된 명령만 확인
```

인스톨러는 진행 상태를 저장하지 않고 매번 실제 리소스를 조회합니다. 그래서 중간까지
CLI 로 진행한 환경에서도 이어서 쓸 수 있습니다. 인스톨러는 기반 배포 뒤에 `default`
Runtime 에이전트용 Memory 를 만들고 `agent-runtime/.env` 를 채우는 데까지 해 두므로,
Runtime 에이전트를 붙일 때 Memory 생성 단계를 건너뛸 수 있습니다.

---

# 기반 배포

## 0. 사전 준비

**도구**

| 도구 | 버전 | 용도 |
| --- | --- | --- |
| Terraform | ≥ 1.5 | 인프라 |
| AWS CLI | v2 | 계정 설정 |
| Docker | `linux/amd64` 빌드 가능 | web/server 이미지 |
| Python | 3.13 | 인스톨러, 배포 스크립트 |
| `bedrock-agentcore-starter-toolkit` | 최신 | `agentcore` CLI (Runtime 에이전트를 붙일 때만) |
| `curl` | — | 배포 스크립트의 Registry 등록 |

```bash
pip install bedrock-agentcore-starter-toolkit
aws sts get-caller-identity   # 배포할 계정이 맞는지 확인
```

**권한과 리전**

- 관리자 수준 자격 증명이 필요합니다. IAM 역할, VPC, ECS, Cognito, AgentCore 리소스를
  모두 만듭니다.
- 리전은 `ap-northeast-1`(도쿄)이 기본입니다. 이 플랫폼이 쓰는 AgentCore 기능(Harness,
  Runtime, Memory, Gateway, 내장 툴, Evaluations, Agent Registry, Web Search)이 모두 있는
  아시아 리전입니다. 서울(`ap-northeast-2`)에는 Agent Registry 와 Web Search 가 없습니다.
  다른 리전을 쓰려면 `terraform.tfvars` 의 `region`·`azs`, `backend.tf` 의 `region`, 그리고
  아래 명령의 `--region` 을 같이 바꿉니다.
- Amazon Bedrock 콘솔의 **Model access** 에서 Anthropic Claude 모델(기본값 Sonnet 5.5,
  스레드 오버라이드 선택지 `allowed_models` 의 Opus 5.5·Haiku 5.5)을 켭니다.

**비용**

ALB, NAT 게이트웨이, Fargate 태스크 2개는 사용량과 관계없이 시간당 요금이 붙습니다.
쓰지 않는 동안 줄이는 방법은 [운영](#운영)의 park 를 참고합니다.

---

## 1. 계정 단위 설정 (1회)

### Transaction Search

Insights 의 Trace 드릴다운은 CloudWatch Logs 에서 스팬을 읽습니다. AgentCore 는
Transaction Search 를 켜기 전까지 에이전트·게이트웨이 스팬을 내보내지 않으므로, 이
설정이 없으면 Trace 패널이 계속 비어 있습니다.

CLI 로 켤 때는 X-Ray 가 스팬을 CloudWatch Logs 에 쓸 수 있도록 로그 리소스 정책을 먼저
둡니다. 이 정책 없이 목적지를 바꾸면 `AccessDeniedException: XRay does not have permission to
call PutLogEvents on the aws/spans Log Group` 이 납니다. 콘솔에서 켤 때는 콘솔이 이 정책을 대신
만들어 줍니다.

```bash
REGION=ap-northeast-1
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)

# 1) 로그 리소스 정책
aws logs put-resource-policy --region $REGION --policy-name TransactionSearchXRayAccess \
  --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Sid\":\"TransactionSearchXRayAccess\",
    \"Effect\":\"Allow\",\"Principal\":{\"Service\":\"xray.amazonaws.com\"},\"Action\":\"logs:PutLogEvents\",
    \"Resource\":[\"arn:aws:logs:$REGION:$ACCOUNT:log-group:aws/spans:*\",
                 \"arn:aws:logs:$REGION:$ACCOUNT:log-group:/aws/application-signals/data:*\"],
    \"Condition\":{\"ArnLike\":{\"aws:SourceArn\":\"arn:aws:xray:$REGION:$ACCOUNT:*\"},
                  \"StringEquals\":{\"aws:SourceAccount\":\"$ACCOUNT\"}}}]}"

# 2) 스팬 목적지와 샘플링 비율
aws xray update-trace-segment-destination --destination CloudWatchLogs --region $REGION
aws xray update-indexing-rule --name Default --region $REGION \
  --rule '{"Probabilistic":{"DesiredSamplingPercentage":10}}'

aws xray get-trace-segment-destination --region $REGION   # Status: ACTIVE 확인
```

설정은 계정·리전 단위입니다. 스택을 올릴 리전에서 켜야 하고, 수집된 스팬 수만큼 과금됩니다.
샘플링 비율은 비용에 맞춰 정합니다.

---

## 2. Terraform state 백엔드

state 를 담을 S3 버킷(`<project>-tfstate-<account-id>`)과 잠금 테이블
(`<project>-tflock`)을 만듭니다. 환경마다 한 번만 합니다.

```bash
cd infra/bootstrap
terraform init
terraform apply -var project=bap
```

> `bootstrap` 은 로컬 state 를 씁니다. 같은 디렉터리에서 `-var project=` 값만 바꿔 다시
> apply 하면 Terraform 은 기존 버킷과 테이블을 지우고 새로 만들려고 합니다. 두 번째
> 프로젝트용 백엔드는 workspace 로 분리합니다.
>
> ```bash
> terraform workspace new <project> && terraform apply -var project=<project>
> ```

이어서 환경 디렉터리에 backend 설정을 넣습니다. 버킷 이름에 계정 ID 가 들어가므로 이
파일은 git 에 올라가지 않습니다(`.gitignore`).

```bash
cd ../envs/standalone
cp backend.hcl.example backend.hcl    # <account-id> 를 실제 값으로
terraform init -backend-config=backend.hcl
```

---

## 3. 인프라 1차 apply

### tfvars 작성

```bash
cp terraform.tfvars.example terraform.tfvars
```

필수 값은 Cognito 초기 사용자 두 명입니다. `admin_email` 은 `admin` 그룹(Insights·
Settings·레지스트리 승인 권한), `user_email` 은 일반 사용자로 만들어집니다. 비밀번호는
Cognito 정책상 대문자·소문자·숫자를 포함해 8자 이상이어야 합니다.

```hcl
admin_email    = "admin@example.com"
admin_password = "<choose-a-password>"
user_email     = "user@example.com"
user_password  = "<choose-a-password>"
```

자주 바꾸는 선택 값입니다. 전체 목록과 설명은 `terraform.tfvars.example` 과
`variables.tf` 에 있습니다.

| 변수 | 기본값 | 설명 |
| --- | --- | --- |
| `project` | `bap` | 모든 리소스 이름의 접두사 |
| `region` / `azs` | `ap-northeast-1` / `1a`·`1c` | 배포 리전과 가용 영역 2개 |
| `bedrock_model_id` | `global.anthropic.claude-sonnet-5-5` | 서버가 쓰는 기본 모델 |
| `web_search_backend` | `agentcore` | Gateway 웹 검색: `agentcore`(AgentCore Web Search, API 키 불필요) / `none`. us-east-1·eu-west-1·ap-northeast-1 밖이면 `none` |
| `registry_auto_approval` | `true` | `false` 면 레지스트리 레코드가 DRAFT → 관리자 승인을 거침 |
| `alb_ingress_cidrs` | 전체 공개 | ALB 접근을 사내 대역 등으로 제한 |
| `certificate_arn` + `public_host` | `""` | ALB 에서 TLS 종료. 비우면 HTTP 전용 |
| `alert_email` | `""` | CloudWatch 알람 SNS 구독 주소 |
| `bucket_suffix` | `""` | S3 버킷 이름이 이미 쓰이고 있으면 `-<account-id>` 등을 덧붙임 |
| `enable_agent_registry` | `true` | 조직 SCP 가 Registry API 를 막는 경우 `false` |

> **HTTPS 를 권장합니다.** `certificate_arn` 없이 올리면 ALB 는 평문 HTTP 입니다. 대부분의
> 기능은 동작하지만 Entra ID 로그인은 HTTPS 가 전제이고, 사내망 밖에 노출한다면
> `alb_ingress_cidrs` 로 접근 범위를 좁힙니다.

### apply

ECR 리포지토리를 이 apply 가 만들기 때문에 아직 올릴 이미지가 없습니다. 서비스 태스크를
0 으로 두고 인프라만 먼저 올립니다.

```bash
terraform apply -var desired_count=0
```

> **빈 계정에서는 `cost_allocation_tags` 모듈이 실패할 수 있습니다.** Billing 은 태그
> 키가 청구된 사용량에서 관측된 뒤(최대 24시간)에야 활성화를 받아 줍니다. 이 경우
> `-var activate_cost_allocation_tags=false` 를 붙여 진행하고, 플랫폼을 하루 정도 쓴 뒤
> 기본값으로 다시 apply 합니다. 이 태그가 없는 동안에는 Insights 의 에이전트별 청구
> 비용만 `집계 중` 으로 남습니다.

---

## 4. 이미지 빌드와 서비스 기동

```bash
cd ../../..   # 프로젝트 루트
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION=ap-northeast-1
PROJECT=bap
ECR=$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com

aws ecr get-login-password --region $REGION \
  | docker login --username AWS --password-stdin $ECR

docker build --platform linux/amd64 -t $ECR/$PROJECT/server:latest ./server
docker push $ECR/$PROJECT/server:latest

docker build --platform linux/amd64 -t $ECR/$PROJECT/web:latest ./web
docker push $ECR/$PROJECT/web:latest
```

`--platform linux/amd64` 는 빼면 안 됩니다. Fargate 태스크가 x86_64 로 뜨므로 Apple
Silicon 에서 기본값으로 빌드한 arm64 이미지는 크래시 루프에 빠집니다.

`terraform.tfvars` 에 이미지 URI 를 넣고 다시 apply 합니다.

```hcl
server_image = "<account-id>.dkr.ecr.ap-northeast-1.amazonaws.com/bap/server:latest"
web_image    = "<account-id>.dkr.ecr.ap-northeast-1.amazonaws.com/bap/web:latest"
```

```bash
cd infra/envs/standalone
terraform apply
terraform output alb_url
```

ALB 타깃이 healthy 가 되면(보통 2~3분) `alb_url` 로 접속해 3단계의 계정으로 로그인합니다.
여기까지가 기반 배포입니다.

---

## 기반 배포 확인

| 확인 | 방법 | 기대 결과 |
| --- | --- | --- |
| 웹 | `terraform output alb_url` 접속 | 로그인 화면 |
| 서버 | `curl $(terraform output -raw alb_url)/api/auth/config` | JSON 응답 |
| Harness | Harness 화면에서 에이전트를 만들고 채팅 | 스트리밍 응답, 툴 호출 카드 |
| Registry | Registry 화면 | 방금 만든 Harness 레코드 |
| Insights | admin 으로 로그인 → Insights | 턴 수·토큰·비용, Trace 드릴다운 |

문제가 생기면 먼저 서버 로그를 봅니다.

```bash
aws logs tail /ecs/bap/server --follow --region ap-northeast-1
```

Runtime 에이전트가 없는 동안에는 선택기 맨 위에 고정되는 **기본 에이전트**가 없어 새 채팅은
에이전트를 골라 시작합니다. 기본 에이전트는 `agent_runtime_arn` 이 가리키는 Runtime 의
Registry 레코드이기 때문입니다.

---

# 추가 배포

아래 항목은 서로 독립이라 필요한 것만 골라 붙입니다.

## Runtime 에이전트와 기본 에이전트

`agent-runtime/` 의 Strands 에이전트를 AgentCore Runtime 에 코드로 배포합니다. 이 런타임을
가리키는 Registry 레코드가 **기본 에이전트**가 되어 채팅 선택기 맨 위에 고정되고, 에이전트를
고르지 않은 새 채팅은 여기로 갑니다. 스레드 오버라이드로 이 대화만 다른 모델로 돌릴 수
있습니다.

배포되는 에이전트는 `default` 하나(`bap_default`)입니다. Gateway 툴(AgentCore Web Search,
`fetch_url` 등)과 artifact 툴을 쓰는 단일 Strands 에이전트입니다.

### 1) Memory 생성

런타임은 AgentCore Memory 에서 대화와 세션 요약을 복원합니다. Terraform provider 에 대응
리소스가 없어 CLI 로 만듭니다. 인스톨러를 썼다면 이미 있으므로 건너뜁니다.

```bash
aws bedrock-agentcore-control create-memory \
  --name bap_conversations_default \
  --event-expiry-duration 365 \
  --memory-strategies '[{"summaryMemoryStrategy":{"name":"session_summary","namespaces":["/summaries/{actorId}/{sessionId}"]}}]' \
  --region ap-northeast-1
```

`--event-expiry-duration` 을 빼면 기본값 30일이 적용되어 대화가 조용히 만료됩니다.
`ACTIVE` 가 될 때까지 1~2분 걸립니다.

### 2) 배포

```bash
cd agent-runtime
cp -n .env.example .env

cd ../infra/envs/standalone
export GUARDRAIL_ID=$(terraform output -raw guardrail_id)
export GUARDRAIL_VERSION=$(terraform output -raw guardrail_version)
export EXECUTION_ROLE=$(terraform output -raw agent_runtime_role_arn)   # Gateway 호출 권한이 있는 역할
export PLATFORM_API_URL=$(terraform output -raw alb_url)   # Registry 자동 등록용
export PLATFORM_ADMIN_USERNAME=admin@example.com           # 3단계의 admin 계정
export PLATFORM_ADMIN_PASSWORD='<admin-password>'
cd ../../../agent-runtime

MEMORY_ID=$(aws bedrock-agentcore-control list-memories --region ap-northeast-1 \
  --query "memories[?starts_with(id, 'bap_conversations_default-')].id" --output text)

AGENT_MODULE=default MEMORY_ID=$MEMORY_ID LONG_TERM_RECALL=true ./scripts/deploy.sh
```

`deploy.sh` 한 번이 configure → launch → READY 대기 → 비용 태그 → Registry 등록까지 합니다.
수 분이 걸립니다(컨테이너는 CodeBuild 에서 ARM64 로 빌드됩니다). `MCP_GATEWAY_URL` 은
비워 두면 `terraform output mcp_gateway_url` 에서 채웁니다. 인라인 변수는 `.env` 보다
우선합니다. `EXECUTION_ROLE` 을 비우면 `agentcore configure` 가 역할을 새로 만드는데, 그
역할에는 Gateway 호출 권한이 없어 툴 로딩이 403 으로 실패합니다. 인스톨러를 썼다면 이
값들은 이미 `agent-runtime/.env` 에 들어 있습니다.

`PLATFORM_*` 세 값이 없으면 배포는 그대로 되고 Registry 등록만 건너뜁니다. 그때는
웹의 Registry 화면에서 **Sync deployed** 로 등록합니다.

### 3) 기본 에이전트 연결

`bap_default` 의 ARN 을 tfvars 에 넣고 apply 하면 그 ARN 을 가리키는 레코드가 기본
에이전트로 선택기 맨 위에 고정됩니다. 이미지 재빌드는 필요 없습니다.

```bash
aws bedrock-agentcore-control list-agent-runtimes --region ap-northeast-1 \
  --query "agentRuntimes[?agentRuntimeName=='bap_default'].agentRuntimeArn" --output text
```

```hcl
agent_runtime_arn = "arn:aws:bedrock-agentcore:ap-northeast-1:<account-id>:runtime/bap_default-XXXXXXXXXX"
```

```bash
cd ../infra/envs/standalone && terraform apply
```

스레드 오버라이드에서 고를 수 있는 모델은 `allowed_models` 로 정합니다. Runtime·Harness 를
가리지 않는 전역 상한이고, 아래 팀 설정의 허용 모델은 이 안에서만 좁힙니다. 비워 두면
모델 오버라이드가 숨겨집니다.

### 직접 만든 에이전트 추가

`agent-runtime/agents/` 에 모듈을 추가하고 `AGENT_MODULE=<module> ./scripts/deploy.sh` 로
배포하면 `bap_<module>` 이라는 별도 Runtime 으로 올라갑니다. 세션 요약 회상을 쓰려면
`bap_conversations_<module>` 이름으로 Memory 를 하나 더 만들어 그 ID 를 넘깁니다. 방법은
[`agent-runtime/README.md`](agent-runtime/README.md) 에 있습니다.

### 콜드 스타트 줄이기

Runtime 마이크로VM 을 EventBridge Scheduler 로 5분마다 깨워 첫 응답 지연을 줄입니다.
`agent-runtime/scripts/keepwarm.sh` 의 런타임 ID 를 배포한 값으로 바꾼 뒤 실행합니다.

```bash
./agent-runtime/scripts/keepwarm.sh up
```

## 내장 툴 게이트웨이

AgentCore 내장 툴(Code Interpreter, Browser, Web Search)을 하나의 MCP Gateway 로 묶습니다.
Code Interpreter 와 Browser 는 Gateway 타깃 타입이 없어 Lambda 로 중계하므로 Terraform
대신 멱등한 boto3 스크립트로 올립니다.

```bash
cd infra/builtin_tools_gateway
./deploy.py up        # 생성 또는 갱신
./deploy.py test      # MCP 로 툴 호출 확인
```

출력된 `gateway_arn` 을 Harness 화면에서 툴로 붙이면 harness 가 코드 실행·브라우저를 씁니다.
사용자마다 샌드박스 세션이 분리되는 방식은 [README](infra/builtin_tools_gateway/README.md)
에 있습니다.

## MCP 서버 연결 (MCP Apps)

플랫폼은 [MCP Apps](https://github.com/modelcontextprotocol/ext-apps) 규격을 지원합니다. 툴에
`ui://` 리소스를 붙인 MCP 서버라면 어떤 것이든, 그 툴이 불릴 때 채팅 안에 앱이 렌더링됩니다
(서버가 리소스를 릴레이하고 웹이 샌드박스 iframe 에 띄웁니다). 붙이는 순서는 서버와 관계없이
같습니다.

1. MCP 서버를 AgentCore Runtime 에 올린다 (`-p MCP`).
2. `runtime_mcp_servers` 에 Runtime ARN 을 넣고 apply 해 `bap-gateway` 타깃으로 붙인다. 에이전트와
   Harness 가 툴을 부를 수 있게 된다.
3. Registry 에 MCP 레코드로 등록한다. 화면이 앱 리소스를 찾을 수 있게 된다.

아래는 저장소에 들어 있는 예제 서버 [`mcp-apps-server/`](mcp-apps-server/README.md)(플랫폼
텔레메트리 대시보드) 기준입니다. 붙이지 않아도 플랫폼은 그대로 동작합니다.

**1) Runtime 에 배포**

```bash
cd mcp-apps-server
agentcore configure --entrypoint server.py --name bap_platform_status \
  --requirements-file requirements.txt --region ap-northeast-1 --ecr auto -p MCP
agentcore launch
```

**2) 실행 역할에 CloudWatch 읽기 권한**

역할은 agentcore CLI 가 만들었으므로 인라인 정책으로 붙입니다. 역할 이름은 `agentcore launch`
출력이나 `.bedrock_agentcore.yaml` 의 `execution_role` 에 있습니다.

```bash
aws iam put-role-policy --role-name <execution-role-name> \
  --policy-name PlatformTelemetryRead \
  --policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["cloudwatch:GetMetricData"],"Resource":"*"}]}'
```

**3) Gateway 타깃으로 연결**

Runtime ARN 을 tfvars 에 넣고 apply 하면 `bap-gateway` 에 `platform-status` 타깃(mcpServer,
Gateway 역할 SigV4)이 생깁니다. `bap_default` 와 Harness 는 이 Gateway 를 통해 툴을 부릅니다.

```bash
aws bedrock-agentcore-control list-agent-runtimes --region ap-northeast-1 \
  --query "agentRuntimes[?agentRuntimeName=='bap_platform_status'].agentRuntimeArn" --output text
```

```hcl
runtime_mcp_servers = {
  "platform-status" = "arn:aws:bedrock-agentcore:ap-northeast-1:<account-id>:runtime/bap_platform_status-XXXXXXXXXX"
}
```

```bash
cd infra/envs/standalone && terraform apply
terraform output runtime_mcp_endpoints   # 4단계에서 쓸 URL
```

**4) Registry 에 MCP 레코드로 등록**

웹의 Registry 화면 → 등록 → 유형 **MCP** 로, 원격 URL 에 위 출력의
`platform-status` 값을 넣습니다. 서버는 이 URL 로 앱 리소스(`ui://`)를 읽어 채팅에 띄웁니다.
`Sync deployed` 는 MCP 프로토콜 Runtime 을 에이전트로 등록하지 않으므로 이 단계는 직접 합니다.

채팅에서 "플랫폼 상태 보여줘" 처럼 물으면 `platform-status___get_platform_telemetry` 툴이 불리고
대시보드가 답변 안에 나타납니다.

## 팀과 게이트웨이 정책

조직 안에서 여러 팀이 에이전트를 나눠 쓰려면 팀을 1급 개념으로 둡니다. `teams` 에 팀
이름을 적고 apply 하면 팀마다 Harness 실행 역할과 Cognito 그룹 `team:<name>` 이 생기고,
그 그룹에 든 사용자가 그 팀 소속으로 로그인합니다. 정책 엔진은 `hashicorp/time` provider
를 쓰므로 기존 체크아웃에서는 `terraform init` 을 한 번 다시 합니다.

```hcl
teams                 = ["finance", "hr"]
enable_gateway_policy = true
policy_mode           = "LOG_ONLY"   # 결정만 기록. ENFORCE 로 바꾸면 거부합니다
```

```bash
cd infra/envs/standalone && terraform init && terraform apply
```

apply 뒤에 달라지는 것:

- **팀 설정.** admin 의 Settings → 팀 탭에서 팀별 허용 모델·툴 패턴·비용 경고를 둡니다.
  허용 모델은 `allowed_models` 안에서만 좁혀지고, 비관리자의 Harness 조합에서 요청한
  툴은 팀 목록이 이깁니다.
- **레코드의 팀.** Registry 레코드는 `custom_metadata.team` 으로 팀에 속하고, 목록·검색·
  상세·실행 바인딩에서 admin 은 전부, 그 외는 공유 레코드와 자기 팀 레코드만 봅니다.
  팀을 알 수 없는 레코드는 비관리자에게 숨겨집니다. `teams` 가 비어 있으면 이 규칙은
  꺼지고 기존처럼 전부 보입니다.
- **게이트웨이 정책.** `enable_gateway_policy` 는 Lambda 툴 Gateway 앞에 AgentCore Policy
  (Cedar) 엔진을 붙입니다. 플랫폼 역할(Runtime·기본 Harness·서버)은 모든 툴을, 팀 역할은
  `team_tools` 에 적은 툴과 `team_shared_actions`(기본값은 AgentCore Web Search) 만
  부를 수 있습니다. `ENFORCE` 에서는 허용되지 않은 툴이 `tools/list` 에서 아예 사라집니다.
  정책 목록과 Cedar 형식은 [`infra/modules/gateway_policies/README.md`](infra/modules/gateway_policies/README.md)
  에 있습니다.
- **Insights 팀 뷰.** 턴 원장에 팀이 기록되고 정책 거부 횟수가 팀별로 집계됩니다.

`demo_tools = true` 는 워크숍용 모의 툴(`approve_expense`, `lookup_salary`)을 Gateway 에
싣습니다. 금액 한도가 있는 정책 예제를 보여 주기 위한 것이라 평소에는 꺼 둡니다. 켜고
끌 때는 Gateway 타깃을 `-replace` 해야 합니다(`infra/modules/mcp_gateway/README.md`).

사용자를 팀에 넣는 가장 빠른 방법은 Cognito 콘솔에서 `team:<name>` 그룹에 추가하는
것이고, 아래 데모 사용자 스크립트는 `--teams finance,hr` 로 팀을 나눠 만들어 줍니다.

## Microsoft Entra ID 로그인

Cognito 옆에 두 번째 로그인 공급자로 붙일 수 있습니다. HTTPS 가 전제입니다. 앱 등록과
tfvars 값은 [`infra/README.md` 의 Entra ID 로그인](infra/README.md#entra-id-로그인)을
따릅니다.

## 데모 사용자

Insights 화면을 여러 사용자의 데이터로 확인하려면 Cognito 에 데모 사용자를 만듭니다.

```bash
cd server
python scripts/seed_demo_users.py --password '<choose-a-password>'
# 팀을 만들었다면: 사용자를 팀 그룹에 나눠 넣고 팀마다 <team>-user 계정도 만듭니다
python scripts/seed_demo_users.py --password '<choose-a-password>' --teams finance,hr
```

---

## 운영

**이미지 업데이트.** 같은 태그(`latest`)로 다시 push 하면 Terraform 이 변경을 감지하지
못하므로 서비스를 강제로 재배포합니다. 태그를 바꿔 tfvars 에 넣고 apply 해도 됩니다.

```bash
aws ecs update-service --cluster bap-cluster --service server --force-new-deployment --region ap-northeast-1
aws ecs update-service --cluster bap-cluster --service web    --force-new-deployment --region ap-northeast-1
```

**Runtime 에이전트 업데이트.** 같은 `AGENT_MODULE` 로 `deploy.sh` 를 다시 실행하면 기존
Runtime 이 갱신됩니다.

**유휴 상태로 내리기 (park).** 리소스와 데이터는 남기고 ECS 태스크와 NAT 게이트웨이만
내립니다. ALB 는 DNS 이름을 유지하기 위해 남습니다.

```bash
./infra/scripts/park.sh status
./infra/scripts/park.sh down
./infra/scripts/park.sh up
```

---

## 삭제

Terraform 밖에서 만든 리소스를 먼저 지웁니다. 붙이지 않은 항목은 건너뜁니다.

1. 웹 화면에서 만든 Harness 와 Knowledge Base 를 각 화면에서 삭제합니다.
2. 내장 툴 게이트웨이: `./infra/builtin_tools_gateway/deploy.py down`
3. keep-warm 스케줄: `./agent-runtime/scripts/keepwarm.sh down`
4. 배포한 Runtime(`bap_default`, 붙였다면 `bap_platform_status` 같은 MCP 서버)과
   `bap_conversations_*` Memory 를 AgentCore 콘솔이나 CLI 로 삭제합니다.
5. Insights 가 Runtime 사용량 로그를 모으기 위해 만든 CloudWatch Logs delivery 를 지웁니다.
   서버가 떠 있는 동안 리전의 Runtime 마다 `<project>-usage-<runtime>` 이름으로 자동 생성되며,
   Terraform 밖의 리소스라 남아 있으면 destroy 가 `DeleteDeliveryDestination` 400 오류로 멈춥니다.

   ```bash
   R=ap-northeast-1; P=bap
   for id in $(aws logs describe-deliveries --region $R \
       --query "deliveries[?starts_with(deliverySourceName, '$P-usage-')].id" --output text); do
     aws logs delete-delivery --region $R --id "$id"; done
   for n in $(aws logs describe-delivery-sources --region $R \
       --query "deliverySources[?starts_with(name, '$P-usage-')].name" --output text); do
     aws logs delete-delivery-source --region $R --name "$n"; done
   ```

그다음 스택을 지웁니다. DynamoDB 테이블은 삭제 보호가 켜져 있어 먼저 풉니다. 데이터가
필요하면 `./infra/scripts/backup.sh` 로 먼저 백업합니다.

```bash
cd infra/envs/standalone
terraform apply -var allow_destroy=true   # 삭제 보호 해제
terraform destroy
```

state 백엔드까지 지우려면 마지막으로 `infra/bootstrap` 에서 `terraform destroy` 를
실행합니다. 버킷에 state 버전이 남아 있으면 먼저 비웁니다.

---

## 더 보기

- [`infra/README.md`](infra/README.md): 모듈 목록, Cost Allocation Tags,
  백업·복원
- [`agent-runtime/README.md`](agent-runtime/README.md): 에이전트 구조, 새 에이전트 추가,
  환경 변수 전체 목록
- [`infra/builtin_tools_gateway/README.md`](infra/builtin_tools_gateway/README.md): 내장
  툴 게이트웨이와 사용자별 세션 분리
