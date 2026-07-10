<!-- Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms. -->

# LLM Gateway — 빠른 시작 (3-client × Mac/Windows)

> **대상**: 사내 게이트웨이에 **Claude Code · Codex · Cowork** 를 붙이려는 개발자.
> **소요**: 최초 ~5분. 이후 인증(Virtual Key)은 자동 발급·갱신된다.
> 상세는 [user-guide.md](user-guide.md)(Claude Code) · [../docs/guides/connect.md](../docs/guides/connect.md)(Claude Code+Cowork) · [../gateway-clients/README.md](../gateway-clients/README.md)(격리 컨테이너) 참고.

---

## 0. 운영자에게 받을 값 (4개)

| 변수 | 예시 | 용도 |
|---|---|---|
| `OIDC_ISSUER_URL` | `https://cognito-idp.ap-northeast-2.amazonaws.com/ap-northeast-2_xxx` | IdP 주소 |
| `OIDC_CLIENT_ID` | `<COGNITO_APP_CLIENT_ID>` | OIDC 클라이언트 |
| `ADMIN_API_URL` | `https://admin-api.llm-gateway.example.com` | VK 발급 |
| `ANTHROPIC_BASE_URL` | `https://gateway.llm-gateway.example.com` | 추론 진입점(=게이트웨이) |

> 이 4개를 셸 프로파일(`~/.zshrc`/`~/.bashrc`) 또는 PowerShell 프로파일에 넣어두면 편하다.

---

## 1. 공통 — gateway-cli 설치 + 로그인 (세 클라이언트 공유)

세 클라이언트 무엇을 쓰든 **신원 인증(OIDC) → Virtual Key 발급** 흐름은 동일하고, `gateway-cli` 가 담당한다.

### macOS / Linux
```bash
# (A) 운영자 제공 패키지
curl -L "<운영자 download URL>" -o gateway-cli.tar.gz
tar xzf gateway-cli.tar.gz
sudo mv gateway-cli api-key-helper /usr/local/bin/

# (B) 소스에서 uv 격리 설치 (권장 — 깨끗한 제거 가능)
uv tool install --from ./gateway-cli gateway-cli

gateway-cli version
```

### Windows (관리자 PowerShell)
```powershell
Invoke-WebRequest -Uri "<운영자 download URL>" -OutFile gateway-cli.zip
Expand-Archive gateway-cli.zip -DestinationPath "$env:ProgramFiles\GatewayCLI"
[Environment]::SetEnvironmentVariable("Path", $env:Path + ";$env:ProgramFiles\GatewayCLI", "Machine")
gateway-cli version
```

### 로그인 (양 OS 공통)
```bash
gateway-cli login --issuer-url "$OIDC_ISSUER_URL" --client-id "$OIDC_CLIENT_ID"
```
- 브라우저 PKCE(S256) 로그인 → 토큰이 `~/.gateway-cli/oidc-tokens.json`(권한 0600)에 캐시.
- 이후 VK 발급·1시간 갱신은 `api-key-helper` 가 자동. **재로그인 불필요.**

---

## 2. 클라이언트별 연결

### 2-A. Claude Code (터미널 CLI)
```bash
# claude 설치 확인 (없으면)
npm install -g @anthropic-ai/claude-code

# 게이트웨이 연동 (settings 자동 작성)
gateway-cli setup --gateway-url "$ANTHROPIC_BASE_URL" --admin-api-url "$ADMIN_API_URL"

claude            # 이제 그냥 실행하면 게이트웨이로 감 (Bearer VK 자동 주입)
```
- 기록 위치: macOS `/etc/claude-code/managed-settings.d/gateway.json`(sudo) 또는 개인 `~/.config/Claude/settings.json`.
- **원복**: `gateway-cli disable` → 직접 API 로 복귀.

