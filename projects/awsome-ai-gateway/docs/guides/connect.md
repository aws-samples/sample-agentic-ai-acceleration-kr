# LLM Gateway 연결 가이드 (Claude Code & Cowork)

> **목적**: 테스트/사용을 위해 **Claude Code** 와 **Cowork**(Claude 데스크톱 앱) 를 우리 사내 **LLM Gateway**(`llm-gateway-dev`, 계정 `333344445555`, 리전 `ap-northeast-2`) 에 붙이는 방법을, **터미널을 여는 순간부터** 한 줄씩 따라 할 수 있게 정리한다. **해제(원복)** 방법까지 포함한다.
>
> **읽는 사람**: 처음 붙여보는 엔드유저/테스터. 위에서 아래로 그대로 따라 하면 된다.

---

## 0. 먼저 이해할 것 — 두 클라이언트, 두 연결 방식

| | **Claude Code** (터미널 CLI) | **Cowork** (Claude 데스크톱 앱 안의 에이전트) |
|---|---|---|
| 연결 방식 | 환경변수 `ANTHROPIC_BASE_URL` + `gateway-cli`/`api-key-helper` 가 발급한 **Virtual Key(VK)** | 앱 config 파일(`Claude-3p/configLibrary/<uuid>.json`)의 `inferenceProvider: "gateway"` |
| 인증 | `Authorization: Bearer <VK>` (api-key-helper 자동 주입) | `inferenceGatewayApiKey: <VK>` + `inferenceGatewayAuthScheme: bearer` |
| 게이트웨이가 식별 | UA `claude-cli/... (external, cli)` → `client=claude-code` | UA `claude-cli/... (external, claude-desktop-3p)` → `client=cowork` |
| 백엔드(현재 dev) | 333 Bedrock `InvokeModel` (Seoul) | **222 Bedrock Mantle Opus 4.8** (Tokyo) — routing_profiles 로 자동 라우팅 |

> 게이트웨이는 두 클라이언트를 **User-Agent** 로 자동 구분한다. 별도 설정 없이도 `usage_logs.client` 에 `claude-code` / `cowork` 로 기록되고, Cowork 트래픽은 routing_profiles 규칙에 따라 222 Mantle 로 라우팅된다.

### 우리 환경의 실제 엔드포인트 (dev, 계정 333, HTTP ALB)

| 용도 | URL |
|------|-----|
| **Gateway (추론 진입점)** | `http://<GATEWAY_HOST>` |
| **Admin API** | `http://<ADMIN_API_HOST>` |
| **Admin UI (대시보드)** | `http://<ADMIN_UI_HOST>` |

> ⚠️ **현재 dev 는 HTTP ALB 직결**이다 (공인 HTTPS/CloudFront 미배포). Claude Code 는 HTTP baseURL 을 그대로 수용한다. **Cowork 는 문서상 HTTPS 를 요구**하지만 실측상 `http://` 도 통한다(§B-주의 참고). 운영 전환 시 CloudFront(HTTPS)로 교체 예정.

---

# A. Claude Code 를 게이트웨이에 붙이기

## A-1. 터미널 열기 + 사전 확인

1. **터미널**(macOS: `⌘+Space` → "Terminal" → Enter)을 연다.
2. Claude Code 가 설치돼 있는지 확인:
   ```bash
   claude --version
   ```
   - 버전이 안 나오면 먼저 설치: `npm install -g @anthropic-ai/claude-code` (또는 사내 배포본).
3. 게이트웨이가 살아있는지 확인 (200 이 나오면 정상):
   ```bash
   curl -s -o /dev/null -w "gateway: %{http_code}\n" \
     http://<GATEWAY_HOST>/health
   ```

## A-2. Virtual Key(VK) 발급받기

게이트웨이는 `Authorization: Bearer <VK>` 로 인증한다. VK 를 얻는 방법은 두 가지다.

