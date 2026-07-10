# 07. Cognito (OIDC) 사용자 / 팀 온보딩

**목적**: 배포 직후 Cognito User Pool 에 첫 admin 등록, 그룹 운영 정책 정리, OIDC 흐름 end-to-end 검증.
**소요**: 30~45분
**전제**: [02-terraform-apply.md](./02-terraform-apply.md), [04-helm-install.md](./04-helm-install.md), [05-smoke-test.md](./05-smoke-test.md) 완료.

---

## 배경 — 우리 OIDC 모델

| 무엇 | 누가 결정 | 어디서 |
|------|--------|------|
| 사용자 신원 (있는 사람인가) | **Cognito** (admin 이 콘솔에서 생성) | AWS Console / CLI |
| 사용자가 어느 팀인가 | **Cognito Groups** (admin 이 그룹에 추가) | 동일 |
| 팀 자체의 budget / 모델 정책 | **Gateway admin UI** | gateway 운영자 |
| 사용자 / 팀 DB row 생성 | **자동** — 첫 OIDC 로그인 시 admin-api 가 자동 INSERT | (자동) |

→ admin 이 Cognito 에 사용자 추가 + 그룹 매핑하면, 사용자가 첫 호출 시 우리 DB 에 자동 등록됩니다. 별도 사용자 추가 작업 불필요.

> **주의**: 자동 생성된 팀은 budget=$0 + HARD_BLOCK 상태입니다. admin이 Admin UI에서 팀 예산을 활성화하기 전까지 해당 팀의 모든 API 요청은 429로 차단됩니다.

---

## 0. terraform output 확인

배포된 환경의 OIDC 정보 추출:

```bash
cd deployment/terraform/environments/${ENV}
terraform output cognito_user_pool_id
terraform output cognito_client_id
terraform output cognito_issuer_url
terraform output cognito_hosted_ui_domain
terraform output cognito_groups
```

이후 단계에서 이 값들 사용하므로 환경변수로 캡처:

```bash
export USER_POOL_ID=$(terraform output -raw cognito_user_pool_id)
export CLIENT_ID=$(terraform output -raw cognito_client_id)
export ISSUER_URL=$(terraform output -raw cognito_issuer_url)
export HOSTED_UI=$(terraform output -raw cognito_hosted_ui_domain)
echo "Issuer:     $ISSUER_URL"
echo "Hosted UI:  https://$HOSTED_UI"
```

✅ **확인**: 모든 값이 비어있지 않아야 함.

---

## 1. 첫 Admin 사용자 생성 (Cognito)

운영자 본인을 Cognito 에 등록 + ClaudeAdmin 그룹에 추가.

### 1.1 사용자 생성

```bash
ADMIN_EMAIL="admin@example.com"   # ← 본인 이메일
TEMP_PASSWORD='Temp_Pass-1234!'   # 12자 이상, 대소문자+숫자

aws cognito-idp admin-create-user \
  --user-pool-id "$USER_POOL_ID" \
  --username "$ADMIN_EMAIL" \
  --user-attributes Name=email,Value="$ADMIN_EMAIL" Name=email_verified,Value=true \
  --temporary-password "$TEMP_PASSWORD" \
  --message-action SUPPRESS
```

✅ **확인**: 응답 JSON 의 `User.UserStatus` 가 `FORCE_CHANGE_PASSWORD` 여야 정상.

### 1.2 그룹 추가 (ClaudeAdmin + 팀 그룹)

Admin 권한과 팀 소속 두 가지 모두 필요합니다:

```bash
# Admin 권한 부여
aws cognito-idp admin-add-user-to-group \
  --user-pool-id "$USER_POOL_ID" \
  --username "$ADMIN_EMAIL" \
  --group-name ClaudeAdmin

# 팀 그룹에도 추가 (필수 — 없으면 VK 발급 시 403)
aws cognito-idp admin-add-user-to-group \
  --user-pool-id "$USER_POOL_ID" \
  --username "$ADMIN_EMAIL" \
  --group-name "Claude_Backend"
```

### 1.3 첫 로그인 + 패스워드 변경

브라우저에서 Hosted UI 로 접속:

```bash
echo "https://$HOSTED_UI/login?client_id=$CLIENT_ID&response_type=code&scope=openid+profile+email&redirect_uri=http://localhost:8090/callback"
```

→ 위 URL 복사 → 브라우저 → `$ADMIN_EMAIL` / `$TEMP_PASSWORD` 입력 → **새 패스워드** 설정.

