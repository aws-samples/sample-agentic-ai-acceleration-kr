# 08. Deploy TUI 배포 직후 가이드

**목적**: `deploy-tui` 로 LLM Gateway 를 배포한 **직후** 무엇을 확인하고, 어떤 순서로 운영을 시작하는지.
**소요**: 15~30분
**전제**: `./deployment/scripts/deploy-tui.sh` → **LLM Gateway 배포** 워크플로우가 `[완료]` 로 끝난 상태.

> TUI 는 배포 마지막에 `verify-migration`(= `smoke-test.sh`)을 자동으로 돌린다. 이 문서는 그 자동 검증을 **사람이 다시 확인**하고, VK 발급·Cognito 온보딩·API 실호출로 이어지는 흐름을 한 장으로 정리한 것이다. 상세 troubleshooting 은 각 절 하단의 링크(05·07 문서)를 참조한다.

---

## 0. TUI 가 방금 뭘 했나 (복기)

`build_llm_workflow` 기준, 완료된 스텝은 다음과 같다:

| 스텝 | 한 일 |
|------|-------|
| `build-lambdas` | (chat_db tools 켰을 때만) Lambda 아티팩트 빌드 |
| `tf-init` / `tf-plan` / `tf-apply` | VPC·EKS Fargate·Aurora·Redis·Cognito·ALB IAM 등 인프라 프로비저닝 |
| `install-eks` | 격리 KUBECONFIG(`/tmp/llm-gateway.kubeconfig`)로 helm install + `NEXTAUTH_URL` 패치 |
| `verify-migration` | `smoke-test.sh` (skippable) — Pod readiness·health·`/v1/models`·Ingress 확인 |

→ 이 시점에 **6개 서비스**(gateway-proxy, admin-api, admin-ui, scheduler, notification-worker, cost-recorder-worker)가 EKS 에 떠 있고, ALB 3개(gateway / admin-api / admin-ui)가 프로비저닝 중이다.

> **v2부터**: TUI 는 LLM Gateway 배포가 성공하면 **접속 엔드포인트 3개 + 다음 단계 가이드**를 자동으로 출력한다(ALB 미준비 시 "프로비저닝 중" 안내). 1~2분 뒤 메인 메뉴의 **배포 검증 (Health Check)** 항목을 실행하면 Pod 상태·ALB 라이브 헬스체크·`smoke-test.sh` 를 한 번에 돌린다. 아래 수동 절차는 그 자동 출력을 사람이 재확인하거나 CI 없이 점검할 때 쓴다.

---

## 1. kubectl 컨텍스트부터 맞춘다 (가장 흔한 함정)

TUI 는 `~/.kube/config` 를 **건드리지 않으려고** 배포 내내 격리 KUBECONFIG(`/tmp/llm-gateway.kubeconfig`)를 썼다. 그래서 배포가 끝난 셸에서 그냥 `kubectl` 을 치면 **엉뚱한 클러스터(이 계정의 다른 EKS)** 를 보거나 아무것도 안 보일 수 있다.

배포 후 수동 확인은 아래 둘 중 하나로 한다:

```bash
# 방법 A — TUI 가 쓴 격리 kubeconfig 재사용 (추가 조회 없음)
export KUBECONFIG=/tmp/llm-gateway.kubeconfig
kubectl get pods -n llm-gateway

# 방법 B — 내 기본 kubeconfig 에 컨텍스트를 정식 등록
aws eks update-kubeconfig --region ap-northeast-2 --name llm-gateway
kubectl get pods -n llm-gateway
```

✅ 6개 서비스 Pod 가 전부 `Running` / `Ready` 여야 한다. 아직 `Pending` 이면 Fargate 스케줄링(1~2분) 대기 중일 수 있다.

---

## 2. 스모크 테스트 다시 (사람이 확인)

TUI 안에서 `verify-migration` 이 `⚠ skippable` 로 넘어갔다면(예: 그 시점에 ALB/pod 준비 전) 지금 다시 돌린다:

```bash
export KUBECONFIG=/tmp/llm-gateway.kubeconfig   # 1절과 동일
./deployment/scripts/smoke-test.sh --env dev
```

✅ `PASS: N   FAIL: 0` 이면 통과. FAIL 이 있으면 → [05-smoke-test.md §1](./05-smoke-test.md) 의 로그 확인 절차.

---

## 3. 접속 주소(엔드포인트) 확보