### 방법 ① 정식 — gateway-cli (OIDC 로그인 → VK 자동 발급·갱신) [권장]

**설치** (uv 격리 설치 — 나중에 `uv tool uninstall gateway-cli` 로 깨끗이 제거, 기존 Claude 환경 무영향):
```bash
uv tool install --from ./gateway-cli gateway-cli   # 저장소 gateway-cli/ 기준
gateway-cli version
```
> `gateway-cli login`/`logout` 은 `~/.gateway-cli/` 에만 쓴다(OIDC 토큰·VK 캐시). Claude Code 의 `~/.claude/settings.json` 을 건드리는 건 `setup`/`disable` 뿐이다. Cowork 만 테스트할 거면 `login` 만 쓰면 된다.

**dev(333) 실측값** (운영자 안내 불필요 — 아래 값 그대로):
```bash
export OIDC_ISSUER_URL="https://cognito-idp.ap-northeast-2.amazonaws.com/<COGNITO_USER_POOL_ID>"
export OIDC_CLIENT_ID="<OIDC_CLIENT_ID>"   # Cognito app client: llm-gateway-dev-cli (PKCE, localhost:8090-8092 콜백)
export ADMIN_API_URL="http://<ADMIN_API_HOST>"
```

**(A) Claude Code 자동 연결** — `login` 후 `setup` 으로 settings.json 자동 작성:
```bash
gateway-cli login --issuer-url "$OIDC_ISSUER_URL" --client-id "$OIDC_CLIENT_ID"   # 브라우저 Cognito 로그인
gateway-cli setup --gateway-url "https://<CLOUDFRONT_DOMAIN>" --admin-api-url "$ADMIN_API_URL"
```
`setup` 이 `apiKeyHelper`(매 요청 Bearer VK 자동 주입·갱신) + base URL 을 기록한다. **이후 `claude` 실행만 하면 게이트웨이로 간다.**

**(B) VK 문자열만 직접 뽑기** — Cowork config 에 넣거나 수동 사용 시. `login` 후 OIDC access token 을 `/v1/auth/exchange` 로 교환:
```bash
gateway-cli login --issuer-url "$OIDC_ISSUER_URL" --client-id "$OIDC_CLIENT_ID"
VK=$(python3 - "$ADMIN_API_URL" <<'PY'
import json, os, sys, urllib.request
api = sys.argv[1]
tok = json.load(open(os.path.expanduser("~/.gateway-cli/oidc-tokens.json")))
access = tok.get("access_token") or tok.get("id_token")
req = urllib.request.Request(f"{api}/v1/auth/exchange",
    data=json.dumps({"device_name":"manual"}).encode(),
    headers={"Authorization": f"Bearer {access}", "Content-Type":"application/json"}, method="POST")
print(json.load(urllib.request.urlopen(req, timeout=15))["virtual_key"])
PY
)
echo "VK 길이: ${#VK}"   # 값은 출력하지 않음
```
> ⚠️ **VK 는 1시간 만료** (`OIDC_VK_TTL_HOURS=1`). 만료되면 게이트웨이가 401(`auth_failed`) → 클라이언트는 "API key rejected / rotated or revoked" 표시. **OIDC access token 도 ~1시간 만료**라, 만료 후엔 `gateway-cli login` 부터 다시 해야 한다. 반복 테스트가 잦으면 방법 ①(A) 의 `apiKeyHelper`(자동 갱신) 나 Cowork helper-script 방식(§B-부록)을 쓴다.

### 방법 ② 빠른 테스트 — dev 발급 엔드포인트 (DEV 전용)

dev 환경에는 인증 없이 테스트 VK 를 주는 헬퍼가 있다:
```bash
VK=$(curl -s -X POST \
  http://<ADMIN_API_HOST>/internal/test/issue-key \
  -H "Content-Type: application/json" -d '{}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['virtual_key'])")
echo "발급된 VK 길이: ${#VK}"     # 키 값 자체는 출력하지 않음(노출 방지)
```
> ⚠️ `internal/test/issue-key` 는 **dev 테스트 전용**이다. 운영에서는 방법 ①(gateway-cli)만 쓴다.

