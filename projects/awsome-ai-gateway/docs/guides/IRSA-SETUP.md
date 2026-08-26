# IRSA 설정 가이드 (EKS ServiceAccount → IAM Role)

이 게이트웨이의 파드가 AWS(Bedrock / Bedrock Mantle / AgentCore / Secrets Manager / Cognito / Pricing)를 호출하는 **유일한 인증 수단**은 IRSA(IAM Roles for Service Accounts)입니다. 액세스 키는 어디에도 저장하지 않습니다.

이 문서는 **처음 구축**과 **고장 진단** 양쪽에 쓰도록 작성했습니다. 모든 주장에 코드 근거를 달았고, 검증하지 못한 항목은 [§10](#10-이-문서에서-검증하지-못한-것)에 분리했습니다.

> **관련 문서**
> - HTTP 401 (`Mantle (OpenAI) stream HTTP 401`) 단일 증상 대응 → [§8 증상 → 원인 매핑](#8-증상--원인-매핑) 및 [§4-3](#4-3-애노테이션-변경은-파드를-재기동하지-않으면-반영되지-않는다)
> - cowork 연동 전반 → [COWORK-GATEWAY-SETUP.md](COWORK-GATEWAY-SETUP.md)

---

## 1. IRSA 는 3자 계약이다

IRSA 가 동작하려면 **3곳이 동시에** 맞아야 합니다. 한 곳만 틀려도 파드는 자격증명을 얻지 못하거나, **의도하지 않은 신원**으로 동작합니다.

```
┌─ ① IAM Role + Policy ─────────┐   terraform (modules/irsa)
│  llm-gateway-<env>-gateway-    │   역할 이름·정책 내용
│  proxy-bedrock                 │
└────────────┬───────────────────┘
             │ 신뢰관계(trust): sub = system:serviceaccount:<ns>:<sa>
┌────────────┴───────────────────┐
│─ ② OIDC Provider ─────────────│   terraform (eks 모듈 enable_irsa)
│  클러스터를 IAM 에 등록         │
└────────────┬───────────────────┘
             │
┌────────────┴───────────────────┐
│─ ③ ServiceAccount 애노테이션 ──│   helm (install-eks.sh --set-string)
│  eks.amazonaws.com/role-arn    │
└────────────────────────────────┘
             │ 파드 생성 시점에만 주입
             ▼  AWS_ROLE_ARN / AWS_WEB_IDENTITY_TOKEN_FILE
```

**계층별 실패 증상이 다릅니다** — 이 표가 진단의 출발점입니다.

| 어긋난 곳 | 증상 |
|---|---|
| ① 정책 내용 (Action/Resource 누락) | **403** `AccessDeniedException` |
| ① 신뢰관계 `sub` 불일치 / ② OIDC provider 없음 | **502** (assume 자체 실패) |
| ③ 애노테이션 누락 → `AWS_ROLE_ARN` 미주입 | **401** (노드 역할 등으로 폴백) 또는 자격증명 없음 |
| ③ 애노테이션은 고쳤으나 **파드 미재기동** | **401** — 가장 흔한 함정 ([§4-3](#4-3-애노테이션-변경은-파드를-재기동하지-않으면-반영되지-않는다)) |
| cross-account 신뢰/ExternalId 불일치 | **claude-code 는 200(조용한 오폴백)**, cowork 는 502 ([§6-4](#6-4-claude-code-는-실패해도-200-을-반환한다)) |

### 이 레포가 만드는 역할은 3개다

| 역할 이름 | ServiceAccount (subject) | 용도 |
|---|---|---|
| `<project>-<env>-gateway-proxy-bedrock` | `<ns>:gateway-proxy` | Bedrock / Mantle / AgentCore 호출 + cross-account AssumeRole |
| `<project>-<env>-admin-api` | `<ns>:admin-api` | STS / Cognito / Pricing |
| `<project>-<env>-external-secrets` | `external-secrets:external-secrets` (**하드코딩**) | Secrets Manager 읽기 |

기본값(`project=llm-gateway`, `environment=dev`)이면 literal 은 `llm-gateway-dev-gateway-proxy-bedrock` 등입니다. 접두어·IAM path·랜덤 접미어가 **없습니다** ([modules/irsa/main.tf:143](../../deployment/terraform/modules/irsa/main.tf#L143), [:217](../../deployment/terraform/modules/irsa/main.tf#L217), [:274](../../deployment/terraform/modules/irsa/main.tf#L274)).

> **주의 1** — 고객관리형 **정책 이름이 역할 이름과 완전히 동일**합니다([:133](../../deployment/terraform/modules/irsa/main.tf#L133), [:207](../../deployment/terraform/modules/irsa/main.tf#L207), [:264](../../deployment/terraform/modules/irsa/main.tf#L264)). ARN 타입만 다릅니다(`:role/` vs `:policy/`). `--policy-arn ...:role/...` 로 붙여넣는 실수가 흔합니다.
>
> **주의 2** — 나머지 ServiceAccount(`admin-ui`, `scheduler`, `notification-worker`, `cost-recorder-worker`)는 **의도적으로 역할이 없습니다**. 향후 이들에서 AWS 를 호출하려면 모듈에 subject 를 추가해야 합니다.
>
> **주의 3** — 레포 체크리스트 중 "**4개** ARN(…notification_worker…)" 이라고 쓴 곳이 있으나 모듈 출력은 **3개**입니다([modules/irsa/outputs.tf](../../deployment/terraform/modules/irsa/outputs.tf)). 모듈 출력을 신뢰하십시오.

### gateway-proxy 정책의 Sid 인벤토리

```
BedrockInvoke               bedrock:InvokeModel / InvokeModelWithResponseStream / CountTokens
                            -> var.bedrock_allowed_model_arns
BedrockListModels           bedrock:ListFoundationModels / GetFoundationModel /
                            ListInferenceProfiles / GetInferenceProfile          -> *
InAccountMantleInference    bedrock-mantle:CreateInference / GetInference
                            -> arn:aws:bedrock-mantle:ap-northeast-1:<ACCOUNT>:*
                            -> arn:aws:bedrock-mantle:us-east-2:<ACCOUNT>:*
InAccountMantleBearer       bedrock-mantle:CallWithBearerToken                    -> *
AgentCoreInvokeGateway      bedrock-agentcore:InvokeGateway
                            -> arn:aws:bedrock-agentcore:us-east-1:<ACCOUNT>:gateway/*
AssumeCoworkMantle          sts:AssumeRole -> var.cowork_role_arn            # 변수 비면 렌더 안 됨
AssumeClaudeCode374Bedrock  sts:AssumeRole -> var.claude_code_333_role_arn   # 변수 비면 렌더 안 됨
```
([modules/irsa/main.tf:27-130](../../deployment/terraform/modules/irsa/main.tf#L27-L130))

핵심 3가지:
- **Mantle 은 `bedrock:` 가 아니라 `bedrock-mantle:` 네임스페이스입니다.** 라이브 배포에서 어렵게 확인된 사항이며, 손으로 최소권한 정책을 다시 쓸 때 가장 많이 놓칩니다.
- **Mantle 리전은 변수가 아니라 `local.mantle_regions = ["ap-northeast-1","us-east-2"]` 하드코딩**입니다([:21](../../deployment/terraform/modules/irsa/main.tf#L21)). 세 번째 리전을 쓰려면 **모듈을 수정**해야 하며 tfvars 로 덮을 수 없습니다.
- **AgentCore 는 `us-east-1` 고정**입니다(관리형 WebSearch 커넥터가 us-east-1 전용). 다른 리전에 게이트웨이를 만들면 AccessDenied 이고 모듈 수정이 필요합니다.

---

## 2. 사전 준비

### 2-1. tfstate 백엔드

백엔드는 **부분 설정**입니다 — `key`/`region`만 코드에 있고 **bucket 과 lock table 은 init 시점에 주입**해야 합니다([llm-gateway-dev/backend.tf](../../deployment/terraform/environments/llm-gateway-dev/backend.tf)).

```bash
cd deployment
AWS_REGION=ap-northeast-2 \
TFSTATE_BUCKET=llm-gateway-vanilla-tfstate-<ACCOUNT_ID> \
TFLOCK_TABLE=llm-gateway-vanilla-tflock \
  ./scripts/bootstrap-tfstate.sh
```

> ⚠️ **스크립트 기본값을 반드시 덮어써야 합니다.** [bootstrap-tfstate.sh:15-17](../../deployment/scripts/bootstrap-tfstate.sh#L15-L17) 기본값은 `llm-gateway-tfstate` / `llm-gateway-tflock` 인데, 이 배포물이 쓰는 이름은 `llm-gateway-vanilla-tfstate-<ACCOUNT_ID>` / `llm-gateway-vanilla-tflock` 입니다. 그냥 실행하면 다른 이름으로 만들어지고, 이후 `terraform init` 이 버킷/권한 오류로 실패합니다.

dev 와 prod 는 **같은 버킷**을 쓰고 state key 접두어(`dev/`, `prod/`)로만 구분됩니다.

### 2-2. terraform.tfvars

`*.tfvars` 는 **gitignore 대상**입니다([.gitignore:36-37](../../.gitignore#L36-L37)). 새로 클론하면 파일이 없습니다.

```bash
cd deployment/terraform/environments/llm-gateway-dev
[ -f terraform.tfvars ] || cp terraform.tfvars.example terraform.tfvars
```

> ⚠️ **함정 2개**
> 1. tfvars 없이 apply 하면 `variables.tf` 기본값이 적용되는데, dev 기본값은 `enable_chat_agent=false` 입니다. 라이브 계정에 chat-agent 리소스가 있으면 **plan 이 약 24개 리소스 삭제를 제안**합니다([terraform/NEW_ACCOUNT.md](../../deployment/terraform/NEW_ACCOUNT.md)에 경고 있음). **plan 의 `destroy` 개수를 반드시 0으로 확인하십시오.**
> 2. `terraform.tfvars.example` 의 `bedrock_allowed_model_arns` 는 `variables.tf` 기본값보다 **좁습니다.** example 을 그대로 복사하면 Bedrock 허용목록이 조용히 축소됩니다. 아래 §2-3 을 확인하십시오.

### 2-3. `bedrock_allowed_model_arns` — 필수 입력이자 가장 흔한 403 원인

모듈에 기본값이 **없는 필수 변수**입니다. 애플리케이션이 `global.anthropic.*` cross-region inference profile 로 호출하므로 **inference-profile ARN 과 foundation-model ARN 을 모두** 넣어야 합니다.

```hcl
# deployment/terraform/environments/llm-gateway-<env>/variables.tf 기본값 (6개)
"arn:aws:bedrock:*::foundation-model/anthropic.claude-opus-4-*",
"arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-4-*",
"arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-4-*",
"arn:aws:bedrock:*::inference-profile/global.anthropic.claude-*",
"arn:aws:bedrock:*:*:inference-profile/global.anthropic.claude-*",   # 계정 스코프 형태도 필요
"arn:aws:bedrock:ap-northeast-2::inference-profile/apac.anthropic.claude-*",
```
([llm-gateway-dev/variables.tf:123-138](../../deployment/terraform/environments/llm-gateway-dev/variables.tf#L123-L138))

- **한쪽만 넣으면 403** 입니다. IAM 이 inference-profile 리소스와 그 아래 foundation-model 을 **둘 다** 평가합니다.
- AWS 소유 형태(`:*::inference-profile/`)와 **계정 스코프 형태(`:*:*:inference-profile/`)가 둘 다** 필요합니다.
- `bedrock:CountTokens` 도 같은 리소스 목록을 씁니다 → 목록이 좁으면 토큰 카운트 preflight 도 같이 깨집니다.
- **GPT / Mantle 모델은 이 목록과 무관합니다** (Mantle 은 `bedrock-mantle:` 네임스페이스).

### 2-4. OIDC Provider 는 수동 생성하지 마십시오

EKS 모듈이 `enable_irsa = true` 로 **자동 생성**하고, IRSA 모듈은 그 output(`module.eks.oidc_provider_arn`)만 받습니다. `eksctl utils associate-iam-oidc-provider` 같은 수동 단계는 **필요 없습니다.**

> ⚠️ 별도로 OIDC provider 를 만들면 provider 가 2개가 되고, 역할 신뢰관계는 terraform 이 만든 쪽을 가리켜 **assume 이 실패**합니다.

---

## 3. STEP 1 — terraform apply

```bash
cd deployment/terraform/environments/llm-gateway-dev     # 또는 llm-gateway-prod

terraform init \
  -backend-config="bucket=llm-gateway-vanilla-tfstate-<ACCOUNT_ID>" \
  -backend-config="dynamodb_table=llm-gateway-vanilla-tflock"

terraform plan -out tf.plan      # ⚠️ destroy 개수가 0 인지 확인 (§2-2)
terraform apply tf.plan
```

### 출력 확인

```bash
terraform output all_role_arns                  # gateway_proxy / admin_api / external_secrets (3개)
terraform output -raw gateway_proxy_role_arn    # arn:aws:iam::<ACCOUNT>:role/llm-gateway-dev-gateway-proxy-bedrock
terraform output -raw admin_api_role_arn
```

### IRSA 만 고치고 싶을 때

레포의 실제 선례는 **타깃 apply** 입니다 — dev 에 us-east-2 Mantle 권한을 추가할 때 `admin_api` 드리프트를 피하려고 정책 리소스만 지정했습니다.

```bash
terraform plan  -target=module.irsa
terraform apply -target=module.irsa

# 정책 하나만 (레포 선례)
terraform apply -target=module.irsa.aws_iam_policy.bedrock

# -target 실행 뒤에는 반드시 전체 plan 으로 다른 드리프트가 없는지 확인
terraform plan
```

> `-target` 은 의존 output 갱신을 건너뜁니다. `install-eks.sh` 는 **정상 apply 로 output 이 갱신된 뒤에** 실행하십시오.

> **state 주소 참고** — 이 모듈에는 자체 `aws_iam_role` 리소스가 **없습니다.** 3개 역할 모두 업스트림 모듈(`terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks`, `~> 5.48` 고정)이 만듭니다. 따라서 `terraform state` 작업은 자식 모듈 경로를 지정해야 합니다:
> `module.irsa.module.gateway_proxy_irsa.aws_iam_role.this[0]`
>
> `.terraform.lock.hcl` 은 **의도적으로 커밋**되어 있습니다. init 오류를 "고치려고" 삭제하지 마십시오 — 신뢰정책 렌더링이 다른 5.x 버전이 올라올 수 있습니다.

---

## 4. STEP 2 — 역할 ARN 을 파드까지 전달

### 4-1. 권장 경로: `install-eks.sh`

```bash
./deployment/scripts/install-eks.sh dev      # 또는 prod
```

이 스크립트가 `terraform output` 의 역할 ARN 을 읽어 helm 애노테이션으로 **매 실행마다 주입**합니다.

```bash
GATEWAY_ROLE_ARN=$(terraform output -json   | jq -r '.gateway_proxy_role_arn.value')   # :96
ADMIN_API_ROLE_ARN=$(terraform output -json | jq -r '.admin_api_role_arn.value')       # :97
...
  --set-string "gatewayProxy.serviceAccount.annotations.eks\.amazonaws\.com/role-arn=${GATEWAY_ROLE_ARN}" \
  --set-string "adminApi.serviceAccount.annotations.eks\.amazonaws\.com/role-arn=${ADMIN_API_ROLE_ARN}"
```
([install-eks.sh:96-97](../../deployment/scripts/install-eks.sh#L96-L97), [:378-379](../../deployment/scripts/install-eks.sh#L378-L379))

**즉, 이 경로에서는 values 파일에 적힌 역할 ARN 리터럴이 무효화됩니다.** 스크립트는 엄격한 post-apply 소비자라서 `.terraform` 이 없거나 `terraform output` 이 실패하면 실행을 거부합니다([:76-121](../../deployment/scripts/install-eks.sh#L76-L121)).

> ⚠️ 초기화된 환경 디렉터리와 **다른 위치/셸**에서 실행하거나 stale 한 로컬 `.terraform` 이 있으면, **다른 환경의 역할 ARN 을 조용히 배포**합니다.

### 4-2. 수동 helm 경로를 쓸 때

```bash
helm upgrade llm-gateway ./deployment/charts/llm-gateway \
  --namespace llm-gateway \
  -f ./deployment/charts/llm-gateway/values-eks-fargate-dev.yaml \
  --wait --timeout 15m
```

> ⚠️ **이 경로는 values 파일에 하드코딩된 ARN 을 그대로 사용합니다.** 현재 values 파일에는 예시 계정 `123456789012` 이 박혀 있습니다:
> [values-eks-fargate-dev.yaml:200](../../deployment/charts/llm-gateway/values-eks-fargate-dev.yaml#L200), [:236](../../deployment/charts/llm-gateway/values-eks-fargate-dev.yaml#L236), [values-eks-fargate-prod.yaml:151](../../deployment/charts/llm-gateway/values-eks-fargate-prod.yaml#L151), [:188](../../deployment/charts/llm-gateway/values-eks-fargate-prod.yaml#L188)
>
> 다른 계정에 배포하면서 이 경로를 쓰면 **타 계정 역할 ARN**이 주입됩니다. `grep -rn "123456789012" deployment/charts/llm-gateway/` 로 일괄 점검하십시오 — `imageRegistry`(ECR), `AGENTCORE_RUNTIME_ARN`, `CHAT_STAGING_BUCKET` 도 함께 교체 대상입니다.

**ServiceAccount 이름은 절대 바꾸지 마십시오.** 신뢰정책이 `system:serviceaccount:<ns>:gateway-proxy` 를 못박고 있습니다([values.yaml:324](../../deployment/charts/llm-gateway/values.yaml#L324) `gateway-proxy`, [:423](../../deployment/charts/llm-gateway/values.yaml#L423) `admin-api`).

> **참고** — `values.yaml` 최상위의 `serviceAccount:` 블록([values.yaml:50](../../deployment/charts/llm-gateway/values.yaml#L50))은 **어떤 템플릿도 읽지 않습니다** (`grep -rn '\.Values\.serviceAccount' templates/` → 0건). 여기에 애노테이션을 넣으면 조용히 무시됩니다. 반드시 컴포넌트별 `<svc>.serviceAccount` (예: `gatewayProxy.serviceAccount`) 를 쓰십시오.

### 4-3. 애노테이션 변경은 파드를 재기동하지 않으면 반영되지 않는다

**이 문서에서 가장 중요한 함정입니다.**

파드 템플릿은 **ConfigMap 과 Secret 만** 해시합니다([gateway-proxy/deployment.yaml:33-35](../../deployment/charts/llm-gateway/templates/gateway-proxy/deployment.yaml#L33-L35)). ServiceAccount 애노테이션은 해시 대상이 아니므로, 애노테이션만 바꾼 `helm upgrade` 는 **새 파드 템플릿을 만들지 않고 → 롤아웃도 발생하지 않습니다.**

IRSA 환경변수(`AWS_ROLE_ARN`, `AWS_WEB_IDENTITY_TOKEN_FILE`)는 **파드 생성 시점에만** 주입되므로, 기존 파드는 계속 **이전 신원**(또는 신원 없음)으로 동작합니다.

```bash
# 1) SA 객체에 새 ARN 이 반영됐는지
kubectl get sa gateway-proxy -n llm-gateway -o yaml | grep role-arn
kubectl get sa admin-api     -n llm-gateway -o yaml | grep role-arn

# 2) 파드를 재생성해야 웹훅이 새 역할을 주입한다 (필수)
for c in gateway-proxy admin-api; do
  DEPLOY=$(kubectl get deploy -n llm-gateway -l app.kubernetes.io/component=$c -o jsonpath='{.items[0].metadata.name}')
  kubectl -n llm-gateway rollout restart deploy/"$DEPLOY"
  kubectl -n llm-gateway rollout status  deploy/"$DEPLOY" --timeout=10m
done
```

> Deployment 이름은 릴리스 이름에 따라 달라지므로 위처럼 **라벨로 조회**하십시오.

### 4-4. 네임스페이스

네임스페이스는 차트 값이 아니라 항상 `{{ .Release.Namespace }}` 이고, **`install-eks.sh` 는 dev/prod 모두 `llm-gateway` 로 하드코딩**합니다([install-eks.sh:36](../../deployment/scripts/install-eks.sh#L36)).

> ⚠️ `application_namespace` 를 바꾸면 IRSA 가 **조용히** 깨집니다: 신뢰정책은 새 네임스페이스를 요구하는데 파드는 `llm-gateway` 에 뜨므로 `AssumeRoleWithWebIdentity` 가 거부됩니다. 레포 문서 헤더에는 서로 다른 네임스페이스가 3개 등장하므로 **`install-eks.sh` 를 기준으로 삼으십시오.**

### 4-5. 차트 밖에서 관리되는 ServiceAccount

| 컴포넌트 | 네임스페이스 : SA | 애노테이션 주체 |
|---|---|---|
| External Secrets Operator | `external-secrets:external-secrets` | **terraform** (`helm_release`) |
| ALB Controller | `kube-system:aws-load-balancer-controller` | **terraform** |
| Grafana | `observability:kps-grafana` | 셸 스크립트 |
| AgentCore | K8s SA **없음** | IAM 실행 역할 (서비스 주체가 assume) |

> ESO 의 SA 를 손으로 고치면 다음 `terraform apply` 에 덮여 씁니다. 반대로 IRSA apply 가 실패하면 ESO 는 역할이 없어 모든 ExternalSecret 이 멈춥니다.

---

## 5. STEP 3 — 검증 (전부 읽기 전용)

순서대로 진행하십시오. 앞 단계가 틀리면 뒤 단계 판정은 의미가 없습니다.

### (A) 역할·정책이 존재하고 붙어 있는가

```bash
ENV=dev
ROLE="llm-gateway-$ENV-gateway-proxy-bedrock"
aws iam get-role --role-name "$ROLE" --query 'Role.AssumeRolePolicyDocument'
aws iam list-attached-role-policies --role-name "$ROLE"
```
신뢰정책의 `...:sub` 가 정확히 `system:serviceaccount:llm-gateway:gateway-proxy` 여야 합니다.

### (B) **배포된** 정책 내용 확인 — "코드에 있다 ≠ 배포돼 있다"

```bash
ARN=$(aws iam list-policies --scope Local \
       --query "Policies[?contains(PolicyName,'gateway-proxy-bedrock')].Arn" --output text)
VER=$(aws iam get-policy --policy-arn "$ARN" --query 'Policy.DefaultVersionId' --output text)
echo "policy=$ARN default-version=$VER"
aws iam get-policy-version --policy-arn "$ARN" --version-id "$VER" \
  --query 'PolicyVersion.Document.Statement[].Sid' --output table
```

> ⚠️ **`--version-id v1` 을 하드코딩하지 마십시오.** 레포의 일부 문서가 v1 로 고정해 두었는데, 그러면 나중에 추가된 statement(두 AssumeRole Sid 포함)를 못 봅니다. 반드시 `DefaultVersionId` 를 먼저 조회하십시오. 버전이 아직 v1 이면 추가분 미반영을 의심하십시오.

기대 Sid: [§1 인벤토리](#gateway-proxy-정책의-sid-인벤토리) 참조. cross-account 를 쓴다면 `AssumeCoworkMantle` / `AssumeClaudeCode374Bedrock` 이 보여야 합니다.

### (C) SA 애노테이션

```bash
kubectl get sa gateway-proxy   -n llm-gateway      -o yaml | grep role-arn
kubectl get sa admin-api       -n llm-gateway      -o yaml | grep role-arn
kubectl get sa external-secrets -n external-secrets -o yaml | grep role-arn
```

### (D) 파드가 실제로 그 신원을 얻었는가 — 결정적 확인

```bash
NS=llm-gateway
POD=$(kubectl get pod -n "$NS" -l app.kubernetes.io/component=gateway-proxy \
       -o jsonpath='{.items[0].metadata.name}')

# IRSA 환경변수 주입 여부
kubectl exec -n "$NS" "$POD" -c gateway-proxy -- env | grep -E '^AWS_(ROLE_ARN|WEB_IDENTITY)'

# 실제 신원
kubectl exec -n "$NS" "$POD" -c gateway-proxy -- \
  python -c "import boto3;i=boto3.client('sts').get_caller_identity();print(i['Arn']);print(i['Account'])"
```

`assumed-role/llm-gateway-<env>-gateway-proxy-bedrock/...` + **의도한 계정 ID** 가 나와야 합니다.
`AWS_ROLE_ARN` 이 없으면 → 애노테이션 누락 또는 **파드 미재기동**([§4-3](#4-3-애노테이션-변경은-파드를-재기동하지-않으면-반영되지-않는다)).

### (E) 실제 호출이 되는가

```bash
kubectl exec -n "$NS" deploy/"$DEPLOY" -- \
  sh -c 'aws bedrock-runtime invoke-model \
    --model-id global.anthropic.claude-haiku-4-5-20251001-v1:0 \
    --body "{\"anthropic_version\":\"bedrock-2023-05-31\",\"max_tokens\":8,\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}" \
    --cli-binary-format raw-in-base64-out /tmp/out.json && cat /tmp/out.json'
```

> 이 프로브는 **in-account 신원만** 증명합니다. cross-account 경로는 [§6-5](#6-5-검증) 로 별도 확인해야 합니다.

---

## 6. cross-account 확장 (cowork → 222, claude-code → 333)

이 게이트웨이는 클라이언트별로 **다른 AWS 계정**의 Bedrock 을 호출할 수 있습니다. terraform 은 **호출자(caller) 측 권한만** 만듭니다. 타깃 계정의 역할은 별도로 만들어야 합니다.

| 클라이언트 | 타깃 | 방식 | ExternalId |
|---|---|---|---|
| `cowork` | 222 (Tokyo) | Bedrock **Mantle** (bearer) | `cowork-bedrock` |
| `claude-code` | 333 | Bedrock **native** (boto3) | `claude-code-bedrock` |
| `codex` | 동일 계정 | Mantle in-account (assume 불필요) | — |

### 6-1. 호출자 측 (terraform)

```hcl
# deployment/terraform/environments/<env>/terraform.tfvars
cowork_role_arn          = "arn:aws:iam::222233334444:role/llm-gateway-cowork-bedrock"
claude_code_333_role_arn = "arn:aws:iam::333344445555:role/llm-gateway-claude-code-bedrock"
```

**빈 문자열이면 `sts:AssumeRole` statement 가 아예 렌더되지 않습니다** — terraform 오류 없이 cross-account 기능이 조용히 사라집니다([modules/irsa/main.tf:103-129](../../deployment/terraform/modules/irsa/main.tf#L103-L129)).

이 값은 **DB 컬럼과 바이트 단위로 같아야** 합니다:
```sql
SELECT client, account_role_arn, external_id, region, backend, default_model, enabled
FROM model.routing_profiles ORDER BY client;
```

### 6-2. 타깃 계정 측

**cowork (222)** — 커밋된 멱등 스크립트가 있습니다. **222 자격증명으로** 실행하십시오.

```bash
# dev 만 신뢰 (스크립트 기본값)
./deployment/scripts/provision_cowork_cross_account_role.sh

# dev + prod 둘 다 신뢰 (공백 구분)
GATEWAY_PROXY_ROLE_ARNS="arn:aws:iam::<ACCT>:role/llm-gateway-dev-gateway-proxy-bedrock arn:aws:iam::<ACCT>:role/llm-gateway-prod-gateway-proxy-bedrock" \
  ./deployment/scripts/provision_cowork_cross_account_role.sh
```

신뢰정책 형태:
```json
{ "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "AWS": ["arn:aws:iam::<GATEWAY_ACCOUNT>:role/<PROJECT>-<ENV>-gateway-proxy-bedrock"] },
    "Action": "sts:AssumeRole",
    "Condition": { "StringEquals": { "sts:ExternalId": "cowork-bedrock" } } }] }
```

**순서가 중요합니다**: 222 신뢰정책이 123 IRSA 역할 ARN 을 지목하므로 **terraform apply 가 먼저**입니다.

**claude-code (333)** — **프로비저닝 스크립트가 없습니다. 수동 생성해야 합니다.** 권한은 **Bedrock native** 여야 하며 `bedrock-mantle:*` 를 넣으면 안 됩니다(claude-code 는 boto3 native). foundation-model 과 inference-profile ARN 을 **모두** 넣어야 합니다([§2-3](#2-3-bedrock_allowed_model_arns--필수-입력이자-가장-흔한-403-원인) 과 동일한 이유).

> 두 역할 모두 **`MaxSessionDuration >= 3600`** 이어야 합니다. 브로커가 `DurationSeconds=3600` 을 요청합니다([mantle_credentials.py:32](../../gateway-proxy/src/app/services/mantle_credentials.py#L32)). 900초로 조인 역할은 `ValidationError` 로 즉시 거부됩니다.

### 6-3. DB 변경 후 Redis 캐시 무효화 (필수)

라우팅 프로파일은 `routing_profile:{client}` 로 **300초 캐시**되고, 로더는 **DB 를 보기 전에 캐시를 읽고 리턴**합니다([routing_profile_loader.py:16](../../gateway-proxy/src/app/services/routing_profile_loader.py#L16), [:32-43](../../gateway-proxy/src/app/services/routing_profile_loader.py#L32-L43)).

```bash
kubectl exec -i -n llm-gateway "$POD" -c gateway-proxy -- python - <<'PY'
import asyncio
from app.config import get_settings
from app.redis_client import create_redis_client

KEYS = ["routing_profile:claude-code", "routing_profile:cowork"]

async def main():
    r = await create_redis_client(get_settings())   # cluster/standalone 자동 판별
    for k in KEYS:                                  # 키마다 개별 DEL (Cluster CROSSSLOT 회피)
        print(("deleted " if await r.delete(k) else "absent  ") + k)
    await r.aclose()

asyncio.run(main())
PY
```

> ⚠️ 키 이름은 `routing_profile:{client}` 입니다. **`routing:{client}` 를 지우면 오류 없이 no-op** 이고 최대 5분간 옛 프로파일이 계속 서비스됩니다. 롤백 중이라면 장애가 5분 더 이어집니다.

### 6-4. claude-code 는 실패해도 200 을 반환한다

**cutover 후 가장 위험한 경우입니다.**

```python
try:
    return await self._client_resolver()          # cross-account
except Exception:
    logger.warning("bedrock_xacct_resolve_failed_fallback_inaccount", exc_info=True)
    if self._fallback_client is not None:
        return self._fallback_client               # ← 게이트웨이 자기 계정으로 조용히 폴백
    raise
```
([bedrock_adapter.py:65-75](../../gateway-proxy/src/app/providers/bedrock_adapter.py#L65-L75))

신뢰정책이 깨졌거나 ExternalId 가 틀렸거나 `sts:AssumeRole` 이 없어도 **요청은 200 으로 성공**하고, 토큰은 **게이트웨이 계정에 과금**됩니다. 유일한 신호는 WARNING 로그 한 줄입니다.

반면 **cowork 는 폴백이 없어 502** 로 즉시 드러납니다.

### 6-5. 검증

```bash
# ① 파드 안에서 타깃 역할 assume — 신뢰 + 호출자 정책 + ExternalId 를 한 번에 증명
kubectl exec -n llm-gateway "$POD" -c gateway-proxy -- python -c \
 "import boto3;print(boto3.client('sts').assume_role(
    RoleArn='arn:aws:iam::333344445555:role/llm-gateway-claude-code-bedrock',
    RoleSessionName='t',ExternalId='claude-code-bedrock')['Credentials']['AccessKeyId'])"
# ASIA... 로 시작하면 임시 자격증명 발급 성공
# 반드시 파드 안에서 실행 — 신뢰정책이 IRSA 역할을 지목하므로 로컬 IAM 사용자로는 실패합니다.

# ② 조용한 폴백이 일어나고 있지 않은지 (가장 중요)
kubectl logs -n llm-gateway -l app.kubernetes.io/component=gateway-proxy --tail=200 \
  | grep bedrock_xacct_resolve_failed_fallback_inaccount
# 정상 신호:
kubectl logs -n llm-gateway -l app.kubernetes.io/component=gateway-proxy --tail=200 \
  | grep bedrock_xacct_client_built
```

> `bedrock_xacct_client_built` 는 `(role_arn, region, external_id)` 캐시 항목당 **한 번만** 찍히고 클라이언트가 약 55분 재사용됩니다. 짧은 구간에 이 로그가 없다는 것이 폴백의 증거는 **아닙니다** — WARNING 쪽을 확인하십시오.

**실제로 어느 경로가 트래픽을 서비스했는지**:
```sql
SELECT client, provider, model_alias, count(*), round(sum(cost_usd),6)
FROM usage.usage_logs WHERE client='cowork' GROUP BY 1,2,3;

-- 시간창 변형: 컬럼은 requested_at 입니다 (created_at 아님)
SELECT model_alias, status, count(*) FROM usage.usage_logs
WHERE requested_at > now() - interval '10 min' GROUP BY 1,2 ORDER BY 3 DESC;
```

### 6-6. 롤백

```sql
UPDATE model.routing_profiles SET account_role_arn=NULL WHERE client='claude-code';
```
→ in-account 경로로 즉시 복귀(재배포 불필요). **[§6-3](#6-3-db-변경-후-redis-캐시-무효화-필수) 캐시 무효화를 반드시 수행**해야 5분 지연이 없습니다.

---

## 7. prod 드리프트 — 가장 큰 지뢰

**prod 는 두 cross-account 변수를 IRSA 모듈에 전달하지 않습니다. 선언조차 되어 있지 않습니다.**

| | dev | prod |
|---|---|---|
| `module "irsa"` 인자 수 | **9** | **7** |
| `cowork_role_arn` | 전달 | ❌ 없음 |
| `claude_code_333_role_arn` | 전달 | ❌ 없음 |
| 렌더되는 statement 수 | **7** | **5** |

([llm-gateway-dev/main.tf:55-68](../../deployment/terraform/environments/llm-gateway-dev/main.tf#L55-L68) vs [llm-gateway-prod/main.tf:51-62](../../deployment/terraform/environments/llm-gateway-prod/main.tf#L51-L62) — prod 의 `main.tf`/`variables.tf` 에서 두 변수 grep 결과 **0건**)

**결과가 비대칭입니다.**
- `cowork` → AssumeRole 실패 → **모든 요청 502** (즉시 발견됨)
- `claude-code` → **조용히 in-account 로 폴백 → 200** ([§6-4](#6-4-claude-code-는-실패해도-200-을-반환한다)). 계정 귀속이 잘못되지만 **정상으로 보입니다.**

**게다가 DB 마이그레이션은 모든 환경에 cross-account ARN 을 무조건 시드합니다.** 즉 prod DB 는 prod IAM 이 assume 할 수 없는 역할을 가리키게 되며, 이 불일치는 **트래픽이 올 때까지 보이지 않습니다.**

**prod 에서 cross-account 를 쓰려면 tfvars 수정만으로는 불가능합니다**(미선언 변수). 코드 변경 + 리뷰가 필요합니다:

```hcl
# 1) llm-gateway-prod/variables.tf — 변수 선언 추가
variable "cowork_role_arn"          { type = string; default = "arn:aws:iam::222233334444:role/llm-gateway-cowork-bedrock" }
variable "claude_code_333_role_arn" { type = string; default = "arn:aws:iam::333344445555:role/llm-gateway-claude-code-bedrock" }

# 2) llm-gateway-prod/main.tf — module "irsa" 에 추가
  cowork_role_arn          = var.cowork_role_arn
  claude_code_333_role_arn = var.claude_code_333_role_arn
```
```
# 3) 222 / 333 자격증명으로 타깃 역할 신뢰정책에 prod IRSA 역할 ARN 추가
#    arn:aws:iam::<ACCT>:role/llm-gateway-prod-gateway-proxy-bedrock
```

> prod 정책에 statement 가 5개뿐인 것은 **설계상 정상**입니다. dev 와 비교해 "누락"으로 보고 dev 값을 그대로 prod 에 적용하지 마십시오.

---

## 8. 증상 → 원인 매핑

| 증상 | 계층 | 확인 |
|---|---|---|
| **403** `AccessDeniedException ... inference-profile/global.anthropic...` | 정책 Resource 부족 | [§2-3](#2-3-bedrock_allowed_model_arns--필수-입력이자-가장-흔한-403-원인) — foundation-model + inference-profile 양쪽 필요 |
| **403** `bedrock-mantle:CreateInference` 거부 | Mantle Sid 누락/리전 | [§1](#gateway-proxy-정책의-sid-인벤토리) + (B) 배포 정책 확인 |
| **401** `Mantle (OpenAI) stream HTTP 401` | 서명 리전 불일치 또는 파드 신원 | bearer 는 발급됐고(아니면 502) 업스트림이 거부한 상태 → ① 서명 리전이 호출 리전과 같은지 ② 파드가 기대한 역할로 붙었는지([§5](#5-step-3--검증-전부-읽기-전용)) ③ 애노테이션 변경 후 재기동했는지([§4-3](#4-3-애노테이션-변경은-파드를-재기동하지-않으면-반영되지-않는다)) |
| **502** `Mantle (OpenAI) auth failed` | 자격증명 획득 실패 (assume/신뢰/OIDC) | (A)(B)(D) |
| **502** cowork 전 요청 | `AssumeCoworkMantle` 누락 또는 타깃 신뢰/ExternalId | [§6](#6-cross-account-확장-cowork--222-claude-code--333), [§7](#7-prod-드리프트--가장-큰-지뢰) |
| **200 인데 계정이 틀림** | claude-code 조용한 폴백 | [§6-4](#6-4-claude-code-는-실패해도-200-을-반환한다), [§6-5 ②](#6-5-검증) |
| `Not authorized to perform sts:AssumeRoleWithWebIdentity` | 신뢰 subject 불일치 (네임스페이스/SA 이름) | [§4-4](#4-4-네임스페이스), (A) |
| ExternalSecret 이 계속 실패 | ESO 역할/시크릿 경로 | [§9](#9-함정-모음) 선행 슬래시 항목 |
| 애노테이션은 맞는데 계속 실패 | **파드 미재기동** | [§4-3](#4-3-애노테이션-변경은-파드를-재기동하지-않으면-반영되지-않는다) |

> **로그 공백 주의** — Mantle 자격증명 브로커는 logger 를 바인딩만 하고 **한 번도 호출하지 않습니다**([mantle_credentials.py:14](../../gateway-proxy/src/app/services/mantle_credentials.py#L14)). STS AssumeRole·IRSA 취득·bearer 발급·캐시 히트/미스 전부 **로그 0줄**입니다. 이 계층은 CloudTrail 로 봐야 합니다:
> - `AssumeRole` `RoleSessionName=gw-mantle` → cowork → 222 Mantle
> - `AssumeRole` `RoleSessionName=gw-bedrock-xacct` → claude-code → 333 native

---

## 9. 함정 모음

| # | 함정 | 결과 |
|---|---|---|
| 1 | 애노테이션만 바꾸고 파드 재기동 안 함 | 이전 신원 유지 → 401 ([§4-3](#4-3-애노테이션-변경은-파드를-재기동하지-않으면-반영되지-않는다)) |
| 2 | `--version-id v1` 하드코딩 | 나중에 추가된 statement 를 못 봄 ([§5 B](#b-배포된-정책-내용-확인--코드에-있다--배포돼-있다)) |
| 3 | 정책 ARN 을 `:role/` 로 씀 | 이름이 같아서 헷갈림 → 명령 실패 |
| 4 | `application_namespace` 변경 | `install-eks.sh` 는 `llm-gateway` 하드코딩 → subject 불일치 |
| 5 | ServiceAccount 이름 변경 | 신뢰 subject 불일치 → 자격증명 없음 |
| 6 | `terraform.tfvars.example` 을 그대로 복사 | Bedrock 허용목록이 조용히 축소 → 403 |
| 7 | tfvars 없이 apply | `enable_chat_agent=false` → plan 이 ~24개 삭제 제안 |
| 8 | `bootstrap-tfstate.sh` 기본값 사용 | 버킷 이름 불일치 → init 실패 |
| 9 | 수동 helm 경로 + 123 리터럴 | 타 계정 역할 ARN 주입 ([§4-2](#4-2-수동-helm-경로를-쓸-때)) |
| 10 | Mantle 에 `bedrock:` 권한 부여 | `bedrock-mantle:` 네임스페이스여야 함 |
| 11 | 333 에 `bedrock-mantle:*` 부여 | claude-code 는 native → 실패, 게다가 폴백에 가려짐 |
| 12 | 타깃 역할 `MaxSessionDuration` 900초 | AssumeRole `ValidationError` |
| 13 | `routing:{client}` 캐시 삭제 | no-op, 5분간 옛 값 |
| 14 | OIDC provider 수동 생성 | provider 2개 → 신뢰 불일치 |
| 15 | `.terraform.lock.hcl` 삭제 | 신뢰정책 렌더링이 다른 5.x 유입 |
| 16 | ESO 시크릿 경로에 선행 슬래시 누락 | `/llm-gateway/dev/*` 와 불일치 → SecretSyncedError |
| 17 | 전체 apply 로 IRSA 만 고치려 함 | 무관한 드리프트 동반 → `-target` 권장 ([§3](#irsa-만-고치고-싶을-때)) |

### 사용하지 말아야 할 레거시 산출물

- **`scripts/create_cowork_mantle_iam.sh`** — 333 를 **게이트웨이 계정**으로 가정하던 시절의 스크립트입니다(333 는 2026-07-05 cutover 이후 claude-code **타깃** 계정). 지금 실행하면 **잘못된 주체로 신뢰정책을 쓰고** `bedrock-mantle:*` 대신 `bedrock:*` 를 부여합니다. 또한 인라인 정책을 IRSA 역할에 직접 붙이므로 terraform 이 관리하는 attached policy 에서 **보이지 않고 apply 로도 제거되지 않습니다.**
- **`mantle_credentials.py` 의 docstring**, **`db/init/03_seed_data.sql`**, **`docs/guides/connect.md`** 의 333 관련 서술은 **stale** 합니다(claude-code 를 in-account 로 기술). [§6](#6-cross-account-확장-cowork--222-claude-code--333) 과 마이그레이션 0022 를 신뢰하십시오.

### 보안 리뷰에서 지적될 항목

`secrets_manager_kms_key_arns` 기본값이 `["*"]` 이고 **dev·prod 모두 이 값을 전달하지 않습니다**([modules/irsa/variables.tf:32-36](../../deployment/terraform/modules/irsa/variables.tf#L32-L36)). 즉 배포된 ESO 역할은 **계정의 모든 KMS 키에 `kms:Decrypt`** 권한을 가집니다. 좁히려면 **두 환경의 `main.tf` 에 인자를 추가**해야 합니다(현재 어느 쪽도 전달하지 않음).

반대로 잘못 좁히면 CMK 로 암호화된 시크릿을 ESO 가 복호화하지 못합니다. `secret:rds!cluster-*` 와일드카드는 Aurora 관리형 마스터 시크릿(랜덤 접미어 회전)용이므로 **의도적**입니다 — 좁히면 migration Job 의 마스터 비밀번호 조회가 깨집니다.

---

## 10. 이 문서에서 검증하지 못한 것

정직하게 분리합니다. 아래는 **레포 코드로 확인할 수 없었던** 항목이며, 실제 계정/클러스터 상태로 확인해야 합니다.

1. **`namespace_service_accounts` → 신뢰정책 조건의 정확한 렌더링**. 업스트림 자식 모듈 로직이며 이 레포에는 `.terraform/` 아래에만 존재합니다. 레포 전체에서 `system:serviceaccount` grep 결과는 0건입니다. `sub` 형태와 `sts.amazonaws.com` audience 는 업스트림 관례에 근거한 서술입니다.
2. **EKS pod-identity mutating webhook 의 실제 주입 동작**. `AWS_ROLE_ARN` / `AWS_WEB_IDENTITY_TOKEN_FILE` 을 참조하는 레포 파일이 없습니다. "애노테이션 변경은 파드 재생성이 필요하다"는 결론은 **차트 측 근거**(파드 템플릿에 SA 파생 해시 없음)로만 증명했습니다.
3. **라이브 클러스터의 실제 네임스페이스**. 레포 근거는 `llm-gateway` 를 가리키지만 문서 헤더들이 서로 다릅니다.
4. **`llm-gateway-prod` 가 실제로 apply 된 적이 있는지**, prod IRSA 역할이 라이브에 존재하는지.
5. **333 타깃 역할의 실제 신뢰/권한 정책**. 레포에 스크립트·terraform·JSON 이 **전혀 없습니다**. [§6-2](#6-2-타깃-계정-측)의 claude-code 정책 형태는 cowork 스크립트 구조 + IRSA 모듈의 Bedrock action/ARN 에서 **유도한 것**입니다.
6. **333 역할의 `MaxSessionDuration` 실제 값**. cowork 스크립트만 `--max-session-duration 3600` 을 설정합니다.
7. **재사용된 Mantle bearer 가 IAM 상 `bedrock:CallWithBearerToken` 으로 평가되는지 `bedrock-mantle:CallWithBearerToken` 으로 평가되는지**. 토큰 생성기의 SigV4 service name 은 `"bedrock"` 인데 terraform 이 부여하는 것은 `bedrock-mantle:CallWithBearerToken` 입니다. 모듈 주석은 라이브 probe 로 확인했다고 적고 있으나 이 문서에서 IAM 호출로 재확인하지는 못했습니다.
8. **`AmazonBedrockMantleInferenceAccess`** (일부 고객 안내 문서가 권장하는 AWS 관리형 정책)가 222 역할에 실제로 쓰이는지. 레포 스크립트는 `mantle-invoke` 라는 **인라인** 정책에 `bedrock-mantle:*` 를 넣습니다.
9. **cluster-mode ElastiCache 에서의 `DEL`**. [§6-3](#6-3-db-변경-후-redis-캐시-무효화-필수) 스니펫은 레포의 in-pod redis 패턴에서 유도했으며 CROSSSLOT 회피를 위해 키를 하나씩 지웁니다.
10. **`alembic current` / `SELECT version_num FROM alembic_version`** 은 레포에 존재하지 않는 명령입니다. 레포에 있는 리비전 확인 수단은 `alembic upgrade head` 와 Helm values 의 migration 이미지 태그 고정뿐입니다.
11. **CloudTrail 조회 CLI** 는 레포에 없습니다. [§8](#8-증상--원인-매핑)의 `RoleSessionName` 값은 소스 코드에서 확인한 것이지만, `aws cloudtrail lookup-events` 명령 자체는 이 문서에서 새로 작성한 것입니다.
12. **`enabled` 컬럼을 롤백 레버로 쓰는 관행**. 로더는 `enabled=false` 행에 `None` 을 반환해 해당 클라이언트를 **조용히 in-account 경로로** 되돌립니다. 이를 롤백 수단으로 기술한 문서는 없습니다.

---

## 부록 — 빠른 체크리스트

```
[ ] tfstate 백엔드 이름을 llm-gateway-vanilla-* 로 override 했다        (§2-1)
[ ] terraform.tfvars 를 만들고 plan 의 destroy 가 0 이다                (§2-2)
[ ] bedrock_allowed_model_arns 에 foundation-model + inference-profile
    (AWS소유형 + 계정스코프형) 모두 있다                                 (§2-3)
[ ] terraform apply 성공, output 3개 확인                              (§3)
[ ] install-eks.sh 로 배포했다 (또는 values 의 123 리터럴을 교체했다)     (§4)
[ ] SA 애노테이션 확인 후 rollout restart 했다                          (§4-3)
[ ] 배포된 정책의 DefaultVersionId 를 조회해 Sid 목록을 확인했다          (§5 B)
[ ] 파드 안에서 get_caller_identity 가 의도한 역할/계정을 반환한다        (§5 D)
[ ] (cross-account) 타깃 역할 신뢰에 이 환경의 IRSA 역할 ARN 이 있다      (§6-2)
[ ] (cross-account) terraform 변수 == DB account_role_arn               (§6-1)
[ ] (cross-account) Redis routing_profile:{client} 를 무효화했다          (§6-3)
[ ] (cross-account) fallback_inaccount WARNING 이 없다                  (§6-5)
[ ] (prod) cross-account 를 쓴다면 §7 코드 변경을 반영했다               (§7)
```
