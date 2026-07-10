# 03. Secrets Manager 설정

**목적**: 애플리케이션이 사용할 비밀값을 AWS Secrets Manager에 저장.
**소요**: 15분

---

## 배경

우리 chart는 **External Secrets Operator (ESO)** 를 써서 AWS Secrets Manager → K8s Secret 을 자동 동기화합니다. Helm이 참조할 경로:

```
/llm-gateway/<env>/app        ← 애플리케이션 시크릿 (VK 암호화 키 등)
/llm-gateway/<env>/db         ← DB 비밀번호
/llm-gateway/<env>/redis      ← Redis AUTH 토큰
```

이 경로에 Secret이 없으면 install-eks.sh가 **설치를 중단하고 생성 방법을 알려줍니다**.

---

## 1. App Secrets 생성

### 1.1 키 생성

세 가지 랜덤 값을 `openssl` 로 생성합니다.

```bash
VK_KEY=$(openssl rand -hex 32)
NEXTAUTH_SECRET=$(openssl rand -hex 32)
JWKS_CACHE_KEY=$(openssl rand -hex 32)
```

각 용도:

| 변수 | 용도 |
|-----|-----|
| `VK_KEY` | Virtual Key AES-256-GCM DEK. VK 암/복호화 전체 보안 기반 |
| `NEXTAUTH_SECRET` | Admin UI (Next.js) 세션 토큰 서명 |
| `JWKS_CACHE_KEY` | JWT JWKS 캐시 위변조 방지 HMAC |

⚠️ **이 값들을 로그/Slack에 남기지 마세요**. 터미널 스크롤 기록도 주의.

### 1.2 Secrets Manager에 저장

```bash
aws secretsmanager create-secret \
  --name "/llm-gateway/$ENV/app" \
  --description "LLM Gateway application secrets (env: $ENV)" \
  --secret-string "{
    \"virtual_key_encryption_key\": \"$VK_KEY\",
    \"nextauth_secret\": \"$NEXTAUTH_SECRET\",
    \"jwt_jwks_cache_key\": \"$JWKS_CACHE_KEY\"
  }" \
  --region "$AWS_REGION"
```

✅ 성공 시 `ARN`, `Name`, `VersionId` 가 출력됩니다.

🐛 `ResourceExistsException` — 이전 배포의 secret이 남아있는 경우입니다. 두 가지 상황에 따라 처리:

**상황 A: secret이 활성 상태로 존재** — 값만 갱신:
```bash
aws secretsmanager put-secret-value \
  --secret-id "/llm-gateway/$ENV/app" \
  --secret-string "{
    \"virtual_key_encryption_key\": \"$VK_KEY\",
    \"nextauth_secret\": \"$NEXTAUTH_SECRET\",
    \"jwt_jwks_cache_key\": \"$JWKS_CACHE_KEY\"
  }" \
  --region "$AWS_REGION"
```

**상황 B: secret이 삭제 예약 상태 (30일 recovery window)** — `InvalidRequestException: You can't create this secret because a secret with this name is already scheduled for deletion` 에러가 나는 경우:
```bash
# 1) 삭제 예약 취소 후 복원
aws secretsmanager restore-secret \
  --secret-id "/llm-gateway/$ENV/app" \
  --region "$AWS_REGION"

# 2) 값 갱신
aws secretsmanager put-secret-value \
  --secret-id "/llm-gateway/$ENV/app" \
  --secret-string "{
    \"virtual_key_encryption_key\": \"$VK_KEY\",
    \"nextauth_secret\": \"$NEXTAUTH_SECRET\",
    \"jwt_jwks_cache_key\": \"$JWKS_CACHE_KEY\"
  }" \
  --region "$AWS_REGION"
```

---

## 2. DB Secret — Terraform 이 자동 관리

`enable_rds_proxy=true` 환경에서는 **DB secret 을 Terraform 이 자동 생성/관리**합니다 (RDS Proxy auth 에 gateway user 를 등록하기 위함). operator 는 `openssl rand` 로 비번을 만들지 않고, `terraform apply` 후 자동 생성된 secret 을 **확인만** 합니다.