## A-3. Claude Code 를 게이트웨이로 향하게 설정 (수동 — 방법 ② 사용 시)

`gateway-cli setup` 을 안 쓰고 직접 붙일 때는 환경변수 2개만 주면 된다:
```bash
export ANTHROPIC_BASE_URL="http://<GATEWAY_HOST>"
export ANTHROPIC_AUTH_TOKEN="$VK"     # 위 A-2 ②에서 받은 VK. Claude Code가 Bearer 로 보냄
```
> `ANTHROPIC_AUTH_TOKEN` 을 주면 Claude Code 가 `Authorization: Bearer <VK>` 로 보낸다. (정식 방법 ①은 api-key-helper 가 이걸 자동으로 한다.)

## A-4. 사용 시작 + 연결 확인

```bash
claude
```
- 정상 동작하면 게이트웨이를 거쳐 응답이 온다.
- **게이트웨이에 기록됐는지 확인** (관리자/테스터): Admin UI 대시보드의 **앱별 비용 점유율** 위젯에서 `Claude Code` 항목이 늘어난다. 또는 SQL/BI 챗으로 확인.

빠른 단발 테스트(채팅 세션 없이 1회 호출):
```bash
curl -s -X POST "$ANTHROPIC_BASE_URL/v1/messages" \
  -H "Authorization: Bearer $VK" \
  -H "Content-Type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -H "User-Agent: claude-cli/2.1.97 (external, cli)" \
  -d '{"model":"claude-sonnet-4-6","max_tokens":20,"messages":[{"role":"user","content":"say gateway-ok"}]}'
```
→ `"gateway-ok"` 가 오면 연결 성공. 이 요청은 `client=claude-code` 로 기록된다.

## A-5. Claude Code 해제 (게이트웨이에서 떼기 — 원래대로)

### gateway-cli 로 설정한 경우
```bash
gateway-cli logout          # VK 캐시 삭제(설치본에 명령이 있으면)
```
그리고 `gateway-cli setup` 이 기록한 설정을 되돌린다:
```bash
# managed-settings.d 에 기록된 경우 — 해당 항목 제거
ls ~/.config/Claude/ 2>/dev/null
ls /Library/Application\ Support/ClaudeCode/managed-settings.d/ 2>/dev/null   # (경로는 환경마다 다름)
# gateway 관련 settings.json(apiKeyHelper/ANTHROPIC_BASE_URL) 을 지우거나 백업본으로 교체
```

### 환경변수로 설정한 경우 (방법 ②)
현재 터미널 세션에서만 풀려면:
```bash
unset ANTHROPIC_BASE_URL
unset ANTHROPIC_AUTH_TOKEN
```
`~/.zshrc` 등에 영구로 넣었다면 그 줄을 삭제하고 `source ~/.zshrc` 또는 새 터미널을 연다.

해제 확인:
```bash
echo "${ANTHROPIC_BASE_URL:-<unset>}"     # <unset> 이면 게이트웨이 연결 해제됨
```
이제 `claude` 는 다시 Anthropic 공식 API(또는 기존 설정)로 직결된다.

---

# B. Cowork 를 게이트웨이에 붙이기 (macOS)

> Cowork = Claude 데스크톱 앱 안의 에이전트. `inferenceProvider` 키 하나로 백엔드를 고른다: `bedrock`(직결, 게이트웨이 안 거침) ↔ **`gateway`(우리 게이트웨이 경유)**. 우리는 `gateway` 모드로 바꾼다.

## B-1. 터미널 열기 + config 파일 찾기

