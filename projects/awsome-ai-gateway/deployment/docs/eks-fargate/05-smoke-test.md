# 05. Smoke Test — 배포 후 E2E 검증

**목적**: 배포된 시스템이 실제로 동작하는지 확인.
**소요**: 10분

---

## 1. 자동 스모크 테스트

```bash
cd /path/to/LLM-Gateway-Vanilla
./deployment/scripts/smoke-test.sh --namespace llm-gateway
```

검사 항목:
1. 6개 서비스의 Pod readiness (migration Job은 성공 후 자동 삭제)
2. 각 서비스의 `/health` 엔드포인트
3. `/v1/models` 응답
4. Ingress LoadBalancer 주소 확보

✅ 성공 시:
```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  PASS: 12   FAIL: 0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

🐛 FAIL 항목 있으면 해당 서비스 로그 확인:
```bash
kubectl logs -l app.kubernetes.io/component=<component-name> -n llm-gateway --tail=100
```

---

## 1.5 HPA 메트릭 가용 확인

`install-eks.sh` 가 설치한 `prometheus-adapter` 가 `metrics.k8s.io` API 를 서빙하는지 확인. 실패 시 HPA 가 `<unknown>` 으로 멈춰 모든 Deployment 가 minReplicas 에 고정됨.

```bash
# 1. metrics.k8s.io API 응답
kubectl get --raw '/apis/metrics.k8s.io/v1beta1/pods' | head -c 80

# 2. kubectl top 동작 (prometheus collect window 때문에 첫 배포 후 3~5분 기다려야 할 수 있음)
kubectl top pod -n llm-gateway

# 3. HPA TARGETS 가 실제 수치로 표시 (cpu: <unknown>/65% 아님)
kubectl -n llm-gateway get hpa
```

✅ HPA 컬럼이 `cpu: 2%/65%` 같은 숫자로 보이면 정상.
🐛 `<unknown>` 이 계속 유지되면 [troubleshooting.md — HPA 가 `<unknown>/65%` 로 멈춰있음](./troubleshooting.md#hpa-가-unknown65-로-멈춰있음) 참조.

---

## 2. 수동 API 검증

> ⚠️ **순서 중요**: `/v1/models`, `/v1/messages` 는 전부 **Virtual Key (VK) 인증 필수** 엔드포인트입니다.
> 인증 없이 호출 시 `401 Unauthorized` 가 나오는 것이 정상입니다 (middleware 가 동작하는 증거).
> 따라서 2.1 에서 **먼저 VK 를 발급** 받고, 2.2 부터 그 VK 로 API 를 호출합니다.

### 2.1 Virtual Key 발급 (API 호출 용)

#### 2.1.1 URL 변수 설정

**방식 A (도메인 없음)** — ALB DNS 로 직접:
```bash
GATEWAY_URL="http://$(kubectl get ingress llm-gateway-gateway -n llm-gateway \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')"
ADMIN_API_URL="http://$(kubectl get ingress llm-gateway-admin-api -n llm-gateway \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')"
ADMIN_UI_URL="http://$(kubectl get ingress llm-gateway-admin-ui -n llm-gateway \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')"

echo "Gateway  : $GATEWAY_URL"
echo "Admin API: $ADMIN_API_URL"
echo "Admin UI : $ADMIN_UI_URL"
```

**방식 B (도메인 있음)**:
```bash
GATEWAY_URL="https://gateway-dev.llm-gateway.mycompany.com"
ADMIN_API_URL="https://admin-api-dev.llm-gateway.mycompany.com"
ADMIN_UI_URL="https://admin-dev.llm-gateway.mycompany.com"
```

#### 2.1.2 VK 발급 (dev 전용 엔드포인트)

admin-api 는 dev 환경에서 **인증 없이 바로 VK 를 발급**하는 `/internal/test/issue-key` endpoint 를 제공합니다 (prod 에서는 비활성화됨). 이 endpoint 가 smoke test 전용 가장 빠른 경로:

```bash
VK_RESPONSE=$(curl -s -X POST "$ADMIN_API_URL/internal/test/issue-key" \
  -H "Content-Type: application/json" -d '{}')

echo "$VK_RESPONSE" | jq .