### 2.1 Terraform 이 만드는 2 개의 secret

```
/llm-gateway/$ENV/db                 ← Helm ExternalSecret 이 참조
                                          {"password": <gateway pw>,
                                           "master_password": <Aurora managed>}

/llm-gateway/$ENV/db/gateway-user    ← RDS Proxy auth 전용
                                          {"username": "gateway",
                                           "password": <동일 gateway pw>}
```

두 secret 의 `password` 값은 Terraform 의 single `random_password` 리소스에서 파생 — 항상 동기화.

### 2.2 생성 확인

```bash
cd deployment/terraform/environments/$ENV

# Terraform output 에 신규 ARN 이 있는지 확인
terraform output gateway_user_secret_arn
terraform output db_secret_arn

# AWS Console → Secrets Manager 에서도 확인 가능:
aws secretsmanager list-secrets \
  --filters Key=name,Values="/llm-gateway/$ENV/db" \
  --query 'SecretList[].Name' --output table --region "$AWS_REGION"
```

✅ 다음이 보여야 함 (`$ENV=prod` 기준):
```
/llm-gateway/prod/db
/llm-gateway/prod/db/gateway-user
```

### 2.3 (Proxy 안 쓰는 경우만) 수동 DB secret 생성

`enable_rds_proxy=false` 로 내려서 쓰는 환경에서는 Terraform 이 secret 을 만들지 않으므로 operator 가 수동 생성. 이 경우에만 다음 블록 실행:

<details>
<summary>수동 생성 (접어두기)</summary>

```bash
AURORA_SECRET_ARN=$(terraform output -raw aurora_master_user_secret_arn)
AURORA_MASTER_PASSWORD=$(aws secretsmanager get-secret-value \
  --secret-id "$AURORA_SECRET_ARN" \
  --query SecretString --output text | jq -r .password)

GATEWAY_DB_PASSWORD=$(openssl rand -base64 32 | tr -d '/+=' | head -c 32)

aws secretsmanager create-secret \
  --name "/llm-gateway/$ENV/db" \
  --description "LLM Gateway DB credentials (env: $ENV)" \
  --secret-string "{\"password\":\"$GATEWAY_DB_PASSWORD\",\"master_password\":\"$AURORA_MASTER_PASSWORD\"}" \
  --region "$AWS_REGION"
```

</details>

### 2.4 역할 구분 (참고용)

| Secret key | 용도 | 누가 사용 |
|---|---|---|
| `master_password` | Aurora master (`postgres_admin`) 비번 | migration Job (init SQL 실행, application user 생성) |
| `password` | Application user (`gateway`) 비번 | 모든 서비스 + RDS Proxy auth |

migration Job 이 **매 helm install/upgrade 마다** 실행되어:
1. init SQL (`db/init/*.sql`) 실행 — schemas, tables, seed data (idempotent)
2. `gateway` 유저 생성 (없으면) 또는 비번 업데이트 (있으면) — **Terraform 이 생성한 password 사용**
3. 모든 schema 에 필요한 권한 부여 (USAGE, CREATE, SELECT/INSERT/UPDATE/DELETE, DEFAULT PRIVILEGES)
4. `alembic stamp head`

수동 SQL 실행 불필요. 신규 계정에서도 이 구조 그대로 재현.

---

## 3. Redis AUTH 복제

Terraform이 ElastiCache용 AUTH 토큰을 이미 저장해둔 경로:

```bash
cd deployment/terraform/environments/$ENV
REDIS_AUTH_SECRET_ARN=$(terraform output -raw elasticache_auth_token_secret_arn)
```

**ESO가 참조할 경로**(`/llm-gateway/$ENV/redis`)는 **별도로 만들어야** Helm values 와 경로가 일치합니다:

```bash
REDIS_AUTH=$(aws secretsmanager get-secret-value \
  --secret-id "$REDIS_AUTH_SECRET_ARN" \
  --query SecretString --output text)

aws secretsmanager create-secret \
  --name "/llm-gateway/$ENV/redis" \
  --description "LLM Gateway Redis AUTH token (env: $ENV)" \
  --secret-string "{\"password\": \"$REDIS_AUTH\"}" \
  --region "$AWS_REGION"
```

🐛 `ResourceExistsException: The operation failed because the secret /llm-gateway/.../redis already exists.` — 이전 배포의 secret이 남아있는 경우입니다. 두 가지 상황에 따라 처리:

**상황 A: secret이 활성 상태로 존재** — 값만 갱신:
```bash
aws secretsmanager put-secret-value \
  --secret-id "/llm-gateway/$ENV/redis" \
  --secret-string "{\"password\": \"$REDIS_AUTH\"}" \
  --region "$AWS_REGION"
```

**상황 B: `InvalidRequestException: You can't create this secret because a secret with this name is already scheduled for deletion`** — 삭제 예약 상태:
```bash
# 1) 삭제 예약 취소 후 복원
aws secretsmanager restore-secret \
  --secret-id "/llm-gateway/$ENV/redis" --region "$AWS_REGION"

# 2) 값 갱신
aws secretsmanager put-secret-value \
  --secret-id "/llm-gateway/$ENV/redis" \
  --secret-string "{\"password\": \"$REDIS_AUTH\"}" \
  --region "$AWS_REGION"
```

✅ 확인:
```bash
aws secretsmanager list-secrets \
  --filters Key=name,Values="/llm-gateway/$ENV/" \
  --query 'SecretList[].Name' --output table
```

총 **5개** 경로가 보여야 정상입니다:

| 경로 | 생성 주체 | 용도 |
|------|-----------|------|
| `/llm-gateway/$ENV/app` | 수동 | VK 암호화 키, NextAuth, JWKS (ESO 참조) |
| `/llm-gateway/$ENV/redis` | 수동 | Redis AUTH (ESO 참조) |
| `/llm-gateway/$ENV/db` | Terraform | DB 비밀번호 (ESO 참조) |
| `/llm-gateway/$ENV/db/gateway-user` | Terraform | RDS Proxy auth 전용 |
| `/llm-gateway/$ENV/redis/auth_token` | Terraform | ElastiCache AUTH 원본 (`/redis`로 복사한 소스) |

---

## 4. KMS 키 접근 확인 (선택)

ESO가 AUTH 토큰을 읽을 때 KMS 복호화 권한이 필요합니다. Terraform의 IRSA 모듈이 이미 `kms:Decrypt` 를 부여했으나, 커스텀 KMS 키를 쓴다면 해당 키 ARN을 `irsa` 모듈의 `secrets_manager_kms_key_arns` 에 추가해야 합니다.

---

## 5. 체크리스트 (03 단계 완료 시점)

- [ ] `/llm-gateway/$ENV/app` 생성 (operator, 3 key: `virtual_key_encryption_key`, `nextauth_secret`, `jwt_jwks_cache_key`)
- [ ] `/llm-gateway/$ENV/db` 생성 (**Terraform 이 자동 생성** when `enable_rds_proxy=true`, 2 key: `password`, `master_password`)
- [ ] `/llm-gateway/$ENV/db/gateway-user` 생성 (**Terraform 이 자동 생성** when `enable_rds_proxy=true`, RDS Proxy auth 전용)
- [ ] `/llm-gateway/$ENV/redis` 생성 (operator, 1 key: `password`)
- [ ] `aws secretsmanager list-secrets` 로 위 4개 경로 확인 (terraform 이 만든 `/redis/auth_token` 도 보이면 정상, 총 5개):
  ```bash
  aws secretsmanager list-secrets \
    --filters Key=name,Values="/llm-gateway/$ENV/" \
    --query 'SecretList[].Name' --output table
  ```

**여기까지 끝나면 [04-helm-install.md](./04-helm-install.md) 로 진행.** 이후 추가 수동 작업 없음.

---

[👈 02-terraform-apply.md](./02-terraform-apply.md) | [다음: 04-helm-install.md 👉](./04-helm-install.md)