1. **Cowork(Claude 데스크톱 앱)를 완전히 종료**한다 (⌘+Q). 파일 교체 중 앱이 덮어쓰지 않게.
2. 터미널을 연다.
3. Cowork config 디렉터리로 간다. 적용 중인 파일은 `_meta.json` 의 `appliedId` 가 가리킨다:
   ```bash
   cd ~/Library/Application\ Support/Claude-3p/configLibrary/
   ls -la
   cat _meta.json        # {"appliedId":"<uuid>", ...}  ← 이 uuid 가 현재 적용 중
   ```
   예: `appliedId` 가 `d5ef301d-...` 이면 적용 파일은 `d5ef301d-....json`.

## B-2. 현재 config 백업 (원복 보장 — 반드시!)

```bash
APPLIED=$(python3 -c "import json;print(json.load(open('_meta.json'))['appliedId'])")
cp "${APPLIED}.json" "${APPLIED}.json.ORIGINAL"
cp _meta.json _meta.json.ORIGINAL
echo "백업 완료: ${APPLIED}.json.ORIGINAL"
```
> 원본은 보통 `bedrock` 직결 형태다 (`"inferenceProvider": "bedrock"`, `inferenceBedrockRegion`, `inferenceBedrockBearerToken` 등). 이 백업이 있어야 §B-4 에서 깔끔히 되돌린다.

## B-3. gateway 모드로 교체

> **⚠️ 핵심 (실측으로 확정 — 2026-06-20): Cowork 는 HTTPS 가 필수다. HTTP ALB 직결은 안 된다.**
> Cowork(Claude 데스크톱 앱)의 config 스키마는 `inferenceGatewayBaseUrl` 에 `remotePolicy:{rejectLoopback:true, originPinned:true}` 를 강제한다(앱 번들 `Claude.app/Contents/Resources/app.asar` 디컴파일로 확인). HTTP origin 은 거부되어 **"provider setup needs a fix"** / **"Server is busy. Retrying…"** 무한 재시도로 빠진다. 따라서 게이트웨이 ALB 앞에 **CloudFront(HTTPS)** 를 세우고 그 `https://...cloudfront.net` URL 을 써야 한다.
>
> **dev(333) CloudFront (생성 완료):** `https://<CLOUDFRONT_DOMAIN>` (분포 ID `<CLOUDFRONT_DISTRIBUTION_ID>`, origin = gateway-proxy ALB, http-only origin + redirect-to-https viewer + POST/SSE). CloudFront 가 없는 환경이라면 §B-부록 "CloudFront 생성" 참고.

적용 파일(`<uuid>.json`)을 아래 내용으로 바꾼다. **VK 는 §A-2 에서 발급받은 값**을 넣는다. `bedrock` 전용 키(`inferenceBedrockRegion`, `inferenceBedrockBearerToken`)는 **제거**하고, 원본의 메타 키(`inferenceModels`, `deploymentOrganizationUuid`, `disableDeploymentModeChooser`)는 **보존**한다.

```jsonc
{
  "inferenceProvider": "gateway",
  "inferenceGatewayBaseUrl": "https://<CLOUDFRONT_DOMAIN>",
  "inferenceCredentialKind": "static",
  "inferenceGatewayApiKey": "<VIRTUAL_KEY>",
  "inferenceGatewayAuthScheme": "bearer",
  "inferenceModels": ["claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"],
  "deploymentOrganizationUuid": "<원본값 보존>",
  "disableDeploymentModeChooser": true
}
```

| 키 | 의미 | 필수 |
|----|------|------|
| `inferenceProvider` | `"gateway"` → 게이트웨이 모드 활성 | ✅ |
| `inferenceGatewayBaseUrl` | **HTTPS(CloudFront) URL** — HTTP 는 거부됨(위 경고) | ✅ |
| `inferenceCredentialKind` | `"static"` (VK 직접 입력 시). **빠지면 "needs a fix" / degraded** — 실측으로 확인된 필수 키 | ✅ |
| `inferenceGatewayApiKey` | VK. **빈 값 불가** | ✅ |
| `inferenceGatewayAuthScheme` | `"bearer"` → `Authorization: Bearer <VK>` | ✅ |
| `inferenceModels` | 모델명 리스트. 게이트웨이가 Cowork UA 를 보고 어떤 모델명이든 222 Mantle(`cowork-opus`)로 라우팅하므로 값 자체는 무관하나, **키가 있어야** 앱이 정상 인식 | ✅ |
| `deploymentOrganizationUuid`, `disableDeploymentModeChooser` | 원본에 있던 배포 메타. **보존**(빠지면 "setup 필요" 경고) | 원본 유지 |

