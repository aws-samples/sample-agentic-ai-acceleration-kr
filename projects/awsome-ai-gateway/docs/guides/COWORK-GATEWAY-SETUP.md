# Cowork → 우리 LLM Gateway 연결 운영 가이드

> **목적**: Claude 데스크톱 앱의 **Cowork** 추론(inference)을 **우리 사내 LLM Gateway**(`llm-gateway-dev`, 계정 `333344445555`)로 라우팅한다.
> 그러면 Cowork 트래픽이 게이트웨이를 거치면서 **식별(identification) · 로깅 · 거버넌스(예산·rate-limit·모델 접근제어·사용량 추적)** 가 적용된다.
>
> **읽는 사람**: 운영자/엔드유저. 위에서 아래로 **그대로 따라 하면** 된다.
>
> **표기 규칙** — 각 단계는 둘 중 하나로 표시한다:
> - ✅ **verified procedure** — `COWORK-vs-CLAUDE-CODE.md` §C 의 **실측 검증된** 절차. 그대로 동작이 확인됨.
> - ⏳ **pending endpoint provisioning** — 절차/구성은 확정이지만, **우리 게이트웨이의 Cowork용 공인 HTTPS 진입점(CloudFront)이 아직 미배포** 상태라 그 URL이 생겨야 실행 가능하다. (Phase 4 예정)
>
> **솔직성 고지(중요)**:
> - 우리 게이트웨이 데이터플레인은 `llm-gateway-dev`(계정 `333344445555`)에 떠 있다.
> - 하지만 **Cowork가 요구하는 공인 HTTPS 진입점(CloudFront)은 아직 프로비저닝되지 않았다.** 그래서 게이트웨이의 공개 HTTPS URL이 필요한 모든 자리에 다음 placeholder를 쓴다:
>   `<COWORK_GATEWAY_HTTPS_URL — to be provisioned via CloudFront, see Phase 4>`
>   **임의의 URL을 지어내지 않는다.** 이 가이드는 그 엔드포인트가 생기는 즉시 그대로 사용 가능하다.
> - 게이트웨이 인증 키(Virtual Key)가 필요한 자리에는 `<VIRTUAL_KEY>` placeholder를 쓴다. 실제 키는 **admin-api 가 발급**한다.
> - `cowork-llm-gateway-main/` 레포는 **다른 AWS 계정(`444455556666`)** 의 참고용(reference) 자산이다. 거기에 박혀 있는 URL/좌표(`<REFERENCE_CLOUDFRONT_DOMAIN>` 등)는 **패턴 참고용일 뿐 우리 계정 값이 아니다 — 복사 금지.**

---

## 0. 배경 — Cowork의 추론 모드 2가지

Cowork(= Claude 데스크톱 앱 안의 에이전트 탭)는 `inferenceProvider` 키 하나로 백엔드를 고른다:

| 모드 | 의미 | 거버넌스 |
|------|------|----------|
| `bedrock` | **AWS Bedrock 직결.** 앱이 직접 Bedrock을 호출(예: us-west-2, Bearer 토큰). **게이트웨이를 안 거친다.** | ❌ 우리가 보거나 통제 못 함 |
| `gateway` | **우리 LLM Gateway로 라우팅** → 게이트웨이가 뒤에서 Bedrock으로 프록시. | ✅ 식별·로깅·예산·접근제어 가능 |

우리가 원하는 것 = **`gateway` 모드**. Cowork를 우리 게이트웨이를 가리키게 바꾸면, 게이트웨이가 뒤에서 Bedrock을 호출하면서 모든 요청을 식별·기록한다.

> Cowork와 Claude Code는 **같은 엔진**이라 헤더가 거의 같다. 게이트웨이가 둘을 구분하는 **결정적 단서는 User-Agent 괄호 안의 surface 토큰** 하나다(§5 검증 참고). Cowork 트래픽은 `claude-desktop-3p` / `local-agent` 로 나타난다.

---

## 1. 사전 준비 (공통)

✅ **verified procedure** (백업·재시작 절차) / ⏳ 게이트웨이 URL 부분만 pending

