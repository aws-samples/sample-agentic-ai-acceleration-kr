# LLM Gateway — Architecture & Data Flows

> **메인 배포 계정: `123456789012` (ap-northeast-2, dev/prod EKS Fargate).** 게이트웨이 전 서비스 + Aurora + AgentCore Runtime + AgentCore 웹서치 Gateway + ECR 가 이 계정에 있고 `gateway-proxy` IRSA 도 이 계정에 속한다.
> **3-client × 멀티계정 백엔드:** `claude-code` → **345678901234** cross-account Bedrock **NATIVE** (실패 시 859 in-account 투명 폴백), `codex` → **859 in-account** Mantle GPT-5.5 (us-east-2), `cowork` → **234567890123** cross-account Mantle Opus 4.8 (도쿄 ap-northeast-1).
> Last updated: 2026-07-09 (3-client 라우팅 + claude-code→374 cross-account native 컷오버(0022) + 서버사이드 웹서치(0021) + resilience 반영).

이 문서는 **as-built** 시스템을 기술한다: 배포된 컴포넌트, 데이터 스토어, 인증 모델, 3-client × 멀티계정 데이터플로우(요청/비용·사용량/client 태그/cross-account/웹서치), 그리고 resilience/scale 특성. 기능별 근거는 `deepdive.md`, 부하 분석은 `devlog_websearch.md §부하 분석`, 사용자 온보딩은 `guides/QUICKSTART.md` 참조.

---

## 1. System at a glance

LLM Gateway 는 사내 코딩 에이전트 **3-client** — **Claude Code**, **Codex**, **Cowork** — 에게 단일 OIDC 인증 진입점을 제공한다. Virtual Key(VK)를 발급하고, 사용자/팀/앱(client)별 예산 · rate limit · 모델 접근 제어를 집행하며, 사용량/비용을 기록하고, 서버사이드 웹서치와 자연어 BI 어시스턴트를 제공한다.

두 개의 plane 을 명확히 분리한다:

- **Data plane** — `gateway-proxy` 가 client 별로 알맞은 백엔드로 inference 를 프록시한다. 지연에 민감하고 QPS 가 높다.
- **Control plane** — `admin-ui` → `admin-api` 로 VK 발급, 거버넌스 CRUD, 대시보드/분석, BI 챗. 사람 대상, 저 QPS.

두 방언(dialect) × 두 백엔드 종류가 client 별로 갈린다:

| client | 진입 경로 | 백엔드 종류 | 대상 계정/리전 | 방언 |
|---|---|---|---|---|
| **claude-code** | `/v1/messages` | Bedrock **native** (boto3 `invoke_model`) | 345678901234 / ap-northeast-2 (cross-account, 실패 시 859 폴백) | Anthropic Messages |
| **codex** | `/v1/responses` | Bedrock **Mantle** (async httpx bearer, GPT-5.5) | 859 in-account / us-east-2 (오하이오) | OpenAI Responses |
| **cowork** | `/v1/messages` | Bedrock **Mantle** (async httpx bearer, Opus 4.8) | 234567890123 / ap-northeast-1 (도쿄, cross-account) | Anthropic Messages |

```
                          ┌──────────────────── CONTROL PLANE ────────────────────┐
  Admin (browser, OIDC) ─→│ admin-ui (Next.js) ──→ admin-api (FastAPI) ──→ Aurora │
                          │                          └─→ AgentCore Runtime (BI) ─┐│
                          └──────────────────────────────────────────────────┐  ││
                                                                              ▼  ▼│
                          ┌──────────────────── DATA PLANE ──────────────────────┐│
  Claude Code (cli)  ─VK─→│ gateway-proxy (FastAPI)                               ││
  Codex (codex_cli)  ─VK─→│  OTel→ClientId→Auth→ClientAuthZ→Budget→Downgrade→     ││
  Cowork (desktop3p) ─VK─→│  RateLimit→HeaderInjector→StateInjection→router       ││
                          │   _select_backend(client) via routing_profiles        ││
                          │     ├─ claude-code → Bedrock NATIVE 374 (STS assume,   ││
                          │     │                실패 시 859 in-account 투명 폴백)  ││
                          │     ├─ codex       → Mantle OpenAI Responses 859 (u-e-2)││
                          │     └─ cowork      → Mantle Anthropic Msgs 905 (Tokyo) ││
                          │   (opt) 웹서치 루프: web_search 주입→인터셉트→AgentCore  ││
                          │   cost finalize ─XADD→ cost:stream (Valkey)            ││
                          └───────────────────────────────────────────────────┬──┘│
                                                                               ▼   ▼
                          cost-recorder-worker ──→ usage_logs / budget_usages (Aurora)
                          notification-worker  ──→ email (SES/SMTP/mock)   scheduler (cron)
```

라우팅은 **데이터 드리븐**이다: client 별 백엔드는 코드가 아니라 `model.routing_profiles` 테이블 row 로 결정되고, `RoutingProfileLoader` 가 Redis(TTL 300s)에 캐시한다. 미등록/disabled client 는 기본 Bedrock native 경로로 폴백한다.

---

## 2. Deployed components (Helm chart `llm-gateway`)

Helm 이 배포하는 워크로드는 **6 long-running Deployment + 1 migration Job** 이다. 각 워크로드는 자체 ServiceAccount + **IRSA**(워크로드별 least-privilege AWS role)를 갖는다. long-running 서비스는 HPA + PDB + Service 를, 워커는 Service 를 생략, `scheduler` 는 singleton(HPA/PDB 없음)이다.