> 터미널로 한 번에 쓰기 (원본 메타 보존 + gateway 전환). `$VK` 는 §A-2 에서 발급받은 값, `$CF` 는 CloudFront URL:
> ```bash
> CFGDIR="$HOME/Library/Application Support/Claude-3p/configLibrary"
> APPLIED=$(python3 -c "import json,os;print(json.load(open(os.path.join('$CFGDIR','_meta.json')))['appliedId'])")
> CF="https://<CLOUDFRONT_DOMAIN>"
> python3 - "$CFGDIR" "$APPLIED" "$CF" "$VK" <<'PY'
> import json, os, sys
> cfgdir, applied, cf, vk = sys.argv[1:5]
> orig = json.load(open(os.path.join(cfgdir, f"{applied}.json.ORIGINAL")))  # §B-2 백업 기준
> new = dict(orig)
> new.pop("inferenceBedrockRegion", None); new.pop("inferenceBedrockBearerToken", None)
> new["inferenceProvider"] = "gateway"
> new["inferenceGatewayBaseUrl"] = cf
> new["inferenceCredentialKind"] = "static"
> new["inferenceGatewayApiKey"] = vk
> new["inferenceGatewayAuthScheme"] = "bearer"
> new["inferenceModels"] = ["claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"]
> json.dump(new, open(os.path.join(cfgdir, f"{applied}.json"), "w"), indent=2)
> print(f"{applied}.json → gateway(HTTPS) 모드로 교체 완료")
> PY
> ```

## B-4. Cowork 재시작 + 연결 확인

1. **Cowork(앱)를 다시 실행**한다 (반드시 ⌘+Q 완전 종료 후).
2. 에이전트 탭에서 메시지 1개(`hi`)를 보낸다.
3. **연결 확인** — "needs a fix"/"Server is busy" 없이 정상 응답이 오면 성공.

> **사전 셀프체크 (앱 켜기 전, VK 가 유효한지)**: config 의 VK 로 게이트웨이를 직접 찔러본다. `200` 이면 OK, `401` 이면 VK 만료 → §A-2 로 재발급.
> ```bash
> CFGDIR="$HOME/Library/Application Support/Claude-3p/configLibrary"
> APPLIED=$(python3 -c "import json,os;print(json.load(open(os.path.join('$CFGDIR','_meta.json')))['appliedId'])")
> VK=$(python3 -c "import json,os;print(json.load(open(os.path.join('$CFGDIR','$APPLIED.json')))['inferenceGatewayApiKey'])")
> curl -s -o /dev/null -w "models→%{http_code}\n" \
>   "https://<CLOUDFRONT_DOMAIN>/v1/models" \
>   -H "Authorization: Bearer $VK" -H "User-Agent: claude-cli/2.0.0 (external, claude-desktop-3p)"
> # 실제 추론(Mantle Opus 4.8)까지 확인:
> curl -s -X POST "https://<CLOUDFRONT_DOMAIN>/v1/messages" \
>   -H "Authorization: Bearer $VK" -H "Content-Type: application/json" \
>   -H "User-Agent: claude-cli/2.0.0 (external, claude-desktop-3p)" \
>   -H "anthropic-client-platform: desktop_app" -H "anthropic-version: 2023-06-01" \
>   -d '{"model":"claude-opus-4-7","max_tokens":16,"messages":[{"role":"user","content":"hi"}]}' \
>   | python3 -c "import sys,json;d=json.load(sys.stdin);print('응답 model:',d.get('model'),'| id:',d.get('id','')[:18])"
> # → "응답 model: claude-opus-4-8 | id: msg_bdrk_…" 이면 222 Mantle 정상.
> ```
> ⚠️ **모델에게 "너 누구냐" 물어보면 'Opus 4.5/4.x' 라 답할 수 있다 — 무시하라.** LLM 은 자기 백엔드를 모르고 학습 데이터의 이름을 답할 뿐이다. 라우팅 증거는 **응답 JSON 의 `.model` 필드**(=`claude-opus-4-8`)와 `.id`(=`msg_bdrk_…` Mantle 형식), 그리고 게이트웨이 `usage_logs`(아래)다.