1. **Claude 데스크톱 앱(Cowork)** 이 Mac에 설치되어 있어야 한다. (실측 기준 버전 `1.13576.4`. surface 토큰은 버전 의존이라 큰 버전업 시 §5 재확인.)
2. 게이트웨이의 **Cowork용 공인 HTTPS 진입점 URL** 을 확보한다:
   `<COWORK_GATEWAY_HTTPS_URL — to be provisioned via CloudFront, see Phase 4>`
   ⏳ **아직 없음.** 이 URL이 생기기 전에는 §3(로컬 프로브 테스트)까지만 실행 가능하다. §2/§4 의 실제 게이트웨이 연결은 이 URL이 나와야 한다.
3. **Virtual Key** `<VIRTUAL_KEY>` 를 발급받는다. **admin-api 가 발급**한다(운영자/관리자에게 요청). 키가 정말 불필요한 테스트라도 Cowork는 **빈 값을 거부**하므로 placeholder 문자열이라도 채워야 한다.
4. **현재 config를 반드시 백업한다**(원복 보장). 아래 §6 의 백업 절차를 먼저 수행해 두는 것을 권장한다.

> ⚠️ Cowork는 **시작할 때 config를 읽는다.** 어떤 방법으로 바꾸든 마지막엔 **Cowork 완전 종료(Cmd+Q) → 재실행** 이 필요하다(§4 참고). 앱이 켜진 채로는 반영 안 됨.

---

## 2. gateway 모드 설정의 정확한 키 (레퍼런스)

✅ **verified procedure** — 아래 키 형태는 `COWORK-vs-CLAUDE-CODE.md` §C 실측에서 쓴 그대로다.
⏳ `inferenceGatewayBaseUrl` 값(우리 게이트웨이 HTTPS)만 pending.

`gateway` 모드 config 한 장의 전체 예시(JSON):

```json
{
  "inferenceProvider": "gateway",
  "inferenceGatewayBaseUrl": "<COWORK_GATEWAY_HTTPS_URL — to be provisioned via CloudFront, see Phase 4>",
  "inferenceGatewayApiKey": "<VIRTUAL_KEY>",
  "inferenceGatewayAuthScheme": "bearer",
  "inferenceModels": [
    { "name": "global.anthropic.claude-opus-4-7", "supports1m": true }
  ],
  "inferenceCustomHeaders": {
    "X-Client-App": "cowork"
  }
}
```

키 설명:

| 키 | 값/의미 | 필수 |
|----|---------|------|
| `inferenceProvider` | `"gateway"` 로 설정 → 게이트웨이 모드 활성 | ✅ 필수 |
| `inferenceGatewayBaseUrl` | 게이트웨이 호스트. **문서상 HTTPS 필수.** (실측에선 `http://` 로컬 프로브도 통했으나 운영은 HTTPS — §3.5 TLS 함정 참고) | ✅ 필수 |
| `inferenceGatewayApiKey` | 게이트웨이 키(`<VIRTUAL_KEY>`). **빈 값 불가.** 키 불필요면 placeholder 문자열이라도 넣는다. | ✅ 필수 |
| `inferenceGatewayAuthScheme` | `"bearer"`(→ `Authorization: Bearer <키>`) 또는 `"x-api-key"`. 우리 게이트웨이는 Bearer VK 방식이므로 **`bearer`**. | ✅ 필수 |
| `inferenceModels` | 모델 목록. 게이트웨이가 `GET /v1/models`(모델 자동발견)를 구현하면 생략 가능. **없으면 여기에 명시**한다. | 게이트웨이 의존 |
| `inferenceCustomHeaders` | (선택) 운영자 식별용 임의 헤더. **식별엔 사실 불필요** — 게이트웨이는 UA로 Cowork를 알아챈다(§5). 운영 라벨링 용도로만. | 선택 |

> **게이트웨이가 구현해야 하는 계약**: `POST /v1/messages`(스트리밍 SSE + tool use) **필수**, `GET /v1/models`(모델 자동발견) **선택**. (참고: `cowork-probe/probe_http.py` 가 이 두 엔드포인트를 흉내 내 실측을 했다.)

---

## 3. 경로 (a) — 로컬 단일 기기 테스트

두 갈래가 있다: **(a) 로컬 단일 기기 테스트**(이 절), **(b) config 파일 직접 수정**(§4), 그리고 함대 배포용 **MDM `.mobileconfig`**(§4.4).