export VK=$(echo "$VK_RESPONSE" | jq -r .virtual_key)
echo "VK: ${VK:0:30}...    length: ${#VK}"
```

✅ 응답 예시:
```json
{
  "virtual_key": "vk-5960e241fe068a7a9a16c66a0d8c9fb954c509bd7b120ee04621f0c83a44e50e",
  "key_id": "fa52714c-b127-4153-bada-3da6cd0b06bc",
  "user_id": "1b440179-...",
  "team_id": "00000000-0000-4000-a000-000000000003",
  "email": "test@example.com",
  "budget_usd": "1000.0",
  "expires_at": "2026-07-22T...",
  "gateway_endpoint": "http://localhost:8000"
}
```

✅ `VK` 변수에 `vk-` 로 시작하는 64자+ 문자열이 저장돼야 정상. `length: 4` (= `null`) 이면 endpoint 가 비활성화됐거나 dev 모드 아님.

🐛 `404 Not Found` — admin-api 가 prod 빌드로 돌고 있어 `/internal/*` endpoint 가 숨김 상태. values 에서 `DEV_MODE=true` 또는 빌드 플래그 확인.

---

### 2.2 /v1/models 조회 (VK 인증)

이제 2.1 에서 받은 `$VK` 로 모델 목록 조회:

```bash
curl -s "$GATEWAY_URL/v1/models" \
  -H "Authorization: Bearer $VK" | jq .
```

✅ JSON 응답에 `data: [...]` 배열 존재 (실제 seed 된 alias):
```json
{
  "data": [
    {
      "type": "model",
      "id": "claude-haiku-4-5-20251001",
      "display_name": "Claude Haiku 4.5 (Global)",
      "object": "model",
      "owned_by": "gateway",
      "provider": "BEDROCK",
      "provider_model_id": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
      "pricing": { "input_per_1k_usd": "0.001000", "output_per_1k_usd": "0.005000", "currency": "USD" }
    },
    {
      "type": "model",
      "id": "claude-sonnet-4-6",
      "display_name": "Claude Sonnet 4.6 default (Global)",
      "object": "model",
      "owned_by": "gateway",
      "provider": "BEDROCK",
      "provider_model_id": "global.anthropic.claude-sonnet-4-6",
      "pricing": { "input_per_1k_usd": "0.003000", "output_per_1k_usd": "0.015000", "currency": "USD" }
    }
  ],
  "has_more": false,
  "object": "list"
}
```

🐛 `401 Unauthorized` — `$VK` 가 비어있거나 만료됨. 2.1 재실행.

🐛 빈 배열 — admin-api 에 모델이 seed 되지 않음:
```bash
kubectl exec -it deploy/llm-gateway-admin-api -n llm-gateway -- \
  python -c "from app.db import SessionLocal; from app.models import ModelAlias; \
             db = SessionLocal(); print(db.query(ModelAlias).count())"
```
0 이면 migration seed 데이터가 안 들어간 것. `db/init/03_seed_data.sql` 확인.

---

### 2.3 Admin UI 접속

```bash
open "$ADMIN_UI_URL"
```

✅ **로그인 페이지** 가 떠야 함. dev 환경에선 dev login 으로 로그인 가능.

---

### 2.4 Bedrock 실호출 E2E

```bash
# $VK 는 2.1 에서 발급받은 값이 이미 셸 변수에 있음

curl -X POST "$GATEWAY_URL/v1/messages" \
  -H "Authorization: Bearer $VK" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4-6",
    "max_tokens": 50,
    "messages": [{"role": "user", "content": "안녕하세요"}]
  }'
```

사용 가능한 Bedrock 모델 alias: `claude-haiku-4-5-20251001`, `claude-sonnet-4-6`, `claude-sonnet-4-6[1m]`, `global.anthropic.claude-opus-4-6-v1`, `claude-opus-4-7`

✅ 응답에 `content: [{type: "text", text: "안녕하세요..."}]` 포함.

🐛 403 — VK가 만료됐거나 `bedrock_allowed_model_arns` 에 해당 모델이 없음.

🐛 500 — gateway-proxy 로그 확인:
```bash
kubectl logs -l app.kubernetes.io/component=gateway-proxy \
  -n llm-gateway --tail=100
```

---

### 2.5 비용 기록 확인 (cost-recorder-worker)

요청 직후 Aurora에 usage_log가 INSERT됐는지 확인:

```bash
kubectl exec -n llm-gateway deploy/llm-gateway-admin-api -c admin-api -- python -c "
import asyncio, os
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
async def main():
    e = create_async_engine(os.environ['DATABASE_URL'])
    async with e.begin() as c:
        r = await c.execute(text('SELECT request_id, model_alias, input_tokens, output_tokens, cost_usd, requested_at FROM usage.usage_logs ORDER BY requested_at DESC LIMIT 5'))
        for row in r.fetchall():
            print(row)
asyncio.run(main())
"
```

✅ 최근 요청이 row로 보여야 함.

⚠️ 테이블에 row 없음 → cost-recorder-worker 로그 확인:
```bash
kubectl logs -l app.kubernetes.io/component=cost-recorder-worker \
  -n llm-gateway --tail=50
```

Redis Stream 소비 로그가 보여야 합니다.

---

---

### 참고: 이메일 발송 E2E

현재 `email.provider: "mock"` 상태이므로 실제 이메일이 발송되지 않습니다. 내부 메일 API 연동 후 아래 절차로 테스트하세요:

1. Helm values에서 `notificationWorker.email.provider: "internal_api"` + `internalApi.url` 설정 후 `helm upgrade`
2. Redis pub/sub로 예산 알림 트리거:
   ```bash
   kubectl exec -n llm-gateway deploy/llm-gateway-cost-recorder-worker -c cost-recorder-worker -- \
     python -c "
   import redis, json, os
   r = redis.Redis.from_url(os.environ['REDIS_URL'])
   r.publish('notifications:budget', json.dumps({
       'user_email': 'test-user@company.com',
       'budget_pct': 80,
       'used_usd': 800,
       'limit_usd': 1000
   }))
   "
   ```
3. notification-worker 로그에서 `Email sent` 확인 + 실제 메일 수신 확인

---

## 3. 체크리스트

- [ ] `smoke-test.sh` 에서 FAIL 0 (6개 서비스 Pod Ready + Health OK)
- [ ] `/v1/models` JSON 응답 정상 (VK 인증 포함)
- [ ] Admin UI 로그인 페이지 렌더
- [ ] Virtual Key 발급 성공 (`/internal/test/issue-key`)
- [ ] `/v1/messages` 실호출 → Bedrock 응답
- [ ] `usage.usage_logs` 테이블에 row INSERT 확인

---

[👈 04-helm-install.md](./04-helm-install.md) | [다음: 06-upgrade-rollback.md 👉](./06-upgrade-rollback.md)