⚠️ **localhost:8090/callback 으로 리다이렉트되면 "사이트에 연결할 수 없음" 오류**: 정상. callback 서버는 실제 사용 시 `gateway-cli login` 이 띄움. **여기서 중요한 건 패스워드 변경 단계 통과**.

✅ **확인**:
```bash
aws cognito-idp admin-get-user \
  --user-pool-id "$USER_POOL_ID" \
  --username "$ADMIN_EMAIL" \
  --query 'UserStatus'
# "CONFIRMED" 출력되면 패스워드 변경 완료
```

---

## 2. 일반 사용자 / 팀 매핑 (옵션, 검증 시 1명만)

위 admin 계정은 `ClaudeAdmin` 만 있어서 `OIDC_GROUP_PREFIX` (`Claude_`) prefix 매칭 대상이 아닙니다. **admin이라도 `Claude_*` 팀 그룹에 소속되어 있지 않으면 VK 발급 시 403 `no_matching_team_group` 에러가 발생합니다.** 따라서 admin 계정도 반드시 팀 그룹에 추가해야 합니다.

**팀 매핑까지 검증하려면** 일반 사용자 1명을 `Claude_Backend` 같은 그룹에 추가:

```bash
DEV_EMAIL="dev@example.com"
aws cognito-idp admin-create-user \
  --user-pool-id "$USER_POOL_ID" \
  --username "$DEV_EMAIL" \
  --user-attributes Name=email,Value="$DEV_EMAIL" Name=email_verified,Value=true \
  --temporary-password "$TEMP_PASSWORD" \
  --message-action SUPPRESS

aws cognito-idp admin-add-user-to-group \
  --user-pool-id "$USER_POOL_ID" \
  --username "$DEV_EMAIL" \
  --group-name Claude_Backend
```

이 사용자도 위 1.3 절차로 패스워드 변경.

---

## 3. End-to-End 검증 (gateway-cli)

배포된 admin-api / gateway-proxy 를 통한 전체 흐름 검증.

### 3.1 admin-api / gateway-proxy 외부 endpoint 확인

```bash
kubectl get ingress -n llm-gateway
# admin-api / gateway-proxy ingress 의 ADDRESS 컬럼 = ALB hostname

export ADMIN_API_URL="http://$(kubectl get ingress llm-gateway-admin-api -n llm-gateway -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')"
export ANTHROPIC_BASE_URL="http://$(kubectl get ingress llm-gateway-gateway -n llm-gateway -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')"
```

### 3.2 gateway-cli 설치

dev 환경의 운영자 PC 에서:

```bash
# 소스 트리에서 (정식 PyInstaller 빌드는 별도)
cd /path/to/LLM-Gateway-Vanilla/gateway-cli
python3.11 -m venv .venv
.venv/bin/pip install -e .

# OIDC 환경변수
export OIDC_ISSUER_URL="$ISSUER_URL"
export OIDC_CLIENT_ID="$CLIENT_ID"
export OIDC_REDIRECT_PORT=8090
export ADMIN_API_URL="$ADMIN_API_URL"
```

### 3.3 OIDC 로그인

```bash
.venv/bin/gateway-cli login
```

→ 브라우저 자동 → Cognito Hosted UI → admin@example.com 로 로그인.
→ "Login complete" 페이지 → 터미널에 `Login successful` 출력.

> 브라우저에 이전 Cognito 세션이 남아있으면 이메일 입력 없이 자동 로그인될 수 있습니다. 다른 계정으로 테스트하려면 시크릿/프라이빗 창을 사용하세요.

✅ **확인**:
```bash
ls -la ~/.gateway-cli/oidc-tokens.json
# -rw-------  1 ...  ...  ...  oidc-tokens.json  (mode 0600)
```

### 3.4 VK 발급

```bash
.venv/bin/api-key-helper
# vk-... 출력
```

✅ **확인 — DB**:

```bash
# bastion / kubectl exec 로 admin-api 컨테이너 접근
kubectl exec -n llm-gateway deploy/admin-api -- \
  python -c "
from sqlalchemy import create_engine, text
import os
e = create_engine(os.environ['DATABASE_URL'].replace('+asyncpg',''))
with e.connect() as c:
    rows = c.execute(text('SELECT email, role, provider FROM auth.users WHERE provider LIKE \\'oidc%\\''))
    for r in rows: print(r)
"
```

→ `admin@example.com | ADMIN | oidc:cognito` 출력되면 자동 프로비저닝 + admin 부트스트랩 정상.

### 3.5 gateway-proxy 호출 (Deny → Approve)