4. **게이트웨이 기록 확인** (관리자/테스터): Admin UI 대시보드 **앱별 비용 점유율** 위젯에 `Cowork` 항목이 생기거나 늘어난다. Cowork 트래픽은 **222 Mantle Opus 4.8**(Tokyo)로 라우팅되고 `usage_logs.client='cowork'`, `provider='BEDROCK_MANTLE'`, `model_alias='cowork-opus'` 로 기록된다. DB 직접 확인(admin-api pod 내부):
   ```bash
   # kubectl exec … -c admin-api -- python  (app.core.db engine 사용; RDS Proxy 라 raw asyncpg 는 ssl 옵션 미지원)
   SELECT client, provider, model_alias, count(*), round(sum(cost_usd),6)
   FROM usage.usage_logs WHERE client='cowork' GROUP BY 1,2,3;
   ```

## B-5. Cowork 해제 (원래 bedrock 직결로 원복)

1. **Cowork 완전 종료** (⌘+Q).
2. 백업 복원:
   ```bash
   cd ~/Library/Application\ Support/Claude-3p/configLibrary/
   APPLIED=$(python3 -c "import json;print(json.load(open('_meta.json'))['appliedId'])")
   cp "${APPLIED}.json.ORIGINAL" "${APPLIED}.json"
   cp _meta.json.ORIGINAL _meta.json     # (백업해 둔 경우)
   echo "원복 완료 — 다시 bedrock 직결"
   ```
3. **Cowork 재시작** → 이제 다시 Bedrock 직결(게이트웨이 안 거침).

> 백업이 없다면: config 에서 `inferenceProvider` 를 `"bedrock"` 으로 바꾸고 `inferenceGateway*` 키들을 지운 뒤, `inferenceBedrockRegion`(예: `us-west-2`) 등 원래 bedrock 키를 복구해야 한다. **그래서 §B-2 백업이 중요하다.**

---

# C. 문제 해결 (Troubleshooting)

| 증상 | 원인 / 확인 | 해결 |
|------|------------|------|
| `401 Unauthorized` | VK 없음/만료/오타 | §A-2 로 VK 재발급. `Authorization: Bearer <VK>` 형식 확인 |
| `403 permission_error` | 해당 모델이 키 스코프 밖 | 운영자에게 `team_allowed_models` 확인 요청 |
| `404 not_found` | 모델 alias 오타 | `claude-sonnet-4-6` 등 등록된 alias 사용 |
| Claude Code 가 게이트웨이로 안 감 | `ANTHROPIC_BASE_URL` 미설정 또는 settings.json 우선순위 | `echo $ANTHROPIC_BASE_URL` 확인. `~/.claude/settings.json` 의 `env` 가 환경변수보다 우선할 수 있음 |
| Cowork 가 여전히 bedrock | config 파일이 `appliedId` 가 가리키는 게 아님 / 앱이 안 껐다 켜짐 | `_meta.json` 의 `appliedId` 재확인, 앱 완전 종료 후 교체·재시작 |
| **Cowork "provider setup needs a fix"** | **HTTP baseUrl (Cowork 는 `originPinned:true` 로 HTTP origin 거부)** 또는 `inferenceCredentialKind` 누락 | **HTTPS(CloudFront) URL 사용** + `inferenceCredentialKind:"static"` 추가 (§B-3) |
| **Cowork "Server is busy. Retrying…"** | 위와 동일(HTTP 거부) 또는 게이트웨이 5xx | HTTPS URL 로 교체. §B-4 셀프체크로 게이트웨이 200 확인 |
| **Cowork "API key rejected / rotated or revoked"** | **VK 만료 (OIDC VK 는 1시간 수명)** | §A-2 로 VK 재발급(`gateway-cli login` 부터) → config 갱신 → 앱 재시작 |
| 대시보드에 안 보임 | 비용 집계는 비동기(cost:stream→worker) | 수십 초 후 반영. `client=other` 면 UA 식별 실패 — UA 헤더 확인 |