| Workload | Plane | 책임 |
|---|---|---|
| **gateway-proxy** | data | VK 인증, client 식별, **3-client 백엔드 라우팅(Bedrock native / Mantle)**, rate-limit + budget 집행, 모델 다운그레이드, inference 프록시, (opt) 서버사이드 웹서치, cost finalize (XADD `cost:stream`) |
| **admin-api** | control | VK 발급, OIDC→VK 교환, model/team/budget/rate-limit CRUD, **대시보드 / 분석 / 모니터링**, BI 챗 프록시(AgentCore Runtime) |
| **admin-ui** | control | Next.js 14 관리 대시보드 + `/chat` BI UI; server-only API 프록시 라우트 |
| **cost-recorder-worker** | data (async) | `XREADGROUP` `cost:stream` → bulk-INSERT `usage_logs` + UPSERT `budget_usages` + Redis daily counter + threshold pub/sub; daily roll-up → `daily_aggregates` |
| **notification-worker** | control (async) | `notifications:*` pub/sub 구독 → email senders (SES/SMTP/internal-api/mock; dev·prod 기본 `mock` 미발송) |
| **scheduler** | control (cron) | APScheduler singleton(`replicaCount:1` 고정). **admin-api 이미지 재사용**(command=`python -m app.scheduler.main`). 잡: ROI 집계(`aggregate_usage`) + VK 만료 정리(`expire_virtual_keys`). (daily_usage_aggregation 은 cost-recorder-worker 로 이관됨) |
| **migration** (Job) | — | Alembic `upgrade head`(현재 head `0022`), Helm pre-install/pre-upgrade hook(weight -5), `transaction_per_migration=True` |

**Helm 워크로드가 아님:** `admin-chat-agent`(agents-as-tools Strands BI 어시스턴트)는 **Bedrock AgentCore Runtime**(arm64 microVM, container→ECR→`CreateAgentRuntime`)에 배포되고, admin-api 가 `boto3 bedrock-agentcore.invoke_agent_runtime`(SigV4/IAM)로 호출한다. 차트에는 `AGENTCORE_RUNTIME_ARN` env + chat staging S3 버킷으로만 연결된다. `query_db`/`get_schema` Lambda 는 read-only `gateway_chat_reader` DB role 로 실행된다.

**Infra:** EKS **Fargate**(Auto Mode 아님, Fargate Profile 기반, EKS 1.30). Aurora PostgreSQL + ElastiCache/Valkey 는 외부 관리형(`postgresql.enabled=false`, `redis.enabled=false`). ALB Ingress; External Secrets Operator(ESO)가 Secrets Manager → K8s Secret 동기(refreshInterval dev 1h / prod 30m). Terraform 모듈: VPC, EKS Fargate, Aurora, ElastiCache, Cognito, IRSA, ALB, ESO, agentcore-runtime.

---

## 3. Data stores

### 3.1 Aurora PostgreSQL — schemas

| Schema | 주요 테이블 |
|---|---|
| `auth` | `organizations`, `departments`, `teams`, `users`(role ADMIN/TEAM_LEADER/DEVELOPER, `sso_subject` UNIQUE, `provider` sts\|oidc:cognito), `virtual_keys`(`key_value_encrypted` BYTEA, `key_prefix`, status, `expires_at`), `rotation_policies`, `admin_jwt_configs` |
| `model` | `model_aliases`(alias PK, provider, provider_model_id, endpoint_url, api_format), `model_pricings`(input/output/cache_5m/cache_1h/cache_read per-1k, 시계열 버전), `rate_limit_configs`(rpm/tpm/cpm/cph), `user_allowed_models` · `team_allowed_models`(allow-list; 0 rows = allow-all), **`routing_profiles`**(client→backend, account_role_arn, external_id, region, default_model, `web_search_enabled`) |
| `budget` | `budget_configs`(scope USER/TEAM, policy HARD_BLOCK/SOFT_WARNING/THROTTLE, per-app `app_clients`), `budget_usages`(period YYYY-MM, used_usd, threshold_notified_pcts), `downgrade_policies` |
| `usage` | **`usage_logs`**(per-request facts: tokens, cost, `is_streaming`, `downgraded_from`, `sso_subject`, `bedrock_request_id`, **`client`**, web_search_count), `daily_aggregates`(KST roll-up), `request_traces`, `roi_aggregations`, `productivity_events`, `git_events` |
| `audit` | `audit_logs`, `cache_invalidation_failures`, `chat_agent_queries` |
| `notification` | `notification_configs`, `notification_logs` |
| `chat_agent` | `sessions`, `messages`, `schema_embeddings`(pgvector 1024-dim HNSW), `golden_examples` |

**마이그레이션 체인:** head 는 **`0022`** 이고 `0001→0022` 가 끊김 없이 연결된다. 라우팅 관련 순서:
`0008`(Mantle enums) → `0009`(cowork routing/cowork-opus) → `0016`(codex enums) → `0017`(codex routing) → `0018`(client CHECK 확장 → `claude-code`/`cowork`/`codex`) → `0021`(web_search + claude-code invoke row 최초 생성, backend=invoke, default_model NULL) → `0022`(그 row 에 374 `account_role_arn`/`external_id`/`region` UPDATE).

### 3.2 Valkey/Redis — key namespaces