### 2-B. Codex (OpenAI Codex CLI)
Codex 는 OpenAI **Responses API** 방언을 쓴다. `~/.codex/config.toml` 에 gateway provider 를 넣는다:
```toml
model_provider = "gateway"

[model_providers.gateway]
base_url = "<ANTHROPIC_BASE_URL>/v1"   # 끝에 /v1
wire_api = "responses"
env_key  = "GATEWAY_VK"                # 아래 VK 를 이 env 로 참조
```
```bash
export GATEWAY_VK="<VK>"   # gateway-cli 로 발급 (아래 부록) → codex 실행
```
- 게이트웨이는 Codex 가 보내는 `originator: codex_cli_rs` 헤더로 `client=codex` 자동 식별.
- **격리 실행**(호스트 `~/.codex` 미접근): `gateway-clients/codex-box` 도커 이미지 사용 — 컨테이너 안에서만 config 생성. `gateway-clients/README.md` 참고.

### 2-C. Cowork (Claude 데스크톱 앱)
CLI 가 아니라 앱 config 파일을 편집한다.
- **macOS 경로**: `~/Library/Application Support/Claude-3p/configLibrary/<uuid>.json`
- **Windows**: 개인 편집보다 **운영자 MDM/.reg 배포 권장**(§3).

앱 종료 → 해당 `<uuid>.json` **백업** → 아래 4키 설정 → 앱 재시작:
```json
{
  "inferenceProvider": "gateway",
  "inferenceGatewayBaseUrl": "<ANTHROPIC_BASE_URL — 운영은 HTTPS 필수>",
  "inferenceGatewayApiKey": "<VK>",
  "inferenceGatewayAuthScheme": "bearer"
}
```
- 게이트웨이는 UA `…claude-desktop-3p` 로 `client=cowork` 식별 → routing_profiles 로 자동 라우팅.
- **원복**: 백업 JSON 복원 후 앱 재시작 → `inferenceProvider:"bedrock"` 직결 복귀.

> ⚠️ Cowork 는 문서상 `inferenceGatewayBaseUrl` 이 **HTTPS 필수**. 운영 전환 시 게이트웨이 앞 CloudFront(HTTPS) 진입점 필요.

---

## 3. 대량 배포 (운영자 — 함대 단위)

수작업 대신 **MDM 관리형 프로파일**로 배포한다.
- macOS: `.mobileconfig` · Windows: `.reg`
- Admin UI 의 **Export** 버튼이 생성(§ `docs/guides/COWORK-GATEWAY-SETUP.md`).
- Claude Code 는 `/etc/claude-code/managed-settings.d/`(우선순위 최상)로 배포하면 개인 설정보다 우선 적용.

---

## 부록 — VK 문자열만 직접 뽑기 (Codex/Cowork 수동용)

`gateway-cli login` 후, OIDC **id_token** 을 `/v1/auth/exchange` 로 교환한다
(Cognito access_token 에는 email/groups 신원 claim 이 없어 프로비저닝에 id_token 이 필수):
```bash
VK=$(python3 - "$ADMIN_API_URL" <<'PY'
import json, os, sys, urllib.request
api = sys.argv[1]
tok = json.load(open(os.path.expanduser("~/.gateway-cli/oidc-tokens.json")))["id_token"]
req = urllib.request.Request(api + "/v1/auth/exchange", method="POST",
    headers={"Authorization": "Bearer " + tok})
print(json.load(urllib.request.urlopen(req))["virtual_key"])
PY
)
echo "$VK"
```

## 명령어 요약
| 명령 | 설명 |
|---|---|
| `gateway-cli login` | OIDC 로그인(브라우저 PKCE) |
| `gateway-cli setup --gateway-url <URL> --admin-api-url <URL>` | Claude Code 연동 자동 설정 |
| `gateway-cli status` | 현재 설정 상태 |
| `gateway-cli disable` | 게이트웨이 비활성(직접 API 전환) |
| `gateway-cli logout` | 토큰·VK 캐시 삭제 |

## 트러블슈팅
- **게이트웨이 헬스 확인**: `curl -s -o /dev/null -w "%{http_code}\n" "$ANTHROPIC_BASE_URL/health"` → `200`.
- **claude 가 게이트웨이로 안 감**: `gateway-cli status` 로 base URL/apiKeyHelper 확인 → Claude Code 재시작.
- **401**: `gateway-cli login` 재실행(토큰 만료/그룹 변경). VK 는 자동 갱신이나 로그인 세션은 만료될 수 있음.
- **Cowork 400/연결 실패**: `inferenceGatewayBaseUrl` HTTPS 여부, 4키 모두 채웠는지, 앱 재시작 여부 확인.