---

# D. 빠른 요약 (치트시트)

**Claude Code 붙이기:**
```bash
export ANTHROPIC_BASE_URL="http://<GATEWAY_HOST>"
export ANTHROPIC_AUTH_TOKEN="<VK>"   # gateway-cli login 쓰면 자동
claude
```
**Claude Code 떼기:** `unset ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN`

**Cowork 붙이기 (요약):**
```bash
# 1) VK 발급 (1h 만료)
gateway-cli login --issuer-url "https://cognito-idp.ap-northeast-2.amazonaws.com/<COGNITO_USER_POOL_ID>" --client-id "<OIDC_CLIENT_ID>"
# 2) §A-2(B) 로 VK 추출 → §B-3 으로 config 교체(HTTPS + inferenceCredentialKind:"static")
# 3) Cowork ⌘Q 완전종료 후 재시작
```
**Cowork 떼기:** ⌘Q → `<appliedId>.json.ORIGINAL` 복원 → 재시작 (§B-5)

---

# 부록. CloudFront(HTTPS) 생성 — Cowork 필수 인프라

## 왜 CloudFront 인가 — 기업 환경 ALB 와의 차이

**기업 운영 환경이라면 CloudFront 가 필요 없다.** 보통 기업은 자사 도메인(예: `gateway.company.com`)을 보유하고 거기에 ACM 공인 인증서를 발급해 ALB 의 **HTTPS:443 리스너**에 직접 붙인다. 그러면 Cowork 가 요구하는 HTTPS origin 이 ALB 자체로 충족되고, 앞단 CloudFront 가 불필요하다.

**우리 dev(333) 는 그 전제가 빠져 있다:**
- 보유 도메인·ACM 인증서가 없다. ALB 는 기본 DNS(`k8s-...elb.amazonaws.com`)만 가진다.
- 이 `*.amazonaws.com` 호스트명에는 **공인 ACM 인증서를 발급받을 수 없다** — ACM 공인 인증서는 *내가 소유·제어하는 도메인*에만 나오는데 `amazonaws.com` 은 AWS 소유다. → ALB 에 HTTPS:443 리스너를 만들 방법이 없다(인증서 없이는 리스너 생성 불가). 자체서명 인증서를 올려도 Cowork 가 신뢰하지 않아 TLS 실패.

**그래서 CloudFront 가 우회로다:** CloudFront 는 자기 도메인(`*.cloudfront.net`)에 대한 **무료 관리형 공인 인증서를 AWS 가 자동 제공**한다. 도메인 0개로도 즉시 HTTPS 엔드포인트가 생긴다. CloudFront 가 앞단에서 HTTPS 를 종단(redirect-to-https)하고, origin 으로는 기존 ALB 에 http 로 전달한다(AllViewer 헤더 보존).

