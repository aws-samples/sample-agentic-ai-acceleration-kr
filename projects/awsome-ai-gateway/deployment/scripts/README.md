<!-- Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms. -->

# Deployment Scripts

배포 및 프로비저닝 관련 스크립트 레퍼런스.

## 주요 스크립트

### install-eks.sh

EKS Fargate 배포 오케스트레이터. 사전조건 검증 → Terraform (VPC/EKS/Aurora/Cognito/IRSA) → Helm 배포 → health 검증 → (선택) Tool Gateway 프로비저닝.

**사용:**
```bash
./deployment/scripts/install-eks.sh {dev|prod}
# 선택: Tool Gateway 자동 프로비저닝 활성화
ENABLE_TOOL_GATEWAY=true ./deployment/scripts/install-eks.sh dev
```

**실행 단계:**
1. 사전조건 검증 (AWS CLI, kubectl, helm, terraform, jq)
2. 환경 로드 (values-eks-fargate-{dev,prod}.yaml, secrets contract)
3. Terraform init+apply (aws/llm-gateway-dev or llm-gateway-prod)
4. Helm add repo + dependency update + install (gateway, admin-ui, scheduler, workers, migration job)
5. Health checks (`/health`, `/health/ready`, 최대 10 retries with backoff)
6. (선택) `ENABLE_TOOL_GATEWAY=true` 이면 `provision_tool_gateway.sh deploy` 자동 호출
   - 실패 시: non-fatal (경고만 표시, EKS 배포는 유지)

---

### provision_tool_gateway.sh

다중 엔진 검색 AgentCore Tool Gateway 선택적 배포 (opt-in, default OFF).

**사용:**
```bash
deployment/scripts/provision_tool_gateway.sh {deploy|status|teardown}
# 기본값: status (현재 배포 상태 출력)
```

**명령어:**

#### deploy
```bash
# 1. terraform.tfvars 사전 설정 (copy from example)
cd deployment/terraform/environments/tool-gateway-dev
cp terraform.tfvars.example terraform.tfvars
# 필수: project_name, environment, aws_region=us-east-1
# 선택: enable_duckduckgo=true, enable_serper=true + serper_api_key, etc.

# 2. 배포 시작
AWS_REGION=us-east-1 deployment/scripts/provision_tool_gateway.sh deploy
# 단계:
#   - terraform init+apply (tool-gateway-dev env, 격리된 tfstate)
#   - (선택) TOOL_KEY_FILE 있으면 seed-tool-secrets.sh 호출
#   - terraform output → deployment/tool-gateway/dashboard.generated.env 생성

# 3. (선택) API 키 시딩
export TOOL_KEY_FILE=/path/to/engine-keys.txt
# 파일 형식: KEY=VALUE 행 (예: serper=sk-xxxxx, exa=xxx, duckduckgo 키 불필요)
deployment/scripts/provision_tool_gateway.sh deploy
```

결과: gateway ID/URL, enabled engines, Cognito M2M credentials 가 dashboard.generated.env 에 저장되고, admin-ui 에 복사 가능.

#### status
```bash
deployment/scripts/provision_tool_gateway.sh status
# 출력: gateway_id, gateway_url, engines (list), region
# 미배포 시: "not deployed" 또는 "not initialized"
```

#### teardown
```bash
deployment/scripts/provision_tool_gateway.sh teardown
# 실행:
#   - terraform destroy (모든 리소스 제거: gateway, Lambda, Cognito, 등)
#   - dashboard.generated.env 삭제
# 결과: 비용 절감 + 노출 제거 (API 키 남음, manual 삭제 필요시)
```

**환경변수:**
- `AWS_REGION` (기본 `us-east-1`) — AgentCore 관리형 커넥터 region 고정
- `TOOL_KEY_FILE` (선택) — 엔진 API 키 파일 경로 (seed-tool-secrets.sh 자동 호출)

**필수 조건:**
- terraform, aws cli, jq 설치
- AWS credentials 구성 (AdministratorAccess 권한 권고)
- Python 3.12 + pip3 (terraform apply 호스트, Lambda tool zip 빌드용)

---

### seed-tool-secrets.sh