| Namespace | 용도 |
|---|---|
| `rl:config:{scope}:{sid}:{model}`, `rl:{prefix}:{entity}:{model}:{metric}` | rate-limit config + RPM/TPM 토큰 버킷 |
| `rl:cost:{user\|team}:{id}:cpm:{ts}` / `:cph:{ts}` | cost-per-minute/hour 윈도우 |
| `budget:config:{user\|team}:{id}`, `budget:{user\|team}:{id}:{period}`, `budget:user:{uid}:{client}:{period}` | budget config + live USD 카운터(scope별 + **per-app(client)**) |
| `key:vk:{key_hash}` (VK→user_id 매핑), `key:cache:vk:{key_hash}` (AuthContext 캐시, TTL 300s) | 미스 시 무효키 → PermissionError. 무효화 시 두 키 함께 delete |
| `cost:stream` | **Redis Stream**(MAXLEN ~100k), cost/usage 핸드오프, consumer group |
| `model:{alias}`, `model:{provider_model_id}`, `model:list` | model-catalog 캐시(TTL 300s) |
| `routing_profile:{client}` | routing-profile 캐시(TTL 300s). 롤백 시 이 키 플러시 필요 |
| `usage:daily:user:{uid}:{date}:cost/tokens/models` | live daily 카운터 |
| `notifications:{budget,key,security,system}` | pub/sub 채널 |

> **CROSSSLOT 대응:** USER/TEAM budget 키는 hash tag 가 달라 단일 Lua 로 못 묶는다 → scope별 2회 eval 후 Python 에서 합친다. per-app 예산은 3번째 eval. Redis 실패 시 `_check_budget_db` 로 DB fallback(동일 HARD_BLOCK/SOFT_WARNING/THROTTLE 로직 재현).

---

## 4. Auth model

- **End-user VK 발급:** `gateway-cli`(api-key-helper)가 IDP(Cognito)에서 OIDC **id_token** 을 얻어 `POST /v1/auth/exchange`(Bearer id_token) → admin-api 가 JWKS 검증 + 사용자 프로비저닝 → **Virtual Key** 반환. (access_token 이 아니라 id_token 을 쓰는 이유: Cognito access_token 에 email/groups claim 이 없음.)
- **Inference 인증(경로 기반 전략, `resolve_auth_strategy`):**
  - `/model/*` → **VK** 전용(`VKAuthStrategy`)
  - `/v1/messages` · `/v1/responses` · `/v1/models` · `/v1/chat/completions` · `/v1/completions` · `/v1/usage/me` → **DUAL**(VK 또는 admin JWT 자동판별)
  - 그 외 `/v1/*` → **JWT**
  - gateway 는 VK 를 Redis(`key:vk:` 키)로 해석·검증만 하고, OIDC→VK 발급 자체는 admin-api 담당.
- **AWS 인증:** 모든 워크로드가 **IRSA**. cross-account 백엔드(claude-code→374, cowork→905)는 **STS AssumeRole**(DurationSeconds=3600) + `ExternalId` 를 추가로 쓴다. long-lived 원격 계정 키는 저장하지 않는다.
- **Client 식별(`client`) 은 분석/라우팅용이며 인가 신호가 아니다.** UA/originator 헤더는 스푸핑 가능하므로 신뢰하지 않는다. 인가 축은 VK + `allowed_clients` allow-list 이며 `ClientAuthorizationMiddleware` 가 위반 시 403.

**인증 방어 세부:**
- 무효/미등록 VK → Redis `key:vk:` 미스 → user_id=None → 즉시 `PermissionError('Invalid or inactive virtual key')` raise, **DB 세션을 열지 않는다** → 무효키 폭주가 DB 커넥션 풀을 고갈시키지 못한다.
- VK 캐시 히트여도 `user.is_active` 를 DB 로 재확인 → 캐시 TTL(300s) 안에 비활성화된 계정을 즉시 차단하고 캐시를 DEL.
- 모델 접근제어(`allowed_models`): `user_allowed_models` 우선 → 없으면 `team_allowed_models` 폴백 → 둘 다 없으면 None(전체 허용). **user override 조회가 DB 오류로 실패하면 fail-closed**(국가핵심기술 제한)로 인증 차단; `allowed_clients` 조회 실패는 fail-open.

---

## 5. Data plane — request flow (`/v1/messages`, claude-code / cowork)

미들웨어 실행 순서(Starlette `add_middleware` LIFO 등록 → 실행): `OTel → ClientId → Auth → ClientAuthZ → Budget → Downgrade → RateLimit → HeaderInjector → StateInjection → router`. `StateInjection` 은 pure ASGI 라 SSE 와 호환된다(BaseHTTPMiddleware 의 StreamingResponse 비호환 회피).

```
POST /v1/messages  (Bearer VK)
  1. ClientIdMiddleware → identify_client(headers) → state["client"] ∈ {claude-code, cowork, codex, other}
  2. AuthMiddleware     → VK → AuthContext (user/team/dept/roles)
  3. ClientAuthZ        → allowed_clients allow-list 위반 시 403
  4. Budget / Downgrade / RateLimit 미들웨어 (아래 §11)
  5. parse body → _BEDROCK_ALLOWED_FIELDS 필터, metadata.user_id 주입, 1h-cache TTL 감지
  6. _select_backend(client):
        routing_profile_loader.load(redis, db, client)         [Redis TTL 300s]
          ├─ Rule A: mantle profile + default_model  → BEDROCK_MANTLE, 그 모델 강제(cowork→cowork-opus)
          ├─ Rule B: 요청 alias provider == BEDROCK_MANTLE → BEDROCK_MANTLE(요청 alias)
          └─ 그 외                                    → BEDROCK native [default, claude-code]
  7. check_key_scope (allowed_models)              → 403 if not allowed
  8. enforce_rate_limits (RPM+TPM 예약, 3-scope USER→TEAM→GLOBAL)  → 429 if exceeded
  9. adapter 선택 + (opt) 웹서치 루프 + fallback loop:
        BEDROCK native (in-account 859) → boto3 invoke(-with-response-stream)
        BEDROCK native (claude-code→374) → BedrockAccountClientProvider 로 assume한 374 client
                                            (실패 시 859 in-account 투명 폴백) ─ §6
        BEDROCK_MANTLE (cowork→905)     → async httpx POST {endpoint}/v1/messages bearer ─ §7
 10. cost_recorder.finalize(client=…) → inline budget/rate settle → XADD cost:stream ─ §12
```