**정리 — 두 클라이언트의 경로 차이:**
- **Claude Code (CLI)**: HTTP origin 을 그대로 수용 → `ANTHROPIC_BASE_URL` 이 ALB(HTTP) 를 직접 가리킨다. CloudFront **우회**.
- **Cowork (데스크톱 앱)**: config 스키마가 `originPinned:true` 로 HTTP origin 을 거부 → CloudFront(HTTPS) 를 **반드시 경유**.
- 두 경로 모두 종착지는 동일한 gateway-proxy ALB → 팟. CloudFront 는 ALB 를 대체하는 게 아니라 그 앞에 HTTPS 한 겹을 입히는 어댑터다.
- **운영 전환 시**: 자사 도메인 + ACM 이 준비되면 ALB 에 HTTPS:443 을 직접 켜고 CloudFront 를 제거하거나(또는 CDN/WAF 용도로 유지), Cowork 의 `inferenceGatewayBaseUrl` 을 그 도메인으로 바꾸면 된다. dev 의 CloudFront 는 "도메인 없이 HTTPS 를 얻기 위한 dev 전용 우회"다.

---

Cowork 는 HTTPS origin(`originPinned`)을 요구하므로 게이트웨이 ALB(HTTP) 앞에 CloudFront 를 세운다. **dev(333) 는 이미 생성됨**: `https://<CLOUDFRONT_DOMAIN>` (분포 ID `<CLOUDFRONT_DISTRIBUTION_ID>`).

새 환경에서 만들 때 (계정 가드 필수 — 333 확인):
```bash
export AWS_PROFILE=llm-gateway AWS_REGION=ap-northeast-2
aws sts get-caller-identity --query Account --output text   # == 333344445555 확인
ALB="<GATEWAY_HOST>"  # gateway-proxy ALB
# dist-config 템플릿(POST/SSE, http-only origin, redirect-to-https, *.cloudfront.net 인증서)에 ALB 주입
jq --arg ref "llm-gw-cowork-333-$(date +%s)" --arg alb "$ALB" \
   --arg c "LLM gateway-proxy HTTPS front for Claude CoWork" \
   '.CallerReference=$ref | .Comment=$c | .Origins.Items[0].DomainName=$alb' \
   dist-config.json > /tmp/cf.json
aws cloudfront create-distribution --distribution-config file:///tmp/cf.json \
   --query 'Distribution.{Id:Id,Domain:DomainName,Status:Status}' --output json
aws cloudfront wait distribution-deployed --id <위 Id>   # ~15-20분(실측 2분)
```
> `dist-config.json` 템플릿은 `cowork-llm-gateway-main.zip` 의 `cloudfront/dist-config.json` 재사용. (해당 zip 의 `create.sh` 는 config.env 에 DIST_ID 키가 없을 때 `set -e`+grep 으로 죽는 버그가 있어 위처럼 jq 로 직접 렌더했다.)
> **삭제:** `cloudfront/delete.sh` 또는 `get-distribution-config → disable → delete-distribution`.

**Cowork 붙이기:** 앱 종료 → `~/Library/Application Support/Claude-3p/configLibrary/<appliedId>.json` 백업 → `inferenceProvider:"gateway"` + baseUrl + VK 로 교체 → 앱 재시작

**Cowork 떼기:** 앱 종료 → `.ORIGINAL` 백업 복원 → 앱 재시작

---

## 부록 — 관련 문서
- 상세 Cowork 운영 가이드 + 식별 실측: [`COWORK-GATEWAY-SETUP.md`](COWORK-GATEWAY-SETUP.md)
- Claude Code vs Cowork 헤더 식별 근거: [`../COWORK-vs-CLAUDE-CODE.md`](../COWORK-vs-CLAUDE-CODE.md)
- 전체 아키텍처 + 데이터플로우: [`../../ARCHITECTURE.md`](../../ARCHITECTURE.md)
- 사용자 가이드(gateway-cli 상세): [`user-guide.md`](user-guide.md)

> **엔드포인트 주의**: 이 문서의 URL 은 **dev(계정 333, HTTP ALB)** 기준이다. 운영/HTTPS(CloudFront) 전환 시 `inferenceGatewayBaseUrl` / `ANTHROPIC_BASE_URL` 만 그 값으로 교체하면 절차는 동일하다.