### 3.1 앱 UI로 적용 (Apply locally)

✅ **verified procedure**(UI 경로·키 형태) / ⏳ 게이트웨이 URL pending

1. Cowork 앱 → **`Developer → Configure third-party inference`** 진입.
2. **Connection: `Gateway`** 선택.
3. Gateway credentials 입력:
   - **Gateway URL** = `<COWORK_GATEWAY_HTTPS_URL — to be provisioned via CloudFront, see Phase 4>`
   - **API Key** = `<VIRTUAL_KEY>`
   - **Auth scheme** = `bearer`
   - **Models** = `global.anthropic.claude-opus-4-7` (필요 시 추가)
4. **`Apply locally`** 클릭 → **MDM 불필요, 이 기기/이 계정에만** 적용된다(함대 배포 아님).
5. **Cowork 완전 종료(Cmd+Q) → 재실행** (§4.3).

> 이 화면에서 **`Export`** 를 누르면 함대 배포용 `.mobileconfig`(macOS) / `.reg`(Windows) 가 생성된다 → §4.4.

### 3.2 (URL이 아직 없을 때) 로컬 프로브로 헤더 캡처 검증

✅ **verified procedure** — `COWORK-vs-CLAUDE-CODE.md` §C "안전한 실측 절차" 그대로. 게이트웨이 URL 없이 **지금 당장** 할 수 있다.

게이트웨이 HTTPS가 아직 없으므로, **로컬 프로브 서버**(`cowork-probe/probe_http.py`)로 Cowork가 실제로 `gateway` 모드로 요청을 보내는지, 어떤 헤더를 붙이는지 확인한다.

1. **현재 config 백업** (§6 절차). 원복 보장.
2. 로컬 프로브 서버 기동 — `/v1/models` + `/v1/messages`(SSE) 를 흉내 내고 받은 헤더를 전부 로깅한다. 포트 `8480`:
   ```bash
   python3 /path/to/llm-gateway/cowork-probe/probe_http.py
   ```
   (프로브는 dual-stack `::` 바인딩이라 `localhost`가 IPv6 `::1` 로 풀려도 도달한다 — §3.5 참고.)
3. config를 아래로 바꾼다(§4 파일 직접 방식 또는 §3.1 UI):
   ```json
   {
     "inferenceProvider": "gateway",
     "inferenceGatewayBaseUrl": "http://127.0.0.1:8480",
     "inferenceGatewayApiKey": "probe-placeholder",
     "inferenceGatewayAuthScheme": "bearer",
     "inferenceModels": [ { "name": "global.anthropic.claude-opus-4-7", "supports1m": true } ]
   }
   ```
4. **Cowork 완전 종료 → 재시작** → 메시지 1개(`hi`) 전송.
5. 프로브 로그(`cowork-probe/captures_http.log`)에서 실제 헤더 확인(§5).
6. **백업 복원** → Cowork 재시작 → 원래(`bedrock` 직결)대로(§6).

### 3.5 ⚠️ TLS 함정 (실측에서 막혔던 부분 — 꼭 읽을 것)

✅ **verified procedure** — `COWORK-vs-CLAUDE-CODE.md` §C 실측.

- **자체서명(self-signed) HTTPS는 Cowork가 거부한다.** Cowork는 Electron/Chromium net stack을 쓰는데, Chromium은 **`NODE_EXTRA_CA_CERTS` 를 보지 않는다**(Node CA 번들 무시). 그래서 자체서명 인증서를 신뢰시키는 흔한 우회가 **안 통한다.**
- **테스트 우회**: **HTTP 평문** baseUrl(`http://127.0.0.1:8480`)은 통한다(문서는 HTTPS 필수라지만 실측 통과). dev에서 헤더만 캡처할 땐 HTTP로 충분.
- **운영**: **진짜 공인 인증서**가 필요하다. 우리 계획은 게이트웨이 앞에 **CloudFront** 를 세워 무료 관리형 인증서(`*.cloudfront.net`)로 TLS를 종단하는 것이다 — 그래서 `inferenceGatewayBaseUrl` 이 이 CloudFront URL(`<COWORK_GATEWAY_HTTPS_URL …>`)을 가리킨다. ⏳ **이 CloudFront 진입점이 Phase 4 미배포 대상**이다.
  - (참고 패턴, 다른 계정) CloudFront는 캐시를 끄고(**CachingDisabled**), 모든 헤더를 통과(**AllViewer**), `Compress=false` 로 둬서 `Authorization: Bearer <VK>` 헤더와 **SSE 스트리밍**을 보존한다. ALB는 HTTP(80) 전용이라 `OriginProtocolPolicy=http-only` 로 붙인다.