**codex 는 별도 경로:** codex(OpenAI Responses)는 `messages.py` 가 아니라 `openai_compat.py` 의 `/v1/responses` → `_handle_responses` 가 routing-profile 기반으로 별도 dispatch 한다(`MantleOpenAIAdapter`, us-east-2 in-account).

**Provider registry(4-adapter):** `ProviderType → adapter`
- `BEDROCK` — `BedrockAdapter`(boto3). in-account 클라이언트 + (claude-code) cross-account `client_resolver`/`fallback_client` 을 받는다.
- `OPENMODEL` — `OpenModelAdapter`(httpx vLLM/TGI).
- `BEDROCK_MANTLE` — `MantleAdapter`(async httpx + bearer, cowork, Anthropic Messages).
- `BEDROCK_MANTLE_OPENAI` — `MantleOpenAIAdapter`(async httpx + bearer, codex, OpenAI Responses).

`MantleCredentialBroker` / `mantle_http`(httpx.AsyncClient)는 cowork·codex 가 공유한다.

**Region rewrite(`_rewrite_model_id_for_region`):** cross-region inference profile prefix(`us.`/`eu.`/`apac.`)를 타깃 region 에 맞게 재작성. `_REGION_PREFIX_MAP`: us-east-1/us-west-2→us, eu-*→eu, ap-northeast-1/ap-northeast-2/ap-southeast-*→apac. `global.` prefix 는 pass-through. **Mantle 경로는 rewrite 하지 않는다**(provider_model_id 그대로). region 소스는 cross-account(claude-code→374)일 때만 `profile.region`(ap-northeast-2)을 명시 전달하고, in-account 는 pod `AWS_REGION` env(기본 ap-northeast-2) 사용.

---

## 6. claude-code → 374 cross-account Bedrock NATIVE

claude-code inference 는 **345678901234 계정의 bedrock-runtime(boto3 `invoke_model`) native** 로 나간다. Mantle 이 아니다. Bedrock native 는 원래 cross-account 미지원(startup 에 in-account 859 클라이언트 고정)이라, 이 경로는 대상 계정 role 을 STS AssumeRole 하여 그 계정의 bedrock-runtime 클라이언트를 빌드·캐시한다.

```
client=claude-code → routing_profiles row (backend=invoke, account_role_arn=374…claude-code-bedrock,
                     region=ap-northeast-2, external_id=claude-code-bedrock, default_model NULL)
  → _select_backend → BackendDecision(BEDROCK), profile carried
  → 라우터: _xacct = (backend=='invoke' AND account_role_arn) 이면 cross-account adapter 구성:
       adapter = BedrockAdapter(
                   bedrock_client=None,
                   client_resolver=lambda: BedrockAccountClientProvider.get_client(role, region, ext),
                   fallback_client=<in-account 859 bedrock_adapter._client>)   ← 투명 폴백
  → BedrockAccountClientProvider.get_client(role_arn, region, external_id):
       캐시 키 = (role_arn, region, external_id or '')     ← external_id 회전 시 stale 재사용 방지
       cache miss/만료임박 → sts.assume_role(374 role, DurationSeconds=3600, ExternalId=claude-code-bedrock)
                            → temp creds → boto3.client('bedrock-runtime', region, creds) 빌드
       creds 하드만료 −300s(skew) 전에 클라이언트 재빌드(static creds 는 botocore 자동 갱신 안 함)
  → BedrockAdapter._get_client(): resolver 성공 → 374 client / resolver 예외 → 859 in-account fallback
  → invoke_model / invoke-with-response-stream, model id 는 profile.region(ap-ne-2)으로 rewrite
```

**859 투명 폴백:** `BedrockAdapter._get_client` 는 `client_resolver`(374 assume+build)가 예외를 던지면 in-account 859 `fallback_client` 로 폴백한다 → **claude-code 는 절대 안 죽는다**(라이브 실증: 374 AccessDenied → 859 200). STS 클라이언트는 `mantle_assume_region`(기본 ap-northeast-2)에서 생성되며 creds 는 account-global 이라 재사용된다. cross-account 클라이언트 BotoConfig 는 in-account 와 동일(`max_pool_connections=50`, `read_timeout=stream_timeout(300s)`, `retries.total_max_attempts=bedrock_max_attempts`).

**즉시 롤백은 무배포:** `0022 downgrade` 가 claude-code 의 `account_role_arn`/`external_id` 를 NULL 로 되돌리면(코드가 `account_role_arn` NULL 이면 in-account adapter 사용), 다음 요청부터 859 in-account 로 복귀한다. 적용 후 Redis `routing_profile:claude-code` 캐시 키(TTL 5분)를 플러시하면 즉시 반영된다.

---

## 7. cowork / codex — Mantle async httpx cross-account & in-account