Tool Gateway 엔진용 API 키를 AWS Secrets Manager 에 저장. 키는 Terraform 을 거치지 않고 직접 seeding.

**사용:**
```bash
deployment/scripts/seed-tool-secrets.sh <keys-file>
```

**파일 형식** (`<keys-file>`):
```
serper=sk-xxxxx
exa=d89a...
anthropic=sk-ant-...
firecrawl=fc-...
you=ydc-sk-...
brave=BSA...
tavily_lambda=tvly-dev-...
# 빈 줄과 # 주석 무시됨
```

**동작:**
- 각 엔진별 라인을 읽어 AWS Secrets Manager 에 저장
- Secret 이름: `${PROJECT_NAME}/${ENVIRONMENT}/tool/{engine}`
  - 예: `awsome/dev/tool/serper`
- DuckDuckGo 는 키 필요 없음 (쿼리당 무료)
- 실패 시: secret 이 미리 terraform 으로 생성되지 않았거나 AWS 권한 부족

**호출 순서:**
- 수동 호출: `seed-tool-secrets.sh <keys-file>`
- 자동 호출: `provision_tool_gateway.sh deploy` 내부에서 `TOOL_KEY_FILE` 설정 시 자동

---

## install-eks.sh 훅 (Tool Gateway)

`install-eks.sh` 는 표준 배포 후 선택적으로 Tool Gateway 를 프로비저닝합니다.

**활성화:**
```bash
ENABLE_TOOL_GATEWAY=true ./deployment/scripts/install-eks.sh dev
```

**동작:**
1. 표준 배포 완료 (EKS, Helm, health checks)
2. `ENABLE_TOOL_GATEWAY=true` 이면 `provision_tool_gateway.sh deploy` 호출
3. 실패 시: 경고만 표시, EKS 배포는 롤백 안 함 (non-fatal)

**전제조건:**
- terraform.tfvars 를 미리 설정하지 않으면 provision 실패
  - setup: `cd deployment/terraform/environments/tool-gateway-dev && cp terraform.tfvars.example terraform.tfvars && vim terraform.tfvars`

---

## admin-ui 대시보드 연결

provision_tool_gateway.sh deploy 후 dashboard.generated.env 를 admin-ui 환경에 복사:

```bash
# 1. dashboard.generated.env 확인
cat deployment/tool-gateway/dashboard.generated.env

# 2. admin-ui 환경에 복사 (helm values 또는 .env.local)
# Helm 배포 시:
helm upgrade admin-ui deployment/charts/admin-ui \
  --set env.NEXT_PUBLIC_TOOL_GATEWAY_ENABLED=true \
  --set env.NEXT_PUBLIC_TOOL_GATEWAY_URL=<from_generated_env> \
  --set env.NEXT_PUBLIC_TOOL_GATEWAY_ID=<from_generated_env> \
  --set env.NEXT_PUBLIC_TOOL_GATEWAY_REGION=us-east-1 \
  --set env.TOOL_GATEWAY_ARN=<from_generated_env> \
  --set env.COGNITO_TOOL_TOKEN_ENDPOINT=<from_generated_env> \
  --set env.COGNITO_TOOL_M2M_CLIENT_ID=<from_generated_env> \
  --set env.COGNITO_TOOL_M2M_CLIENT_SECRET=<from_generated_env> \
  --set env.COGNITO_TOOL_M2M_SCOPE=<from_generated_env>
```

결과: admin-ui 로그인 후 좌측 메뉴에 "Tool Gateway" 네비게이션 항목 표시 (ADMIN 권한 필요).

---

## 독립성 & 안전성

- **Terraform tfstate 격리:** tool-gateway-dev state 는 llm-gateway-dev 와 분리 (다른 S3 백엔드)
- **배포 독립성:** Tool Gateway 는 install-eks.sh 로 EKS 를 먼저 배포한 후 선택적 프로비저닝
- **Rollback:** 언제든 `provision_tool_gateway.sh teardown` 으로 정지 가능 (비용 절감)
- **비용:** 배포 중일 때만 청구 (gateway, Lambda, Cognito, managed connectors per-query)
