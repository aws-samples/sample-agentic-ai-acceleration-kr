# Cowork vs Claude Code — 식별 & 게이트웨이 연결 (실측 정리)

> 2026-06-18 실측 기준. 두 클라이언트를 동일 로컬 프로브(`cowork-probe/`)로 캡처해 직접 비교.
> Cowork 버전 1.13576.4 / claude-cli 2.1.x 기준. surface 토큰은 비공식·버전 의존이라 버전업 시 재확인.

---

## A. 차이가 뭐냐? — 한 줄

**둘은 같은 엔진이라 헤더가 거의 같다. 유일하게 믿을 차이는 User-Agent 괄호 안의 한 단어다.**

```
Claude Code:  claude-cli/2.1.173 (external, cli)               ← "cli" / "sdk-cli"
Cowork:       claude-cli/2.1.177 (external, claude-desktop-3p)  ← "claude-desktop-3p" / "local-agent"
```

- `(external, cli)` 또는 `(external, sdk-cli)` → **Claude Code** (터미널 CLI)
- `(external, claude-desktop-3p)` 또는 `(external, local-agent, ...)` → **Cowork** (데스크톱 앱)

비유: 쌍둥이인데 명찰만 다름. 얼굴(나머지 헤더)은 같고, 가슴 명찰(UA 괄호 안 단어)만 보면 구분된다.

---

## B. 전체 비교표 (실측)

| 항목 | Claude Code | Cowork | 구분력 |
|------|-------------|--------|--------|
| 제품 형태 | 터미널 CLI (+IDE 확장) | Claude 데스크톱 앱 안의 에이전트 탭 | — |
| 엔진 | agentic core | **동일** agentic core | — |
| 런타임 | Node CLI | Electron(내부 Node + Chromium) | — |
| **UA surface 토큰** | `(external, cli)` / `(external, sdk-cli)` | `(external, claude-desktop-3p)` / `(external, local-agent, agent-sdk/...)` | ★★★ 결정적 |
| `anthropic-client-platform` | (없음) | `desktop_app` (일부 요청) | ★★ 강함(있을 때) |
| 헬스체크 요청 | (없음) | `...Electron/42.4.0 Claude/<ver>` UA + Sentry `baggage` | ★ 보조 |
| 설정 방식 | env (`ANTHROPIC_BASE_URL`, `CLAUDE_CODE_USE_BEDROCK`) + `~/.claude/settings.json` | 앱 UI(Developer→Configure third-party inference) / MDM | — |
| 설정 파일 | `~/.claude/settings.json` | `~/Library/Application Support/Claude-3p/configLibrary/*.json` | — |
| API 포맷 | `/v1/messages` (Anthropic Messages) | **동일** `/v1/messages` | 엔드포인트로는 구분 불가 |

### ⚠️ 차이가 없는 것 (공통 — 식별에 쓰면 오판)
- `x-app: cli` — **둘 다 보냄** ← 가장 흔한 실수(cc-router 등 OSS도 이걸로 오판)
- `anthropic-beta: claude-code-20250219` — 둘 다
- `X-Stainless-Lang: js` / `Runtime: node` — 둘 다 (둘 다 Anthropic JS SDK 기반)
- `X-Claude-Code-Session-Id` — 둘 다
- `anthropic-version: 2023-06-01` — 둘 다

### 식별 로직 (우선순위 — Cowork 먼저!)
```python
ua = headers.get("user-agent", "")
platform = headers.get("anthropic-client-platform", "")

if (platform == "desktop_app"
        or "claude-desktop-3p" in ua
        or "local-agent" in ua
        or ("Electron/" in ua and "Claude/" in ua)):      # Cowork 헬스체크
    client = "cowork"
elif ua.startswith("claude-cli/") and ("(external, cli" in ua or "sdk-cli" in ua):
    client = "claude-code"
elif ua.startswith("claude-cli/"):
    client = "claude-code"        # surface 불명 → 보수적
else:
    client = classify_other(ua)   # cursor / opencode / codex / @anthropic-ai/sdk / zed ...
```
**함정**: `claude-cli/` prefix는 둘 다 가짐 → 반드시 surface 토큰으로 갈라야 하고, **Cowork 체크를 먼저** 해야 Cowork가 claude-code로 오분류되지 않음.

---

## C. Cowork에 게이트웨이(Bedrock) 붙이는 방법 (실측 검증됨)

