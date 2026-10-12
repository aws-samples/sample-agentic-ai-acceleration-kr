# agent-platform 인프라

> 이 절차를 자동화한 TUI가 있습니다: 저장소 루트에서 `./install.sh`
> (`--dry-run`으로 실행 없이 확인).
> 아래 문서는 인스톨러가 무엇을 하는지에 대한 정본이며, 인스톨러 없이 진행할 때도 씁니다.

## 환경

Terraform 환경은 `envs/standalone` 하나다. 전체 스택을 `project` 접두사(기본 `bap`)로 올린다.
한 계정에 스택을 하나 더 올리려면 `project` 를 다른 값으로 주고 state 백엔드도 따로 만든다
(아래 1번의 경고 참고).

AgentCore 런타임은 이 Terraform이 만들지 않는다. 인프라만 여기서 관리하고,
런타임은 `agent-runtime/scripts/deploy.sh`(에이전트)와 `agentcore launch`
(`mcp-apps-server`)가 `bap_*` 이름으로 배포한다.

MCP 게이트웨이 authorizer는 **`AWS_IAM`**이다. 런타임·harness는 실행 역할 SigV4로
게이트웨이를 호출하며, `deploy.sh`가 실행 역할에
`InvokeGateway`를 붙인다. authorizer는 **생성 후 변경 불가**라서 값을 바꾸면
게이트웨이 리소스가 replace된다(in-place update 아님, cutover).

## 모듈

| 모듈 | 올리는 것 |
| --- | --- |
| `network` | VPC, 퍼블릭/프라이빗 서브넷, NAT 게이트웨이+EIP. `hibernate=true` 면 NAT·EIP·프라이빗 기본 경로를 지운다(아래 park/unpark) |
| `ecs` | Fargate 클러스터, web/server 서비스·태스크 정의, ALB(:80/:8081, 인증서가 있으면 :443/:8443), Cloud Map 내부 DNS |
| `ecr` | web/server 이미지 리포지토리 |
| `cognito` | User Pool·앱 클라이언트·`admin`/`user` 그룹과 초기 사용자, Hosted UI 도메인(선택) |
| `dynamodb` | `threads`·`artifacts`·`knowledge-bases`·`usage`·`prefs`·`users`. 전부 삭제 보호+PITR(`allow_destroy` 로 해제) |
| `s3_artifacts` / `s3_knowledge` / `s3_skills` | 산출물·지식 베이스 원본·스킬 번들 버킷 |
| `iam` | ECS 태스크 역할: 테이블·버킷, AgentCore, CloudWatch, Cost Explorer, Pricing, Cognito ListUsers |
| `agent_runtime_role` / `harness_role` / `knowledge_roles` | 런타임 실행 역할, harness 실행 역할, KB 서비스·게이트웨이 역할 |
| `mcp_gateway` | 툴 게이트웨이: AgentCore Web Search 커넥터(`web_search_backend`, 기본 `agentcore`) + Lambda 툴(`fetch_url`·`current_time`·`calculate`·artifact), `AWS_IAM` authorizer |
| `agent_registry` | AgentCore Agent Registry(boto3 스크립트). `auto_approval` 은 별도 `null_resource` 가 `UpdateRegistry` 로 맞추므로 값을 바꿔도 레지스트리가 재생성되지 않는다(재생성되면 레코드가 전부 지워진다) |
| `bedrock_guardrail` | 런타임이 `GUARDRAIL_ID` 로 부착하는 Guardrail |
| `alarms` | ALB 5xx·web healthy host·usage 테이블 쓰기 스로틀 알람 + SNS(`alert_email` 구독) |
| `observability` | AgentCore Runtime vended USAGE_LOGS 목적지(세션별 런타임 비용) |
| `cost_allocation_tags` | `Platform`·`AgentName` 청구 태그 활성화(계정 전역, 아래 참고) |

내장 툴 게이트웨이(웹검색·code interpreter·browser)는 Terraform 밖의 boto3 스크립트
[`builtin_tools_gateway/`](builtin_tools_gateway/README.md)가 올린다.