- **`127.0.0.1` vs `localhost` 주의**: `localhost` 는 Electron이 IPv6(`::1`)로 해석할 수 있다. 서버를 dual-stack(`::`)으로 바인딩하거나, baseUrl에 **`127.0.0.1`** 을 직접 쓴다. (`probe_http.py` 는 dual-stack으로 이미 처리되어 있다.)

---

## 4. 경로 (b) — config 파일 직접 수정

✅ **verified procedure**(파일 위치·키 형태·재시작) / ⏳ 게이트웨이 URL pending

UI를 거치지 않고 config JSON을 직접 고치는 방법. 자동화/스크립트에 적합하다.

### 4.1 config 파일 위치

```
~/Library/Application Support/Claude-3p/configLibrary/<uuid>.json
```

- `<uuid>` 는 기기마다 다르다. 어떤 항목이 적용 중인지는 **같은 폴더의 `_meta.json`** 의 `appliedId` 가 가리킨다.
  실측 예(이 기기):
  ```json
  // _meta.json
  { "appliedId": "<APPLIED_CONFIG_UUID>",
    "entries": [ { "id": "<APPLIED_CONFIG_UUID>", "name": "Default" } ] }
  ```
  → 적용 중 파일은 `configLibrary/<APPLIED_CONFIG_UUID>.json`.

- **현재(예시) 원본은 `bedrock` 직결 형태**다 (`cowork-config-backup/` 의 `.ORIGINAL` 백업이 이 모양):
  ```json
  {
    "disableDeploymentModeChooser": true,
    "inferenceProvider": "bedrock",
    "inferenceBedrockRegion": "us-west-2",
    "inferenceBedrockBearerToken": "<원본 Bedrock Bearer 토큰 — 그대로 보존>",
    "inferenceModels": [ { "name": "global.anthropic.claude-opus-4-7", "supports1m": true } ],
    "deploymentOrganizationUuid": "<org-uuid>",
    "disabledBuiltinTools": []
  }
  ```

### 4.2 gateway 모드로 교체

1. **백업 먼저**(§6).
2. `appliedId` 가 가리키는 `<uuid>.json` 을 §2 의 gateway 모드 JSON으로 교체한다. `bedrock` 전용 키(`inferenceBedrockRegion`, `inferenceBedrockBearerToken`)는 제거하고 gateway 키로 바꾼다:
   ```json
   {
     "inferenceProvider": "gateway",
     "inferenceGatewayBaseUrl": "<COWORK_GATEWAY_HTTPS_URL — to be provisioned via CloudFront, see Phase 4>",
     "inferenceGatewayApiKey": "<VIRTUAL_KEY>",
     "inferenceGatewayAuthScheme": "bearer",
     "inferenceModels": [ { "name": "global.anthropic.claude-opus-4-7", "supports1m": true } ],
     "inferenceCustomHeaders": { "X-Client-App": "cowork" }
   }
   ```
   (`deploymentOrganizationUuid`, `disabledBuiltinTools` 같은 비추론 키는 원본 값을 보존해도 된다.)

### 4.3 재시작 요구사항 (필수)

✅ **verified procedure**

Cowork는 **시작 시에만 config를 읽는다.** 따라서:

1. **Cowork(Claude 데스크톱 앱) 완전 종료** — Cmd+Q (단순 창 닫기 ❌).
2. **재실행.**
3. 모델 선택 후 대화 1개 전송 → 정상 응답이면 게이트웨이 경유 성공(§5 로 확정).

### 4.4 (참고) 함대 배포용 MDM `.mobileconfig`

⏳ **pending endpoint provisioning** — 절차는 확정, 게이트웨이 URL 필요.