Mantle 백엔드는 boto3 가 아니라 **async httpx + SigV4 bearer** 다.

```
cowork  → routing_profiles (backend=mantle, role_arn=905…cowork-bedrock, region=ap-northeast-1,
          default_model=cowork-opus, external_id=cowork-bedrock, provider=BEDROCK_MANTLE,
          api_format=ANTHROPIC_MESSAGES, endpoint=https://bedrock-mantle.ap-northeast-1.api.aws/anthropic)
codex   → routing_profiles (backend=mantle, account_role_arn NULL, external_id NULL, region us-east-2,
          provider=BEDROCK_MANTLE_OPENAI, api_format=OPENAI_RESPONSES,
          endpoint=https://bedrock-mantle.us-east-2.api.aws/openai)

MantleCredentialBroker.bearer_token(profile):
  cross-account(cowork): sts.assume_role(905 role, ExternalId=cowork-bedrock)  [859 gateway IRSA 신뢰]
                           → temp creds (캐시 ~1h, refresh −300s skew, keyed by role_arn)
  in-account(codex):    _in_account_creds — assume 없이 pod 자체 IRSA creds 직접 사용
  → BedrockTokenGenerator().get_token(creds, region)
      → bearer (캐시 30m, keyed by (cred_key, region), creds 만료 −60s 상한)
        bearer 는 SigV4 regional-bound 이라 (role, region) 키잉
adapter:
  cowork: MantleAdapter  → POST {endpoint}/v1/messages   (Anthropic Messages, model=anthropic.claude-opus-4-8)
  codex:  MantleOpenAIAdapter → POST {endpoint}/v1/responses (OpenAI Responses, GPT-5.5)
  두 adapter 모두 httpx.AsyncClient + resp.aiter_lines() → 완전 async 스트리밍
```

**Zero key storage** — 게이트웨이는 원격 계정 long-lived 키를 보관하지 않는다. 메모리에 short-lived STS temp creds + bearer 만 둔다. 905 IAM role `llm-gateway-cowork-bedrock` 은 `bedrock-mantle:*`(Mantle 서비스 네임스페이스, `bedrock:*` 와 별개)를 grant 하고 859 gateway IRSA 를 `cowork-bedrock` ExternalId 로 신뢰한다.

---

## 8. 서버사이드 웹서치 (Architecture C)

Bedrock/Mantle 은 Anthropic native server-side `web_search` 를 노출하지 않는다(검증: ValidationException). 그래서 게이트웨이가 이를 **에뮬레이트**한다 — routing profile 의 `web_search_enabled=True` + AgentCore MCP client 구성 시 `run_web_search_loop` 를 탄다. **클라이언트는 아무 설정도 하지 않는다**(1P Claude 검색처럼 하나의 연속 스트림으로 보인다).

```
/v1/messages (web_search_enabled 프로파일) → run_web_search_loop:
  1. web_search 툴 주입(GW_WEB_SEARCH_NAME='web_search') — anthropic/responses 두 방언 지원
  2. 매 turn 스트리밍 → 모델의 OUR web_search tool_use 를 인터셉트(단순 name 동등성)
  3. AgentCore Gateway 관리형 WebSearch 호출 (MCP over httpx, SigV4/IRSA, tool '<target_id>___WebSearch')
       ⚠️ AgentCore 관리형 커넥터는 us-east-1 전용(AGENTCORE_REGION=us-east-1, AWS_REGION=ap-ne-2 와 다름·cross-region)
  4. 검색 결과를 tool_result 로 재투입 → 모델 continue
  5. 반복(max_iterations / total_deadline 가드) → text-only 또는 CLIENT tool_use turn 은 TERMINAL
  6. 모든 turn 을 하나의 연속 클라이언트 스트림으로 스티칭, 내부 검색 배관은 억제
```

- **인터셉트 규칙:** OUR `web_search` tool_use → 검색·continue; CLIENT tool_use(비 web_search) → TERMINAL forward(클라이언트 자체 tool loop 실행); text-only turn → TERMINAL(최종 답변).
- **가드레일:** max_iterations, total deadline. 초과 시 다음 turn 에서 web_search 툴을 빼 모델이 종결 답변을 내게 한다. 검색 실패는 error tool_result 를 주입(스트림 안 죽음). `web_search_count` 는 **성공한 검색만** 카운트하며 per-client 로 usage 에 기록된다.
- 글로벌 kill-switch `web_search_enabled` 기본 **off**. `agentcore_gateway_url` 미구성 시 `agentcore_mcp_client=None` → 라우터 분기 skip(zero-regression).

---

## 9. Resilience / degradation

최근 강화된 견고성 기능(파일:라인 근거).