```bash
VK=$(.venv/bin/api-key-helper)

# 1) 첫 호출: budget=$0 으로 차단 기대 (admin 의 default team)
curl -sS -X POST "$ANTHROPIC_BASE_URL/v1/messages" \
  -H "Authorization: Bearer $VK" \
  -H "Content-Type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -d '{"model":"claude-sonnet-4-6","messages":[{"role":"user","content":"hi"}],"max_tokens":50}'
# → 429 budget_exceeded 기대
```

→ admin UI 에서 default team 의 budget 활성화:
- admin UI URL: `https://<admin-ui-ingress>/`
- ClaudeAdmin 멤버이므로 자동 admin 권한
- Teams 메뉴 → "Default Team" → budget 변경 (예: $1000)

다시 호출:
```bash
# 2) budget 활성화 후 정상 호출
curl -sS ... # 위 명령 다시 → 200 응답
```

✅ **end-to-end 통과**.

---

## 4. 운영 중 사용자 추가

신규 사용자가 들어올 때 admin 의 액션:

```bash
NEW_EMAIL="newdev@example.com"
TEAM_GROUP="Claude_Backend"   # 사용자가 속할 팀

aws cognito-idp admin-create-user \
  --user-pool-id "$USER_POOL_ID" \
  --username "$NEW_EMAIL" \
  --user-attributes Name=email,Value="$NEW_EMAIL" Name=email_verified,Value=true \
  --temporary-password 'Temp_Pass-1234!'

aws cognito-idp admin-add-user-to-group \
  --user-pool-id "$USER_POOL_ID" \
  --username "$NEW_EMAIL" \
  --group-name "$TEAM_GROUP"

# 사용자에게 임시 패스워드 + Hosted UI URL 전달
```

→ 사용자가 `gateway-cli login` 후 Claude Code 호출 시:
- admin-api 가 user 자동 INSERT (email, sso_subject, provider='oidc:cognito')
- `Claude_Backend` 매칭 → Backend Team 자동 생성/배정 (Default Department 하위)
- 첫 호출은 budget=$0 차단 → admin 이 admin UI 에서 Backend Team 의 budget 활성화

---

## 5. 운영 중 새 팀 추가

새 그룹 추가하는 두 가지 방법:

### 방법 A: terraform (IaC, 권장)

```bash
cd deployment/terraform/environments/${ENV}
vim terraform.tfvars
# cognito_groups 리스트에 "Claude_NewTeam" 추가

terraform apply
```

### 방법 B: AWS CLI 직접 (운영 중 즉시 추가)

```bash
aws cognito-idp create-group \
  --user-pool-id "$USER_POOL_ID" \
  --group-name "Claude_NewTeam" \
  --description "LLM Gateway team mapping"
```

⚠️ 방법 B 사용 후 다음 `terraform apply` 시 drift 가 생길 수 있음. 가능하면 다음 변경 시 tfvars 도 같이 업데이트.

---

## 6. Hosted UI vs 다른 IDP 로 교체

고객사 환경에서 **Cognito 가 아닌 다른 IDP (Okta / Azure AD / IC)** 사용 시:

1. terraform 의 `module.cognito` 주석 처리 (또는 환경 변수로 비활성화)
2. helm values 의 `adminApi.oidc.*` 를 고객 IDP 정보로 교체:
   ```yaml
   adminApi:
     oidc:
       issuerUrl: "https://customer-okta.example.com/oauth2/default"
       providerName: "oidc:okta"
       groupsClaim: "groups"  # IDP 별 다름
   ```
3. 사용자 그룹 명명 규칙은 동일 (`Claude_<team>`).
4. helm upgrade.

→ 코드 변경 0건. 환경변수만 교체.

---

## ✅ 완료 체크리스트

- [ ] terraform output 의 cognito_* 값 모두 정상
- [ ] Cognito 에 admin 사용자 생성 + ClaudeAdmin 그룹 + `Claude_*` 팀 그룹 멤버
- [ ] admin 첫 로그인 후 패스워드 변경 (UserStatus=CONFIRMED)
- [ ] gateway-cli login → JWT 캐시 (mode 0600)
- [ ] api-key-helper → VK 발급 + DB 의 users 테이블에 provider='oidc:cognito' 행 INSERT
- [ ] gateway-proxy 호출 → 429 budget_exceeded (정상, deny by default)
- [ ] admin UI 에서 budget 활성화 → 200 응답

이 체크리스트가 모두 통과하면 OIDC 흐름이 production 에서 정상 동작.

다음: 일상 운영 → [troubleshooting.md](./troubleshooting.md) 의 OIDC 섹션 참고.