여러 대를 한 번에 배포하려면 **MDM 관리형 프로파일**(macOS `.mobileconfig`, Windows `.reg`)을 쓴다. §3.1 UI의 **`Export`** 로 생성하거나, 프로그램으로 만든다.

- **패턴 참고**(다른 계정 `444455556666` 레포): `cowork-llm-gateway-main/client/macos/install-cowork-llm-gateway.py` 가 `com.anthropic.claudefordesktop` payload를 가진 `.mobileconfig` 를 생성한다. 핵심은 payload 안에 `inferenceProvider: "gateway"`, `inferenceGatewayBaseUrl`, `inferenceGatewayAuthScheme: "bearer"`, `inferenceModels` 를 넣는 것 — §2 와 **동일한 키**다.
- 정적 키 대신 **자동 갱신 헬퍼**(`inferenceCredentialHelper` = 절대경로 스크립트, TTL ~1800s)를 쓰면 VK 만료 시 자동 재발급된다. 헬퍼는 **stdout에 bare 토큰 한 줄**만 출력해야 하고(`Bearer` 접두어는 Cowork가 붙임), 진단 로그는 stderr로 보낸다. (참고: `cowork-gw-credential-helper.sh`.)
- **우리 계정용으로 쓸 땐** `LLM_GATEWAY_URL`/모델/OIDC/admin-api 좌표를 **우리 `333344445555` 값으로 바꿔야 한다.** 레포에 박힌 `<REFERENCE_CLOUDFRONT_DOMAIN>`·`444455556666` 좌표를 **그대로 복사 금지.**
- 설치(macOS): `open <생성된 .mobileconfig>` → System Settings에서 프로파일 승인 → Cowork Cmd+Q 후 재시작.
- ⚠️ **MDM로 배포된 프로파일은 앱 UI가 read-only** 가 된다("Organization-managed"). 원복은 **MDM 프로파일 제거**로만 가능(§6).

---

## 5. 검증 — 요청이 진짜 게이트웨이에 닿았나 / 식별이 되나

✅ **verified procedure** — UA surface 토큰은 `cowork-probe/captures_http.log` 에 **실측 캡처됨**.

### 5.1 게이트웨이에 도달했는지

- **로컬 프로브 테스트**(§3.2): `cowork-probe/captures_http.log` 에 `POST /v1/messages` 요청과 헤더가 찍히면 도달 성공.
- **실제 게이트웨이**(URL 배포 후): 게이트웨이 로그(`llm-gateway-dev`)에 Cowork의 `POST /v1/messages` 가 찍히는지 확인. 모델 응답이 정상 스트리밍되면 라우팅 성공.

### 5.2 식별(identification)이 되는지 — UA surface 토큰

게이트웨이가 Cowork를 알아채는 **결정적 단서**는 User-Agent 괄호 안의 surface 토큰이다. 실측 캡처:

```
User-Agent: claude-cli/2.1.177 (external, claude-desktop-3p)
User-Agent: claude-cli/2.1.177 (external, local-agent, agent-sdk/0.3.177)
anthropic-client-platform: desktop_app          ← 일부 요청에 동반
```

게이트웨이의 식별 로직(우선순위 — **Cowork를 먼저 검사**):

```python
ua = headers.get("user-agent", "")
platform = headers.get("anthropic-client-platform", "")
if (platform == "desktop_app"
        or "claude-desktop-3p" in ua
        or "local-agent" in ua
        or ("Electron/" in ua and "Claude/" in ua)):   # Cowork 헬스체크 요청
    client = "cowork"
elif ua.startswith("claude-cli/") and ("(external, cli" in ua or "sdk-cli" in ua):
    client = "claude-code"
elif ua.startswith("claude-cli/"):
    client = "claude-code"        # surface 불명 → 보수적
```

→ 위 조건이 맞으면 게이트웨이가 해당 트래픽을 **`cowork` 로 태깅**한다. 이걸 로그/대시보드에서 확인하면 식별 검증 완료.