| 기능 | 동작 | 근거 |
|---|---|---|
| **Readiness 게이트** | `/health` 는 관대(DB 단독 열화 `DB_DEGRADED` 에도 200). `/health/ready` 는 엄격 — `DegradationLevel != HEALTHY` 또는 pool `checkedout >= size+max_overflow`(hard_cap) 시 **503**(비블로킹, 라이브 쿼리 안 함). Helm readinessProbe path=`/health/ready` | `routers/health.py:49-110` |
| **DB pool 튜닝** | `pool_timeout=10s`(SQLAlchemy 기본 30s fast-fail 회피), `pool_recycle=3600s`, `pool_pre_ping=True`, `pool_size=20`. 세션은 요청 전 생애주기 동안 안 열고 consumer 가 short-lived 로 열고 닫아 SSE 중 pool 고갈 방지 | `db.py:20-37`, `config.py:66-78` |
| **무효 VK 방어** | 무효키는 DB 세션을 안 여니 폭주해도 커넥션 풀 고갈 불가(§4) | `auth_service.py:69-76` |
| **Security event LRU** | `SecurityEventDetector` 가 OrderedDict LRU(`_MAX_TRACKED_IPS=4096`)로 스푸핑 가능한 `x-forwarded-for` IP churn 에 의한 워커별 메모리 무한성장(OOM) 방어. 이벤트 발행 후 해당 IP 키 즉시 pop, 상한 초과 시 최오래된 키 evict | `security/event_detector.py:20,29-93` |
| **응답 헤더 유출 방어** | `HeaderInjectorMiddleware` 는 state 에 값이 있을 때만 `X-Request-Id`/`X-Budget-*`/`X-RateLimit-*`/`X-CostLimit-*`/`X-Model-Warning` 주입 | `middleware/otel.py:84-132` |
| **Bedrock retries 배선** | boto3 client: `max_pool_connections=50`, `connect_timeout=10`, `read_timeout=stream_timeout(300s)`, `retries.total_max_attempts=bedrock_max_attempts(기본 1=재시도 없음)`. 과거 dead config 였던 것을 실제 배선 → 게이트웨이 자체 fallback loop 와 곱해진 재시도 폭풍 억제 | `main.py:103-112`, `config.py:132-133` |
| **Redis 복원력** | `socket_timeout=2.0s`, `socket_connect_timeout=1.0s`, `retry(1회 ExponentialBackoff)`, `health_check_interval=30s` 배선(과거엔 max_connections 만 줘 느린 노드가 풀 고갈). rate-limit 회로 차단기(`rl_breaker_enabled` 기본 True)로 Redis 죽음 시 per-request fast-fail | `redis_client.py:24-37,70` |
| **Cost stream spool** | `CostStreamSpool` 이 finalize 시 XADD 실패(Redis blip)를 스풀링 → HealthChecker 가 healthy Redis 체크마다 drain·재발행(영구유실 방지) | `main.py:127-135`, `services/cost_recorder.py` |
| **DegradationManager** | DB/Redis 열화 상태를 게이지로 관리(HEALTHY / DB_DEGRADED / REDIS_DEGRADED / BOTH_DEGRADED). DB 열화 시 라우터가 routing profile 조회를 skip 하고 기본 Bedrock 경로로 강등 | `main.py:118-119`, `messages.py:185-215` |

---

## 10. 부하 / 스케일링 (고동시성 SSE)

**병목(claude-code, Bedrock native 한정):** claude-code 경로는 boto3 동기 스트림을 async 이벤트루프에서 소비하려고 매 청크마다 `run_in_executor` 로 스레드풀에 offload 한다. 청크 수신이 blocking 이라 활성 SSE 하나가 스트림 수명 내내 스레드 슬롯 하나를 점유한다. prod 동시성 산수: HPA 최대 30 pod × uvicorn `--workers 4` = 120 프로세스, 프로세스당 기본 스레드풀 ~6 → ~720 slot << 5000 SSE. 스레드가 네트워크 대기 중이라 CPU 가 한가해 CPU 기준 HPA(targetCPU 65%)가 스케일아웃하지 않는 사각지대가 있다.

**codex/cowork(Mantle)은 이 병목에서 자유** — Mantle 경로는 `httpx.AsyncClient` + `aiter_lines()` 로 완전 async 라 스레드풀을 안 쓰고 이벤트루프만으로 수천 스트림을 처리할 수 있다.

**완화책(효과 큰 순):**
1. **스트리밍 전용 ThreadPoolExecutor** — `BEDROCK_STREAM_EXECUTOR_WORKERS` env 가 `>0` 이면 전용 executor(`thread_name_prefix='bedrock-sse'`), `0`/미설정이면 기본 공유 executor(무회귀·안전 롤백). **PoC 로 코드에는 shipped 됐으나(`bedrock_adapter.py:19-30`) 어떤 chart values 에도 배선돼 있지 않아 배포상 기본 비활성** — 활성화하려면 prod values gateway-proxy env 에 명시 필요(soap: max_pool_connections·memory 2Gi OOM 여유 고려).
2. **async httpx + 자체 SigV4 전환**(정석, HIGH 난이도) — LiteLLM 방식(boto3 스트리밍 버리고 async HTTP, botocore 는 파싱만; 파싱 동기 OK, aioboto3 불필요). 단 로컬 실측(972/s·6.5x)은 루프백·파싱0·소켓무제한 환경이라 실환경 미검증이고 적대검증상 async 순증분은 (전용풀+소켓상향) 대비 +6%. **착수 전 승인 게이트(골든 바이트캡처 테스트 + 실 Bedrock A/B 카나리) 통과가 조건.**
3. uvicorn `--limit-concurrency` 백프레셔.
4. 커스텀 메트릭(활성 SSE/동시성 기준) HPA.

**관련 함정:** boto3 소켓 풀 하드캡 `max_pool_connections=50`(in-account/cross-account 동일, 5000 SSE 규모엔 상향 검토 — 200~500). uvicorn `--workers 4` 는 Dockerfile CMD 하드코딩이라 env override 불가. DB 커넥션 풀은 이 병목의 직접 원인이 **아니다**(인증 short-lived, `pool_timeout=10s` fast-fail). 상세·로드맵·승인 게이트: `devlog_websearch.md §부하 분석`.

---

## 11. Governance — 예산 · rate limit · 다운그레이드