## 계정 단위 선행 작업

### Transaction Search (계정별 1회)

Trace 드릴다운은 CloudWatch Logs에서 스팬을 읽는다. AgentCore는 CloudWatch
Transaction Search를 켜기 전까지 에이전트·게이트웨이 스팬을 아예 내보내지
않으므로 — 기본으로 스팬이 나오는 건 Memory뿐이다 — 이 작업 없으면 trace
패널이 영구히 비어 있고, API는 그 이유를 설명하는 오류조차 내지 않는다.

CLI로 켤 때는 X-Ray가 `aws/spans` 로그 그룹에 쓸 수 있게 하는 로그 리소스 정책이
먼저 필요하다(없으면 `AccessDeniedException … PutLogEvents on the aws/spans Log
Group`). 콘솔은 이 정책을 대신 만든다. 정책 본문을 포함한 전체 명령은
[`DEPLOYMENT.md`](../DEPLOYMENT.md#transaction-search)에 있다.

```bash
aws logs put-resource-policy --policy-name TransactionSearchXRayAccess --policy-document '<DEPLOYMENT.md 참고>' --region ap-northeast-1
aws xray update-trace-segment-destination --destination CloudWatchLogs --region ap-northeast-1
aws xray update-indexing-rule --name Default --region ap-northeast-1 \
  --rule '{"Probabilistic":{"DesiredSamplingPercentage":10}}'
```

현재 상태는 아래 명령으로 확인하고 설정 후에도 다시 실행해 확인한다:

```bash
aws xray get-trace-segment-destination --region ap-northeast-1
aws xray get-indexing-rules --region ap-northeast-1
```

**이 기능은 비용이 발생한다.** Transaction Search는 수집된 스팬 수만큼
과금되므로, 샘플링 비율은 기본값이 아니라 비용 의사결정이다. 10%면 바쁜
배포에서 "이 턴은 왜 느렸나"를 답하기에 충분하고, 특정 문제를 조사할 때
올렸다가 조사 후 내린다. 계정·리전 단위로 걸리므로 같은 리전의 모든 환경에
영향을 준다.

### Cost Allocation Tags (계정별 1회)

에이전트별 청구 비용은 `Platform`·`AgentName` 두 키가 Billing 에서 `Active` 여야
나온다. `standalone` 이 `modules/cost_allocation_tags` 로 이 상태를 소유하므로
apply 하면 켜진다. `terraform output active_cost_allocation_tags` 로 읽는다.

상태는 **계정 전역**이다. 리전별도 스택별도 아니라서, 한 계정에서 두 state 가
같은 키를 선언하면 하나의 리소스를 두고 다툰다. 한 계정에 스택을 둘 올린다면
둘째 스택은 `activate_cost_allocation_tags = false` 로 둔다.

**빈 계정의 첫 apply 는 이 모듈에서 실패한다.** 청구 태그 레지스트리는 키가
*청구된 사용량*에 관측된 뒤에야 그 키를 등록하고, 이게 최대 24시간 늦다. 그
전에는 `UpdateCostAllocationTagsStatus` 가 `ValidationException: Tag keys not
found` 로 거부한다. 첫 apply 는 `-var activate_cost_allocation_tags=false` 로
넘기고, harness 를 하나 만들어 써 본 다음 `server/` 에서

```bash
python -m scripts.activate_cost_tags
```

로 먼저 켠다(활성화는 소급되지 않으므로 다음 apply 를 기다리면 그만큼 귀속을
잃는다). 이후 변수를 다시 `true` 로 두면 Terraform 이 그 상태를 그대로 유지한다
— 이미 Active 면 plan 에 아무것도 안 뜬다. 활성 태그가 없으면 에이전트별 청구
비용 카드가 `집계 중`으로 남고, 나머지 페이지와 카드는 영향이 없다.

태그를 붙이는 쪽은 서버다: harness 를 만들 때 `Platform`/`AgentName` 이
들어가고 관리형 Runtime·endpoint·Memory 로 전파된다. `TagResource` 는 이미
존재하는 harness 와 동반 런타임에도 먹히지만, 태그가 붙기 전에
청구된 시간은 소급되지 않는다.

## 배포 순서

1. State 백엔드 생성 (환경별 1회):
   ```bash
   cd infra/bootstrap
   terraform init
   terraform apply -var project=bap
   ```
   출력된 `state_bucket`, `lock_table` 값을 확인.

   > **경고 — `bootstrap`은 로컬 state를 쓰고, 그 state는 프로젝트마다 분리되지 않는다.**
   > `-var project=` 값을 바꿔 다시 apply하면 Terraform은 "이름이 바뀌었다"고 판단해
   > **기존 프로젝트의 버킷과 잠금 테이블을 지우고 새로 만든다.** 버킷은 비어 있지 않으면
   > 삭제가 실패하지만, 잠금 테이블은 지워지고 버킷의 버저닝·public access block 설정이
   > 풀릴 수 있다.
   >
   > 두 번째 프로젝트의 백엔드를 만들려면 state를 섞지 말고 분리해서 실행한다:
   > ```bash
   > terraform init -reconfigure -backend-config="path=terraform-<project>.tfstate"
   > # 또는 workspace 로 분리
   > terraform workspace new <project> && terraform apply -var project=<project>
   > ```
   > 이미 섞였다면 `terraform state rm` → `terraform import`로 원래 리소스를 되돌린 뒤
   > `terraform plan`이 `No changes`가 되는지 확인한다.

2. Backend 설정. 버킷 이름에 계정 ID가 들어가므로 저장소에 두지 않고 파일로 넘긴다.
   ```bash
   cd infra/envs/standalone
   cp backend.hcl.example backend.hcl   # 계정 ID 채우기
   terraform init -backend-config=backend.hcl
   ```

3. 환경 배포. ECR 리포지토리를 이 apply가 만들기 때문에 첫 실행에는 참조할 이미지가 없다.
   `server_image` / `web_image`는 공용 placeholder로 시작하므로 태스크를 0으로 두고 인프라만 올린다:
   ```bash
   cp terraform.tfvars.example terraform.tfvars   # 값 채우기 (비밀번호 등)
   terraform apply -var desired_count=0
   ```

4. 이미지 빌드 & push (아래 참고) 후 `terraform.tfvars`에 이미지 URI를 넣고 `terraform apply`.
   서비스가 뜨고 ALB 타깃이 healthy가 되면 `terraform output alb_url`로 접속한다.

## 이미지 빌드 & 배포

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION=ap-northeast-1
ECR=$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com
PROJECT=bap

aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $ECR

# server
docker build --platform linux/amd64 -t $ECR/$PROJECT/server:latest ./server
docker push $ECR/$PROJECT/server:latest

# web
docker build --platform linux/amd64 -t $ECR/$PROJECT/web:latest ./web
docker push $ECR/$PROJECT/web:latest
```

`--platform linux/amd64`는 생략하면 안 된다. ECS 태스크 정의에 `runtimePlatform`이
없어 Fargate가 x86_64로 띄우므로, Apple Silicon에서 그냥 빌드한 arm64 이미지는
CloudWatch 로그만 남기고 크래시 루프에 빠진다. 인스톨러는 이 플래그를 항상 붙인다.

이후 `terraform.tfvars`에 `server_image` / `web_image`를 위 URI로 설정하고 `terraform apply`.
같은 태그로 다시 push했다면 Terraform이 변경을 감지하지 못하므로 강제 재배포:

```bash
aws ecs update-service --cluster $PROJECT-cluster --service server --force-new-deployment --region ap-northeast-1
aws ecs update-service --cluster $PROJECT-cluster --service web   --force-new-deployment --region ap-northeast-1
```

ECS 서비스는 **family 의 최신 리비전**을 따른다(`modules/ecs`: `data.aws_ecs_task_definition`
의 리비전과 Terraform 이 등록한 리비전 중 `max()`). tfvars 의 이미지 태그를 바꿔 apply 하면
새 리비전이 등록되고 서비스가 그쪽으로 옮겨가며, 핫픽스로 콘솔·CLI 에서 더 높은 리비전을
등록해 서비스에 올려 두었다면 다음 apply 가 그것을 되돌리지 않는다(그 리비전이 더 높으므로).
`desired_count` 는 `park.sh` 가 `-var desired_count=0` 으로 움직이는 평범한 변수다.

## Entra ID 로그인

기본 로그인은 Cognito 비밀번호 폼입니다. tfvars 에 아래를 채우면 로그인 화면에
"Microsoft 계정 (Entra ID)" 버튼이 함께 나타나고, 둘 중 어느 쪽으로든 로그인할 수
있습니다. Cognito 만 쓰려면 아무것도 하지 않으면 됩니다.

전제: ALB 가 HTTPS(`certificate_arn` + `public_host`)여야 합니다. Entra 는
`http://localhost` 를 제외한 http 콜백을 등록해 주지 않습니다.

1. **Entra 앱 등록** (테넌트 관리자). Azure Portal → App registrations → New
   registration, 단일 테넌트.
   - *Authentication → Add a platform → **Single-page application***.
     Redirect URI 는 `https://<public_host>/auth/callback`. 로컬 개발도 할 거면
     `http://localhost:3000/auth/callback` 을 추가합니다. Web 플랫폼이 아니라 SPA
     여야 token 엔드포인트가 브라우저 CORS 를 허용합니다.
   - *Token configuration → Add groups claim* 에서 **ID** 토큰에 groups 를 넣습니다.
     그룹이 200 개를 넘으면 "Groups assigned to the application" 으로 좁힙니다.
     기본은 그룹 object id 가 내려오니, 이름으로 판정하려면 "Emit group name" 류
     옵션(보안 그룹은 sAMAccountName/cloud display name)을 고르거나 아래 변수에
     object id 를 그대로 적습니다.
   - Overview 의 **Application (client) ID** 와 **Directory (tenant) ID** 를 적어 둡니다.
     client secret 은 만들지 않습니다(PKCE).
2. **tfvars**:
   ```hcl
   oidc_issuer_url   = "https://login.microsoftonline.com/<tenant-id>/v2.0"  # 반드시 v2.0
   oidc_client_id    = "<application-client-id>"
   oidc_admin_groups = ["PlatformAdmin"]            # 관리자 그룹(이름 또는 object id)
   # oidc_required_group = "PlatformUsers"          # 비우면 테넌트 전원 로그인 가능
   # oidc_redirect_uris  = ["https://<public_host>/auth/callback", "http://localhost:3000/auth/callback"]
   ```
   `oidc_redirect_uris` 를 비우면 `https://<public_host>/auth/callback` 하나로
   계산됩니다. 서버는 `/api/auth/config` 에서 요청 Origin 과 같은 호스트의 URI 를
   골라 내려주므로 운영과 로컬이 하나의 앱 등록을 공유합니다.
3. `terraform apply`. 서버 태스크에 `OIDC_PROVIDERS_JSON` 과 `USERS_TABLE`
   (`<project>-users`) 이 들어가고 IAM 에 그 테이블 권한이 붙습니다. 이미지 재빌드는
   필요 없습니다.
4. **검증**: `curl https://<public_host>/api/auth/config` 에 `kind: "oidc"` 항목이
   보이면 서버 설정은 끝. 브라우저에서 버튼을 눌러 Microsoft 로그인 → `/auth/callback`
   → 원래 페이지로 돌아오고, 관리자 그룹 사용자는 사이드바에 Insights 가 보여야
   합니다. 로그인 직후 `POST /api/auth/session` 이 200 이면 사용자 레코드가 쌓였고,
   Insights 의 사용자 표에 이메일이 붙습니다.

알아둘 것:

- Entra `sub` 는 Cognito `sub` 와 다른 값이므로 같은 사람이 두 방식으로 로그인하면
  스레드·지식 베이스·설정이 서로 다른 소유자로 갈립니다. 한 조직은 한 방식으로
  굳히는 편이 안전합니다.
- `cognito:groups` 의 `admin` 은 Cognito 만의 규칙입니다. Entra 쪽 관리자는
  `oidc_admin_groups`/`oidc_admin_emails` 로만 정해지고, Entra 에 `admin` 이라는
  그룹이 있어도 관리자가 되지 않습니다.
- 로컬 서버는 `server/.env` 의 `OIDC_ISSUER_URL`·`OIDC_CLIENT_ID`·
  `OIDC_REDIRECT_URIS=http://localhost:3000/auth/callback` 로 같은 앱 등록을 씁니다
  (`server/env.example`).

## 운영

- 접속 주소: `terraform output alb_url`
- 잠시 내려둘 때: `terraform apply -var desired_count=0` — 리소스를 지우지 않고 태스크만 0으로 만든다.
  ALB와 NAT 게이트웨이 요금은 계속 발생한다. NAT 까지 내리려면 아래 `park.sh`, 완전히 정리하려면
  `terraform destroy`(DynamoDB 삭제 보호를 먼저 풀어야 한다, 아래 참고).
- `certificate_arn`·`public_host` 없이 올리면 ALB 는 평문 HTTP(:80)다. 그 환경에서는 브라우저의
  secure-context 전용 API(`crypto.randomUUID`, `navigator.clipboard`)가 없으므로 웹 코드에서
  직접 쓰지 말고 `web/src/lib/utils.ts`의 `randomId()` / `copyText()`를 쓴다. Entra ID 로그인은
  PKCE 에 `crypto.subtle` 이 필요해 HTTPS 가 전제다.
- 서버 컨테이너 로그: `/ecs/<project>/server`, 웹: `/ecs/<project>/web` (CloudWatch, 14일 보관).
- 콜드스타트 keep-warm: `agent-runtime/scripts/keepwarm.sh up`이 EventBridge Scheduler로
  `KEEPWARM_RATE`(기본 `rate(5 minutes)`)마다 런타임에 ping을 보내 마이크로VM을 데운다
  (Terraform 밖, `agent-runtime/`에 있음). 내릴 때는 `keepwarm.sh down`. 모델 호출이 아닌
  짧은 pong이라 비용은 0에 가깝다.

### 유휴 상태 관리 (park/unpark)

쓰지 않는 동안 고정비를 줄이려면 `scripts/park.sh` 를 쓴다. `hibernate`·`desired_count`
두 변수만 움직이는 Terraform 래퍼다.

```bash
./scripts/park.sh status   # ECS desired/running, NAT 유무, ALB, 테이블 아이템 수
./scripts/park.sh down     # 1) desired_count=0 apply → running=0 대기  2) hibernate.tfvars apply
./scripts/park.sh up       # 기본값으로 apply(NAT·EIP 재생성, 태스크 1) → 서비스 안정화 대기
```

- **내리는 것**: NAT 게이트웨이, EIP, 프라이빗 라우트 테이블의 기본 경로. ECS 태스크는 0.
- **보존하는 것**: VPC·서브넷·ALB(DNS 이름 유지), DynamoDB 테이블, S3 버킷, Cognito,
  AgentCore 런타임·Memory·게이트웨이·레지스트리, Knowledge Base.
- 순서가 중요하다: 태스크가 살아 있는 채로 NAT 를 지우면 egress 가 없어 ECR·Bedrock 호출이
  실패하므로 `down` 은 running=0 을 확인한 뒤에만 NAT 를 지운다. 직접 할 때도
  `terraform apply -var desired_count=0` 다음에 `terraform apply -var-file=hibernate.tfvars`.
- `hibernate.tfvars` 는 `hibernate=true`, `desired_count=0` 두 줄이고 비밀이 없어
  `.gitignore` 예외로 저장소에 둔다. `network` 모듈의 NAT·EIP 는 `count` 로 바뀌었고
  `moved` 블록이 기존 state 주소를 이어받으므로 이 변경을 처음 apply 해도 재생성되지 않는다.

### DynamoDB 보호와 완전 삭제

모든 DynamoDB 테이블에 삭제 보호와 PITR(35일)이 켜져 있어 `terraform destroy` 가 그대로는
실패한다. 보호를 먼저 풀고 지운다:

```bash
terraform apply -var allow_destroy=true   # 1단계: 삭제 보호 해제
terraform destroy                          # 2단계
```

destroy 전 백업과 복원은 `scripts/backup.sh` / `scripts/restore.sh`:

```bash
./scripts/backup.sh [output-dir]                          # 기본 ~/ap-backup-<timestamp>
./scripts/restore.sh <backup-dir> rehearsal               # 임시 이름(-rt<HHMMSS>)에 복원해 비교만 하고 정리 (KEEP=1 로 남김)
./scripts/restore.sh <backup-dir> live [ddb,s3,cognito]   # 실제 리소스에 복원 (대상이 비어 있지 않으면 FORCE=1)
```

- 백업: 테이블 6개(온디맨드 백업 + scan JSON), S3 `artifacts`·`knowledge`·`skills`, tfstate
  버킷, Cognito 사용자 목록, AgentCore 런타임·Memory·게이트웨이 목록(JSON).
- 복원되는 것은 DynamoDB 와 S3 다. Cognito 는 비밀번호를 내보낼 수 없어 사용자를 수동으로
  다시 만들고(`cognito/users.json` 참고), AgentCore Memory 이벤트는 공식 import 가 없고
  레지스트리 레코드는 레지스트리를 다시 만들면 새 ID 로 생기므로 둘 다 복원 대상이 아니다.

## AgentCore Memory (Runtime 에이전트용)

Runtime 에이전트를 붙일 때만 필요하다. Runtime 에이전트는 AgentCore Memory에서 대화를
복원하며, 이 리소스는 **Terraform이 아니라 CLI로 생성**한다. AWS provider에 대응 리소스가
없고, `null_resource`로 CLI를 감싸면 멱등하지 않은 데다 `terraform destroy`로도 지워지지
않는 — Terraform이 정직하게 관리할 수 없는 state가 남기 때문이다. 인스톨러는 기반 배포
뒤에 `default` 에이전트용 Memory를 만들고 그 ID를 `agent-runtime/.env`에 넣는다.

```bash
aws bedrock-agentcore-control create-memory \
  --name bap_conversations_default \
  --event-expiry-duration 365 \
  --memory-strategies '[{"summaryMemoryStrategy":{"name":"session_summary","namespaces":["/summaries/{actorId}/{sessionId}"]}}]' \
  --region ap-northeast-1
```

생성된 id를 `MEMORY_ID`로 넘긴다. id에 계정별 접미사가 붙으므로 저장소에 적어두지 않고
조회한다:

```bash
aws bedrock-agentcore-control list-memories --region ap-northeast-1 \
  --query "memories[?starts_with(id, 'bap_conversations_default-')].id" --output text
```

`--event-expiry-duration`은 최대값인 365일로 고정한다. AWS 기본값은 30일이라,
명시하지 않으면 남겨둘 의도였던 대화가 조용히 만료된다.

세션 요약(long-term) 회상은 위의 **SUMMARIZATION 전략**과 런타임의 `LONG_TERM_RECALL=true`
가 함께 있어야 동작한다. 전략 없이 플래그만 켜면 회상할 요약이 없다. 첫 턴에는 복원할
이벤트가 없으므로 서버가 회상을 건너뛴다(초기 응답 지연 방지). 직접 추가한 에이전트에 회상을
쓰려면 `bap_conversations_<module>` 이름으로 Memory를 하나 더 만든다.

harness 기반 에이전트는 이 리소스를 쓰지 않는다 — harness가 자체 managed memory를
provisioning하고 소유한다(`server/services/harness_service.py`). 그 이벤트를 읽고 쓰는
주체는 실행 중인 세션이므로 harness 실행 롤에 권한이 필요하며, `modules/harness_role`의
`AgentCoreMemory` statement가 apply 때 함께 붙인다.