> ⚠️ **함정**: `x-app: cli`, `anthropic-beta: claude-code-20250219`, `X-Stainless-Lang: js`, `X-Claude-Code-Session-Id` 등은 **Cowork와 Claude Code 둘 다** 보낸다 — 식별에 쓰면 오판. 또 `claude-cli/` prefix도 둘 다 가지므로 **반드시 surface 토큰으로 갈라야 하고, Cowork 체크를 먼저** 해야 한다. (실측에서 `x-app: cli` 가 Cowork 요청에도 그대로 찍혔다.)

---

## 6. 원복 (revert) — 원래대로 되돌리기

✅ **verified procedure** — `COWORK-vs-CLAUDE-CODE.md` §C "원복".

### 6.1 백업 만들기 (변경 전 필수)

```bash
cp "~/Library/Application Support/Claude-3p/configLibrary/<uuid>.json" \
   "~/Library/Application Support/Claude-3p/configLibrary/<uuid>.json.ORIGINAL"
```

(우리는 `cowork-config-backup/d5ef301d-...json.ORIGINAL` 로 백업해 뒀다. `_meta.json` 도 함께 백업 권장.)

### 6.2 복원 방법 (셋 중 하나)

1. **파일 백업 복원(가장 확실)**: 백업 JSON을 `configLibrary/<uuid>.json` 에 덮어쓰기 → **Cowork 재시작.** 원래 `bedrock` 직결로 복귀.
2. **앱 UI**: 로그인/sign-in 화면에서 **Anthropic sign-in** 선택 → 표준 Cowork로 복귀.
3. **MDM 배포본인 경우**: UI가 read-only 라 위 방법이 안 통함 → **MDM 프로파일 제거**(System Settings → Privacy & Security → Profiles 에서 해당 프로파일 삭제)로만 원복.

---

## 부록 — 이 가이드의 단계별 상태 요약

| 단계 | 내용 | 상태 |
|------|------|------|
| §0 | 추론 모드 2가지 배경 | ✅ verified |
| §1.1, §1.3–1.4 | 앱 버전 확인 · VK placeholder · 백업 권고 | ✅ verified |
| §1.2 | 게이트웨이 공인 HTTPS URL 확보 | ⏳ pending (CloudFront 미배포) |
| §2 | gateway 모드 config 키 형태 / JSON 예시 | ✅ verified (URL 값만 ⏳) |
| §3.1 | 앱 UI `Apply locally` 경로 | ✅ verified (URL 값만 ⏳) |
| §3.2 | 로컬 프로브로 헤더 캡처 검증 | ✅ verified (지금 실행 가능) |
| §3.5 | TLS 함정(자체서명 거부, HTTP 우회, 127.0.0.1) | ✅ verified |
| §4.1–4.3 | config 파일 직접 수정 · 위치 · 재시작 | ✅ verified (URL 값만 ⏳) |
| §4.4 | MDM `.mobileconfig` 함대 배포 | ⏳ pending (URL 필요) + 패턴은 verified |
| §5 | 검증(게이트웨이 도달 · UA 식별) | ✅ verified (UA 실측 캡처됨) |
| §6 | 원복(파일 복원 · UI sign-in · MDM 제거) | ✅ verified |

> **요약**: 절차 자체(config 키, UI 경로, 파일 위치, 재시작, TLS 주의, 식별 로직, 원복)는 **전부 실측 검증됨**. 유일하게 ⏳ 인 것은 **우리 게이트웨이의 Cowork용 공인 HTTPS 진입점(CloudFront) URL** — 이것이 Phase 4 에서 배포되면 `<COWORK_GATEWAY_HTTPS_URL …>` placeholder만 채워 **즉시 사용 가능**하다.

---

### 관련 파일 (이 레포)
- 실측 근거: `COWORK-vs-CLAUDE-CODE.md` §C (이 가이드의 PRIMARY 소스)
- 프로브: `cowork-probe/probe_http.py`(HTTP, 포트 8480), 캡처 로그 `cowork-probe/captures_http.log`
- config 백업(원본 bedrock 형태): `cowork-config-backup/d5ef301d-...json.ORIGINAL`, `_meta.json.ORIGINAL`
- 패턴 참고(다른 계정 444455556666, 복사 금지): `cowork-llm-gateway-main/`
  (`client/macos/install-cowork-llm-gateway.py`, `client/macos/cowork-gw-credential-helper.sh`, `cloudfront/dist-config.json`)