### 11.1 예산 (HARD_BLOCK / SOFT_WARNING / THROTTLE)

Lua(`budget_check.lua`)가 scope 별 원자 체크:
- **hard_block** — `used >= limit` 차단
- **soft_warning** — `used >= limit*soft_limit_pct`(기본 110%) 차단, `used >= limit` 경고 플래그
- **throttle** — thresholds(기본 80/90/100) 초과 시 `throttle_active`

TEAM 미설정 = deny, USER 미설정 = pass. **per-app(client) 예산:** `PER_APP_BUDGET_CLIENTS=('claude-code','cowork','codex')`, USER config 의 `app_clients` 에 등록된 client 한해 3번째 eval(`budget:user:{uid}:{client}:{period}`). config 캐시 미스 시 DB 에서 rehydrate(bypass 방어).

### 11.2 Rate limit (3-scope USER / TEAM / GLOBAL)

`RateLimitScope` enum, fast-fail 순서 USER→TEAM→GLOBAL. `team_id` None 이면 TEAM scope skip. 주 집행은 라우터의 `enforce_rate_limits`(RPM+TPM). **TPM 증가분 = input + cache_creation + output**(cache_read 제외). Redis 다운 시 rate-limit 미들웨어가 **in-memory fallback** 으로 3계층 근사 집행(보수적 상수 60/600/6000 RPM). divisor = `rl_fallback_replicas × uvicorn_workers`(과거 하드코딩 4 → HPA replica 무시하던 429×6 버그 배경). budget `throttle_active` 면 USER RPM 감경.

### 11.3 자동 다운그레이드 (TEAM scope 전용)

`DowngradeMiddleware`: `team_id` + `budget_status.threshold_pct` 로 `downgrade_policies` 체인 적용, body 의 model 을 rewrite. **cowork client 는 명시적 skip**(routing profile 이 모델 override). haiku-4-5 로 다운그레이드 시 `thinking` 필드 제거(Bedrock ValidationException 방지).

### 11.4 ROI / 사용량 집계

`/v1/usage/me` = 당일 Redis daily counter + 이전일 DB `daily_aggregates`(date GROUP BY) + budget_info + model_breakdown. `/v1/usage/team/{id}` 는 TEAM_LEADER(본인 팀) 또는 ADMIN 만. DEPT scope 는 ROI/reporting 에만 존재.

---

## 12. Cost / usage pipeline (write path)

**inline** critical-path 단계와 **offloaded** async 단계로 분리해 DB 쓰기가 inference 를 막지 않게 한다.

```
INLINE (gateway-proxy, CostRecorder.finalize, ~1–2ms, fail-soft, DB I/O 없음):
  zero-usage guard(release TPM) → calculate_cost(input+output+cache_write[5m|1h]+cache_read)
  → budget_deduct Lua (user + team, returns threshold_triggered)
  → settle_cost(CPM/CPH USER+TEAM) → settle_tpm
  → XADD cost:stream {CostStreamEntry(... client ...)}     (실패 시 CostStreamSpool 스풀링)

OFFLOADED (cost-recorder-worker):
  StreamConsumer XREADGROUP (backlog id=0 → live id=>, batch, XACK at-least-once)
  → BatchFlusher:
       bulk INSERT usage.usage_logs(… client) ON CONFLICT(request_id) DO NOTHING   [idempotent]
       UPSERT budget.budget_usages (user + team, limit snapshot)
       INCRBY usage:daily:* (Redis)
       PUBLISH notifications:budget  (threshold_triggered 시)
  daily_aggregator (cron KST 00:10 + startup backfill):
       usage_logs → usage.daily_aggregates  GROUP BY (date,user_id,team_id,dept_id,model_alias)
```

> `usage_logs` 는 admin-api 대시보드/분석/모니터링이 읽는 **per-request source of truth**. `daily_aggregates` 는 gateway-proxy `/v1/usage/me` 만 읽는 roll-up.

---

## 13. Client-tag data flow (`client` dimension)

`client` 태그(`claude-code` | `cowork` | `codex` | `other`)는 end-to-end 로 스레드된다.

```
request headers
  → identify_client()  (services/client_identifier.py)  ─ UA/originator 는 신뢰 안 함(로깅·라우팅용)
  → ClientIdentificationMiddleware → scope["state"]["client"]
  → messages.py / openai_compat.py → cost_recorder.finalize(client=…)
  → CostStreamEntry.make(client=…)
  → XADD cost:stream
  → cost-recorder-worker BatchFlusher → INSERT usage.usage_logs(…, client)  [indexed]
```

**식별 규칙(cowork-checked-first, claude-code 와 cowork 둘 다 `claude-cli/` 를 실을 수 있어):**
- `anthropic-client-platform == desktop_app` OR UA 에 `claude-desktop-3p` / `local-agent` / (`Electron/` + `Claude/`) → **cowork**
- originator `codex*` / `codex_cli_rs` OR UA → **codex**
- else UA `claude-cli/…` → **claude-code**
- else → **other**

**Read side(control plane) — 구현 완료:** admin-ui 에 client 차원 UI 가 이미 구현되어 있다 — `ClientShareDonutClient` · `ClientFilter` 컴포넌트 + `/api/dashboard/client-share` 프록시 라우트. `usage_logs.client` 는 populate·indexed 되어 realtime per-client breakdown 이 조회 가능하다.

---

## 14. Control plane — aggregation surface (admin-api)

모두 `usage.usage_logs` realtime 읽기. cost/usage 쿼리는 `cost_period_filter(period)`(SUCCESS + KST month)로 funnel, monitoring 은 raw last-1h/1d(ERROR/TIMEOUT 포함).