### 배경 — Cowork inference 모드 2가지
Cowork는 `inferenceProvider` 키로 백엔드를 고른다:
- **`bedrock`** = AWS Bedrock 직결 (사용자 현재 설정: us-west-2, Opus 4.7, Bearer 토큰). 게이트웨이 안 거침.
- **`gateway`** = 우리 LLM 게이트웨이로 라우팅 → 게이트웨이가 Bedrock으로 프록시. **예산/식별/로깅/접근제어 통제 가능.**

"게이트웨이로 Bedrock 사용" = `gateway` 모드로 바꿔 우리 게이트웨이를 가리키게 하고, 게이트웨이가 뒤에서 Bedrock을 호출.

### 설정 위치
- **앱 UI**: `Developer → Configure third-party inference` → Connection: **Gateway** → Gateway credentials 입력 → (MDM 배포 시 Export로 `.mobileconfig`/`.reg` 생성)
- **로컬 단독 테스트**: `Apply locally` (MDM 불필요, 이 기기/계정만)
- **설정 파일 직접**: `~/Library/Application Support/Claude-3p/configLibrary/<uuid>.json`

### gateway 모드 config 키 (실측에서 쓴 형태)
```json
{
  "inferenceProvider": "gateway",
  "inferenceGatewayBaseUrl": "https://<게이트웨이 호스트>",   // 문서상 https 필수 (실측: http://도 통함)
  "inferenceGatewayApiKey": "<게이트웨이 키>",               // 빈 값 불가; 키 불필요면 placeholder
  "inferenceGatewayAuthScheme": "bearer",                    // 또는 "x-api-key"
  "inferenceModels": [ { "name": "global.anthropic.claude-opus-4-7", "supports1m": true } ],
  "inferenceCustomHeaders": { "X-Client-App": "cowork" }     // (선택) 운영자 식별 헤더. 식별엔 사실 불필요(UA로 됨)
}
```
- 게이트웨이는 **`POST /v1/messages`**(스트리밍+tool use) 필수 구현, **`GET /v1/models`**는 선택(있으면 모델 자동발견, 없으면 `inferenceModels` 명시).

### ⚠️ TLS 함정 (실측에서 막혔던 부분)
- 자체서명 HTTPS는 Cowork(Electron/Chromium net stack)가 **거부**한다. `NODE_EXTRA_CA_CERTS`도 무효 — Chromium은 Node CA 번들을 안 본다.
- 우회(테스트용): **HTTP 평문** baseUrl(`http://127.0.0.1:PORT`)이 통했다. (문서는 https 필수라지만 실제 검증은 통과.)
- 운영: 진짜 공인 인증서(ACM+도메인) HTTPS 권장. dev에서 헤더 실측만 할 땐 http로 충분.
- `localhost`는 Electron이 IPv6(`::1`)로 해석할 수 있음 → 서버를 dual-stack(`::`) 바인딩하거나 baseUrl에 `127.0.0.1` 사용.

### 안전한 실측 절차 (헤더 캡처용, 우리가 한 그대로)
1. **현재 config 백업** (원복 보장): `configLibrary/<uuid>.json` 복사.
2. 로컬 프로브 서버 띄움 — `/v1/models`+`/v1/messages` SSE 흉내, 받은 헤더 전부 로깅 (`cowork-probe/probe_http.py`, 포트 8480).
3. config를 `inferenceProvider: gateway`, `inferenceGatewayBaseUrl: http://127.0.0.1:8480`로 변경.
4. **Cowork 완전 종료 → 재시작** (시작 시 config 읽음) → 메시지 1개("hi") 전송.
5. 프로브 로그에서 실제 헤더 확인.
6. **백업 복원** → Cowork 재시작 → 원래(bedrock 직결)대로.

### 원복 (revert)
- **파일 백업 복원**이 가장 확실: 백업 json을 `configLibrary/<uuid>.json`에 덮어쓰기 → Cowork 재시작.
- 또는 앱 UI: sign-in 화면에서 **Anthropic sign-in** 선택 → 표준 Cowork로 복귀.
- MDM 배포본은 UI가 read-only → MDM 프로파일 제거로만 원복.

---

## D. 관련 파일
- 실측 프로브: `cowork-probe/probe_http.py` (HTTP, 포트 8480), `probe_server.py` (HTTPS 8443), `cert.pem`/`key.pem` (자체서명, 7일)
- config 백업: `cowork-config-backup/d5ef301d-...json.ORIGINAL` (원본 bedrock 직결 설정)
- 상세 실측 로그/분석: `phase_devlog.md` §11(설계), §12(Cowork 실측), §13(양쪽 비교)