ALB 는 apply 후 1~2분 뒤 hostname 이 채워진다. 3개 엔드포인트를 환경변수로 잡아둔다:

```bash
GATEWAY_URL="http://$(kubectl get ingress llm-gateway-gateway   -n llm-gateway -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')"
ADMIN_API_URL="http://$(kubectl get ingress llm-gateway-admin-api -n llm-gateway -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')"
ADMIN_UI_URL="http://$(kubectl get ingress llm-gateway-admin-ui  -n llm-gateway -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')"

echo "Gateway  : $GATEWAY_URL"
echo "Admin API: $ADMIN_API_URL"
echo "Admin UI : $ADMIN_UI_URL"
```

🐛 값이 비어 있으면 ALB 프로비저닝 대기 중 → 1~2분 뒤 재실행.

---

## 4. 첫 API 실호출 (dev 빠른 경로)

dev 환경 admin-api 는 인증 없이 VK 를 뽑아주는 `/internal/test/issue-key` 를 제공한다(`APP_ENV` 가 개발 환경일 때만 — 그 밖에서는 403). Cognito 온보딩 전에 파이프라인이 살아있는지 30초 만에 확인하는 용도:

```bash
export VK=$(curl -s -X POST "$ADMIN_API_URL/internal/test/issue-key" \
  -H "Content-Type: application/json" -d '{}' | jq -r .virtual_key)
echo "VK length: ${#VK}"          # vk- 로 시작하는 64자+ 면 정상

# 모델 목록
curl -s "$GATEWAY_URL/v1/models" -H "Authorization: Bearer $VK" | jq '.data[].id'

# Bedrock 실호출
curl -s -X POST "$GATEWAY_URL/v1/messages" \
  -H "Authorization: Bearer $VK" -H "Content-Type: application/json" \
  -d '{"model":"claude-sonnet-4-6","max_tokens":50,"messages":[{"role":"user","content":"안녕하세요"}]}' | jq .
```

✅ `/v1/messages` 응답에 `content: [{type:"text", ...}]` 가 오면 **인프라 → 인증 → Bedrock** 경로가 전부 살아있는 것이다. 상세·오류 대응은 → [05-smoke-test.md §2](./05-smoke-test.md).

---

## 5. 실사용 온보딩 (Cognito)

`/internal/test/issue-key` 는 검증 전용이다. **실제 사용자·팀 운영**은 Cognito 로 한다:

1. 첫 admin 사용자 생성 + `ClaudeAdmin` **및** `Claude_*` 팀 그룹 추가
2. Hosted UI 첫 로그인 → 패스워드 변경 (`UserStatus=CONFIRMED`)
3. `gateway-cli login` → `api-key-helper` 로 VK 발급
4. 첫 호출은 팀 budget=$0 이라 **429** (deny by default) → Admin UI 에서 팀 budget 활성화 → 200

전체 절차는 → [07-cognito-onboarding.md](./07-cognito-onboarding.md).

> ⚠️ 자동 생성된 팀은 budget=$0 + HARD_BLOCK 이다. Admin UI(`$ADMIN_UI_URL`)에서 예산을 켜기 전까지 그 팀의 모든 요청은 429 로 막힌다. 이는 버그가 아니라 설계다.

---

## 6. (옵션) Tool Gateway 이어붙이기

검색엔진 Tool Gateway 를 쓸 거라면 지금 TUI 메뉴에서 **Tool Gateway 배포**(us-east-1) 를 이어서 돌린다. Tool GW 의 dashboard-env 연동은 방금 뜬 admin-ui 에 의존하므로 **LLM → Tool 순서가 맞다**. Tool GW 는 non-fatal 애드온이라 실패해도 LLM Gateway 운영에는 영향이 없다.

---

## ✅ 배포 직후 체크리스트

- [ ] `KUBECONFIG` 를 격리 파일 또는 `aws eks update-kubeconfig` 로 맞췄다
- [ ] 6개 서비스 Pod 전부 Ready
- [ ] `smoke-test.sh` FAIL 0
- [ ] Gateway / Admin API / Admin UI ALB hostname 3개 확보
- [ ] `/internal/test/issue-key` → `/v1/messages` 실호출 200
- [ ] (실사용) Cognito admin 온보딩 + 팀 budget 활성화
- [ ] (옵션) Tool Gateway 배포

---

[👈 05-smoke-test.md](./05-smoke-test.md) · [07-cognito-onboarding.md](./07-cognito-onboarding.md) · [troubleshooting.md](./troubleshooting.md)