| Endpoint | GROUP BY | Source |
|---|---|---|
| `GET /admin/dashboard/summary` | — (single aggregate) | usage_logs |
| `GET /admin/dashboard/model-share` | `model_alias`(opt `team_id`) | usage_logs |
| `GET /admin/dashboard/client-share` | `client` | usage_logs |
| `GET /admin/dashboard/top-users` / `top-teams` | user / team | usage_logs |
| `GET /admin/analytics` | model / team / user(`group_by`) | usage_logs |
| `GET /admin/analytics/models` | `model_alias`, `(day, model_alias)` | usage_logs |
| `GET /admin/monitoring/{overview,models,events,users}` | — / model / row / user(last-1h) | usage_logs |

admin-ui(Next.js)는 server-only `adminAPI` 클라이언트(`src/lib/api-client.ts`, `ADMIN_API_URL`, cookie-forwarded admin_jwt) + `/api/*` 프록시 라우트로 fetch. 위젯은 SSR `initialData` 를 받는 client 컴포넌트, 필터(period/team/client)가 프록시 라우트로 refetch 를 트리거한다. 대시보드 차트는 **Chart.js**(`react-chartjs-2`), BI 챗 차트는 **recharts**.

**admin-chat-agent(BI):** agents-as-tools 오케스트레이터가 sql/code/validator/viz/**report** 5개 specialist + **L3 self-consistency(`ask_sql_verified`)** + `render_chart` 를 tool 로 보유(최소 5-agent 초과). deep 모드 별도 `orchestrator_deep`, **L4 cross-family critic/auditor**(`CRITIC_ENABLED`, deep+고위험만) 존재. text2SQL 정확도 하네스: deterministic-tool-first + 구조화 Pydantic envelope + reconciliation gate(fail-soft) + DAIL-SQL few-shot + tool 투명성. 골든테스트 12 use-case(Tier A 8 SQL-only + Tier B 4 SQL+Code), static/live, live pass-rate 17→33→50→67%.

---

## 15. Source map (where to look)

| Concern | Path |
|---|---|
| Request routing + backend select | `gateway-proxy/src/app/routers/messages.py`(`_select_backend`), `routers/openai_compat.py`(`/v1/responses`, codex) |
| Provider adapters | `gateway-proxy/src/app/providers/{bedrock,openmodel,mantle,mantle_openai}_adapter.py`, `registry.py` |
| cross-account Bedrock native | `gateway-proxy/src/app/services/bedrock_account_client.py`(`BedrockAccountClientProvider`) |
| Mantle credentials | `gateway-proxy/src/app/services/mantle_credentials.py`(`MantleCredentialBroker`) |
| Routing profiles | `gateway-proxy/src/app/services/routing_profile_loader.py`, `db/versions/0009,0017,0021,0022_*` |
| Client identification | `gateway-proxy/src/app/services/client_identifier.py`, `middleware/{client_id,client_authz}.py` |
| Region rewrite | `gateway-proxy/src/app/routers/bedrock.py`(`_rewrite_model_id_for_region`) |
| 웹서치 | `gateway-proxy/src/app/services/{web_search_loop,agentcore_mcp_client}.py` |
| 예산 / rate limit | `services/budget_service.py`, `redis_scripts/budget_check.lua`, `services/rate_limit_scope.py`, `middleware/{rate_limit,downgrade}.py` |
| Resilience | `routers/health.py`, `db.py`, `redis_client.py`, `security/event_detector.py`, `services/cost_recorder.py`(spool) |
| Cost pipeline | `services/cost_recorder.py`, `cost-recorder-worker/src/worker/{stream_consumer,batch_flusher,daily_aggregator}.py` |
| Dashboards/analytics | `admin-api/src/app/routers/{dashboard,analytics,monitoring}.py`, `core/usage_filters.py`, `repositories/analytics_repository.py` |
| Admin UI | `admin-ui/src/app/page.tsx`, `src/components/dashboard/*`(ClientShareDonutClient/ClientFilter), `src/app/api/dashboard/*` |
| BI assistant | `admin-chat-agent/src/agent/main.py`, `admin-api/src/app/routers/chat_agent.py` |
| Migrations | `db/versions/*`(head `0022`), `db/env.py`, `db/run_migration.sh` |
| Deployment | `deployment/charts/llm-gateway/` |

---

## 16. Multi-app status (spec `capability-requirements.md`)

| # | Requirement | Status |
|---|---|---|
| 1 | Client identification (Claude Code / Cowork / Codex) | ✅ (4값: claude-code/cowork/codex/other) |
| 2 | Bedrock account separation — **CC→374(native), Codex→859(in-account), Cowork→905(Mantle)** | ✅ (CC→374 컷오버 0022 완료) |
| 3 | Call method — Cowork=Mantle Opus 4.8(Tokyo), Codex=Mantle GPT-5.5(Ohio, in-account) | ✅ (live) |
| 4 | User-pool separation | ⏳ 진행 |
| 5 | **Dashboard distinguishes client(client-share donut + filter)** | ✅ (admin-ui 구현 완료) |

계정 매핑 요약: **123456789012** = 메인 배포(게이트웨이/Aurora/AgentCore/ECR), **345678901234** = claude-code 전용 cross-account Bedrock native(같은 계정에 도는 `ds-*` 는 별도 프로젝트), **234567890123** = cowork 전용 cross-account Mantle(도쿄). 세 계정은 중복 없이 분리된다.
