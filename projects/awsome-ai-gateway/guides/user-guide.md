# LLM Gateway — 사용자 가이드 (End-User Guide)

이 문서는 LLM Gateway를 통해 **Claude Code를 사용하는 개발자**를 위한 가이드입니다.
총 5분이면 셋업이 끝나고, 이후 자동으로 인증이 관리됩니다.

---

## 목차

1. [개요](#1-개요)
2. [사전 준비](#2-사전-준비)
3. [설치](#3-설치)
4. [첫 로그인](#4-첫-로그인)
5. [Claude Code 연동](#5-claude-code-연동)
6. [일상 사용](#6-일상-사용)
7. [예산 & 제한](#7-예산--제한)
8. [자주 묻는 질문 (FAQ)](#8-자주-묻는-질문-faq)
9. [트러블슈팅](#9-트러블슈팅)
10. [보안 안내](#10-보안-안내)
11. [부록 A: 명령어 요약](#부록-a-명령어-요약)

---

## 1. 개요

### 1.1 LLM Gateway란?

LLM Gateway는 회사에서 Claude AI를 **안전하고 효율적으로** 사용할 수 있게 해주는 프록시 서비스입니다.

```
Claude Code (내 PC) → Gateway (회사 서버) → AWS Bedrock (Claude AI)
```

### 1.2 왜 직접 API 안 쓰나요?

| 직접 API 사용 | Gateway 사용 |
|---------------|-------------|
| 개인 API 키 관리 필요 | 회사 SSO로 자동 인증 |
| 비용 관리 어려움 | 팀/개인별 예산 자동 관리 |
| 모델 접근 제한 불가 | 팀별 허용 모델 관리 |
| 사용 추적 불가 | 자동 사용량/ROI 추적 |
| 보안 위험 (키 유출) | VK 자동 만료 + 즉시 폐기 |

### 1.3 지원 도구

현재 지원하는 AI 개발 도구:
- **Claude Code** (CLI 및 IDE 확장)
- **Codex** (OpenAI Codex CLI)
- **Cowork** (Claude 데스크톱 앱)

> 본 문서는 **Claude Code** 연동을 기준으로 설명합니다. Codex/Cowork 연동은
> [guides/QUICKSTART.md](QUICKSTART.md)(3-client 빠른 시작)를 참조하세요.

---

## 2. 사전 준비

운영자(Admin)에게 받아야 할 정보:

| 항목 | 예시 | 확인 |
|------|------|------|
| IDP에 등록된 본인 이메일 | `myname@company.com` | ☐ |
| 임시 패스워드 | (운영자가 알려줌) | ☐ |
| Hosted UI URL | `https://xxx.auth.ap-northeast-2.amazoncognito.com/login?...` | ☐ |
| OIDC Issuer URL | `https://cognito-idp.ap-northeast-2.amazonaws.com/ap-northeast-2_xxx` | ☐ |
| OIDC Client ID | `1a2b3c4d5e6f7g8h` | ☐ |
| Gateway URL | `https://gateway.llm-gateway.example.com` | ☐ |
| Admin API URL (VK 자동 발급용) | `https://admin-api.llm-gateway.example.com` | ☐ |

> 위 정보를 받지 못했다면 운영자에게 먼저 문의하세요.

---

## 3. 설치

### 3.1 방법 A: 운영자 제공 패키지 (권장)

**macOS / Linux**:
```bash
curl -L "<운영자 제공 download URL>" -o gateway-cli.tar.gz
tar xzf gateway-cli.tar.gz
sudo mv gateway-cli /usr/local/bin/
sudo mv api-key-helper /usr/local/bin/
```

**Windows** (관리자 권한 PowerShell):
```powershell
Invoke-WebRequest -Uri "<운영자 제공 download URL>" -OutFile gateway-cli.zip
Expand-Archive gateway-cli.zip -DestinationPath "$env:ProgramFiles\GatewayCLI"
# PATH에 추가
[Environment]::SetEnvironmentVariable("Path", $env:Path + ";$env:ProgramFiles\GatewayCLI", "Machine")
```

**설치되는 바이너리 2개**:

| 바이너리 | 역할 |
|----------|------|
| `gateway-cli` | 셋업/로그인/상태확인 CLI |
| `api-key-helper` | Claude Code가 자동 호출하는 VK 발급 헬퍼 |

### 3.2 방법 B: 소스에서 설치 (개발자)

```bash
git clone <repo> && cd LLM-Gateway-Vanilla/gateway-cli
python3 -m venv .venv
.venv/bin/pip install -e .
```

이 경우 실행 파일 경로: `.venv/bin/gateway-cli`, `.venv/bin/api-key-helper`

### 3.3 설치 확인

```bash
gateway-cli version
# 출력: gateway-cli 0.1.0
```

---

## 4. 첫 로그인

### 4.1 환경 변수 설정

운영자가 알려준 값으로 셸 프로파일에 추가합니다. 이 환경변수는 `gateway-cli`와 `api-key-helper`가 사용합니다:

```bash
# ~/.zshrc 또는 ~/.bashrc 에 추가
export OIDC_ISSUER_URL="https://cognito-idp.ap-northeast-2.amazonaws.com/ap-northeast-2_xxx"
export OIDC_CLIENT_ID="1a2b3c4d5e6f7g8h"
export ADMIN_API_URL="https://admin-api.llm-gateway.example.com"
export ANTHROPIC_BASE_URL="https://gateway.llm-gateway.example.com"
```

적용:
```bash
source ~/.zshrc
```

> **참고**: 5단계에서 생성하는 settings.json의 `env` 블록은 **Claude Code 프로세스**에 주입되는 값입니다. 셸 환경변수는 `gateway-cli`/`api-key-helper`용이고, settings.json은 Claude Code용으로 역할이 다릅니다.

### 4.2 (최초 1회) 임시 패스워드 변경

1. 브라우저에서 운영자가 알려준 **Hosted UI URL** 접속
2. 이메일 + 임시 패스워드 입력
3. 새 패스워드 설정 (요구사항: 8자 이상, 대소문자+숫자+특수문자)
4. 설정 완료 → 브라우저 닫기

### 4.3 CLI 로그인

```bash
gateway-cli login
```

동작:
1. 브라우저가 자동으로 IDP 로그인 페이지를 엽니다
2. 이메일 + 새 패스워드 입력
3. "Login successful" 메시지 확인
4. 터미널로 돌아옴

출력 예:
```
Login successful.
  IDP:       https://cognito-idp.ap-northeast-2.amazonaws.com/ap-northeast-2_xxx
  Client ID: 1a2b3c4d5e6f7g8h
  Token TTL: 3600s
  Refresh:   yes (auto-refresh enabled)
```

확인:
```bash
ls -la ~/.gateway-cli/oidc-tokens.json
# -rw------- 1 user user ... (mode 0600)
```

---

## 5. Claude Code 연동

### 5.1 gateway-cli setup (권장)

한 줄 명령으로 Claude Code 설정을 자동 생성합니다:

```bash
gateway-cli setup \
  --gateway-url https://gateway.llm-gateway.example.com \
  --admin-api-url https://admin-api.llm-gateway.example.com
```

> Password 입력창이 뜰 경우: 시스템 디렉토리(`/etc/claude-code/managed-settings.d/`)에 쓰기 위해 `sudo` 권한이 필요합니다. 본인 PC 로그인 패스워드를 입력하세요.

출력:
```
  Gateway URL:     https://gateway.llm-gateway.example.com
  Admin API URL:   https://admin-api.llm-gateway.example.com
  API Key Helper:  /usr/local/bin/api-key-helper

  Gateway enabled: /etc/claude-code/managed-settings.d/50-gateway.json

Restart Claude Code to apply changes.
```

이 명령은 `/etc/claude-code/managed-settings.d/50-gateway.json`에 설정을 기록합니다. Claude Code는 이 경로를 최우선으로 읽습니다.

### 5.2 수동 설정 (gateway-cli setup을 사용할 수 없는 경우)

managed-settings.d에 쓰기 권한이 없거나 직접 제어하고 싶을 때 사용합니다:

```bash
mkdir -p ~/.config/Claude
cat > ~/.config/Claude/settings.json <<'EOF'
{
  "apiKeyHelper": "/usr/local/bin/api-key-helper",
  "env": {
    "ANTHROPIC_BASE_URL": "<운영자 안내값>",
    "ADMIN_API_URL": "<운영자 안내값>",
    "OIDC_ISSUER_URL": "<운영자 안내값>",
    "OIDC_CLIENT_ID": "<운영자 안내값>"
  }
}
EOF
```

> `<운영자 안내값>` 부분을 2단계에서 설정한 환경변수 값으로 교체하세요.

소스 설치한 경우 `apiKeyHelper` 경로를 조정:
```json
"apiKeyHelper": "/path/to/.venv/bin/api-key-helper"
```

> **참고**: `gateway-cli setup`은 managed-settings.d에, 수동 설정은 `~/.config/Claude/settings.json`에 기록됩니다. 경로는 다르지만 Claude Code가 둘 다 인식합니다. 두 곳에 동시 설정하면 managed-settings.d가 우선합니다.

### 5.3 Claude Code 시작

```bash
claude
```

첫 실행 시:
1. Claude Code가 자동으로 `api-key-helper` 호출
2. OIDC 토큰으로 VK(Virtual Key) 자동 발급
3. Gateway를 통해 Claude에 연결
4. 첫 메시지를 보내면 성공!

---

## 6. 일상 사용

최초 로그인 이후에는 VK가 자동 발급/갱신되므로, 별도 인증 작업 없이 Claude Code를 바로 사용하면 됩니다.

### 6.1 자동 인증 관리

| 상황 | 동작 |
|------|------|
| Claude Code 실행 | 자동으로 캐시된 VK 사용 |
| VK 만료 (기본 1시간) | `api-key-helper`가 자동으로 새 VK 발급 |
| OIDC ID Token 만료 (~1시간) | Refresh Token으로 자동 갱신 |

### 6.2 재로그인이 필요한 경우

| 빈도 | 조건 | 명령 |
|------|------|------|
| Refresh Token 만료 시 | 운영자 설정에 따라 다름 (보통 7~30일) | `gateway-cli login` |
| 팀 이동 후 | 운영자가 IDP 그룹 변경 | `gateway-cli login` |
| 운영자 요청 시 | 보안 정책 변경 | `gateway-cli login` |

### 6.3 상태 확인

```bash
gateway-cli status
```

출력:
```
Gateway CLI Status
==================================================
  Managed settings: /etc/claude-code/managed-settings.d/50-gateway.json

  Gateway: [ON]
    Base URL:        https://gateway.llm-gateway.example.com
    Admin API URL:   https://admin-api.llm-gateway.example.com
    API Key Helper:  /usr/local/bin/api-key-helper
==================================================
```

### 6.4 Gateway 비활성화 (직접 API 사용으로 전환)

```bash
gateway-cli disable
```

→ Claude Code가 직접 API 접근으로 돌아감. 재활성화: `gateway-cli setup`

### 6.5 로그아웃

```bash
gateway-cli logout
# ~/.gateway-cli/{oidc-tokens.json, vk-cache.json} 삭제
```

---

## 7. 예산 & 제한

### 7.1 예산 정책

운영자가 팀/개인에 설정한 월간 예산:

| 정책 | 의미 |
|------|------|
| `HARD_BLOCK` | 예산 소진 시 즉시 차단 (가장 일반적) |
| `SOFT_WARNING` | 110%까지 허용 후 차단 |
| `THROTTLE` | 예산 압박 시 속도 제한 |

예산 소진 시 메시지:
```
Error: 429 budget_exceeded
Your monthly budget has been exhausted. Contact your admin.
```

### 7.2 Rate Limit

분당/시간당 요청 제한:

| 제한 유형 | 의미 |
|-----------|------|
| RPM | 분당 요청 수 |
| TPM | 분당 토큰 수 |
| CPM | 분당 비용 (USD) |
| CPH | 시간당 비용 (USD) |

Rate Limit 초과 시 메시지:
```
Error: 429 rate_limit_error
Retry-After: 15
```
→ 15초 후 자동 재시도됩니다.

### 7.3 모델 접근 제한

팀별로 사용 가능한 모델이 다를 수 있습니다:
- 허용되지 않은 모델 요청 시: `403 Forbidden`
- 사용 가능한 모델 확인: Claude Code에서 `/model` 명령

### 7.4 자동 모델 다운그레이드

**팀 예산 사용률**이 높아지면 운영자 설정에 따라 자동으로 저가 모델로 전환될 수 있습니다:
- 예: 팀 예산 80% 도달 시 Opus → Sonnet 자동 전환
- 응답 헤더 `X-Downgraded-From`으로 확인 가능
- 다음 달 시작 시 자동 복원

> **TEAM scope 전용**: 자동 다운그레이드는 본인이 속한 **팀 예산**의
> threshold 기반으로만 발동합니다. 개인 예산이 80% 가 되어도 자동
> 다운그레이드는 적용되지 않으며, 개인 예산 한도 초과는 `429 budget_exceeded`
> 로 차단됩니다 (§7.1). 개인 예산 절약은 본인이 직접 모델을 골라 호출하세요.

---

## 8. 자주 묻는 질문 (FAQ)

### Q: VK(Virtual Key)가 뭔가요?

API 호출 시 사용되는 임시 인증 토큰입니다. `vk-` 접두사를 가지며 자동 발급/갱신됩니다. 개인 관리 불필요.

### Q: 매번 로그인해야 하나요?

아닙니다. 
- VK는 OIDC 모드 기본 1시간 캐시 → `api-key-helper`가 자동 갱신
- OIDC ID Token ~1시간 → Refresh Token으로 자동 갱신
- Refresh Token 만료 시 → `gateway-cli login` 필요 (만료 기간은 운영자 설정에 따라 다름, 보통 7~30일)

### Q: 여러 PC에서 사용 가능한가요?

네. 각 PC에서 `gateway-cli login` 실행하면 됩니다. 단, 1인 1키 정책이므로 새 VK 발급 시 이전 PC의 VK는 자동 만료됩니다.

### Q: 어떤 모델을 쓸 수 있나요?

Claude Code에서 `/model` 명령으로 사용 가능한 모델 목록을 확인할 수 있습니다. 팀별로 다를 수 있으며, 운영자에게 추가 모델 요청 가능합니다.

### Q: 캐시(Prompt Caching)는 지원하나요?

네. Gateway가 Bedrock의 Prompt Caching을 투명하게 지원합니다. 별도 설정 불필요.

---

## 9. 트러블슈팅

### 9.1 인증 관련

| 에러 | 원인 | 해결 |
|------|------|------|
| `Login failed: redirect port 8090 is busy` | 다른 앱이 포트 점유 | `gateway-cli login --redirect-port 8091` |
| `OIDC token error: not logged in` | 토큰 없음/만료 | `gateway-cli login` |
| `OIDC token error: refresh failed: HTTP 401` | Refresh Token 만료 | `gateway-cli login` |
| `401 Unauthorized` | VK 만료/폐기됨 | `gateway-cli login` (자동 재발급) |
| `403 Forbidden: user inactive` | 계정 비활성화됨 | 운영자에게 문의 |

### 9.2 예산/제한 관련

| 에러 | 원인 | 해결 |
|------|------|------|
| `429 budget_exceeded` | 월간 예산 소진 | 운영자에게 예산 상향 요청 |
| `429 rate_limit_error` (RPM) | 분당 요청 초과 | 잠시 대기 (Retry-After 참고) |
| `429 rate_limit_error` (TPM) | 분당 토큰 초과 | 긴 프롬프트 분할 또는 대기 |
| `429 team_budget_unset` | 팀 예산 미설정 | 운영자에게 팀 예산 활성화 요청 |
| `403 model_not_allowed` | 팀에 모델 비허용 | 운영자에게 모델 허용 요청 |

### 9.3 연결 관련

| 에러 | 원인 | 해결 |
|------|------|------|
| `Connection refused` | Gateway 서버 다운 | 운영자에게 연락 |
| `timeout` | 네트워크 또는 서버 부하 | 잠시 후 재시도 |
| `502 Bad Gateway` | ALB → Pod 연결 문제 | 운영자에게 연락 |
| `503 Service Unavailable` | 서비스 일시 중단 | 운영자에게 연락 |

### 9.4 디버깅 방법

```bash
# 1) 현재 상태 확인
gateway-cli status

# 2) 상세 로그 출력
gateway-cli login --verbose

# 3) 토큰 파일 확인
ls -la ~/.gateway-cli/
# oidc-tokens.json  — OIDC 토큰 (있어야 함)
# vk-cache.json     — VK 캐시 (있어야 함)

# 4) Gateway 연결 테스트
curl -s https://gateway.llm-gateway.example.com/health
# {"status":"ok"} 이 나와야 정상

# 5) 모델 목록 확인 (VK 필요)
curl -H "Authorization: Bearer $(cat ~/.gateway-cli/vk-cache.json | python3 -c 'import json,sys; print(json.load(sys.stdin)["virtual_key"])')" \
  https://gateway.llm-gateway.example.com/v1/models
```

---

## 10. 보안 안내

### 10.1 보호해야 할 파일

| 파일 | 내용 | 보호 |
|------|------|------|
| `~/.gateway-cli/oidc-tokens.json` | Refresh Token 포함 | mode 0600 (자동) |
| `~/.gateway-cli/vk-cache.json` | Virtual Key 평문 | mode 0600 (자동) |

### 10.2 주의사항

- 위 파일들을 **다른 사람과 공유하지 마세요**
- Git에 커밋하지 마세요 (`.gitignore`에 이미 포함)
- 다른 PC로 복사하지 마세요 — 각 PC에서 `gateway-cli login`으로 새로 발급
- 공용 PC에서는 작업 후 반드시 `gateway-cli logout`

### 10.3 PC 도용 의심 시

즉시:
1. 다른 기기에서 `gateway-cli login` (새 VK 발급 → 이전 VK 자동 만료)
2. 운영자에게 VK revoke 요청 (즉시 차단)
3. IDP 패스워드 변경

---

## 부록 A: 명령어 요약

| 명령 | 설명 |
|------|------|
| `gateway-cli version` | 버전 확인 |
| `gateway-cli setup --gateway-url <URL>` | Claude Code에 Gateway 연동 설정 |
| `gateway-cli login` | OIDC 로그인 (브라우저 PKCE flow) |
| `gateway-cli logout` | 토큰 + VK 캐시 삭제 |
| `gateway-cli status` | 현재 설정 상태 확인 |
| `gateway-cli disable` | Gateway 비활성화 (직접 API로 전환) |

### 환경 변수

| 변수 | 용도 | 필수 |
|------|------|------|
| `OIDC_ISSUER_URL` | IDP 주소 | ✅ |
| `OIDC_CLIENT_ID` | OIDC 클라이언트 ID | ✅ |
| `ADMIN_API_URL` | Admin API 주소 (VK 발급) | ✅ |
| `ANTHROPIC_BASE_URL` | Gateway Proxy 주소 (API 호출) | ✅ |
| `OIDC_AUDIENCE` | (선택) OIDC audience | — |
| `GATEWAY_CLI_LANG` | 언어 (en/ko) | — |
| `GATEWAY_CLI_VERBOSE` | 디버그 로그 (true/false) | — |

### 설정 파일 우선순위 (높은 순)

1. CLI 옵션 (`--gateway-url`, `--verbose` 등)
2. 환경 변수 (`GATEWAY_CLI_*`, `OIDC_*`)
3. config.yaml (`~/.config/gateway-cli/config.yaml`)

### config.yaml 예시

`~/.config/gateway-cli/config.yaml` (선택 — 환경변수로 대체 가능):

```yaml
gateway_url: "https://gateway.llm-gateway.example.com"
lang: ko
verbose: false
connect_timeout: 5
read_timeout: 10
```

---

## 빠른 시작 (TL;DR)

```bash
# 1. 설치
curl -L "<download URL>" -o gateway-cli.tar.gz && tar xzf gateway-cli.tar.gz
sudo mv gateway-cli api-key-helper /usr/local/bin/

# 2. 환경변수 (운영자 안내값)
export OIDC_ISSUER_URL="..."
export OIDC_CLIENT_ID="..."
export ADMIN_API_URL="..."
export ANTHROPIC_BASE_URL="..."

# 3. 로그인
gateway-cli login

# 4. Claude Code 설정
gateway-cli setup --gateway-url $ANTHROPIC_BASE_URL --admin-api-url $ADMIN_API_URL

# 5. 사용 시작
claude
```

문제 발생 시: 운영자에게 **본인 이메일 + 발생 시각 + 에러 메시지**를 전달하세요.

---

## 부록 B: 현재 운영 환경 값 (참고)

> 이 절은 본 deliverable 의 **현재 배포된 prod 환경**(AWS 계정 `123456789012`,
> 리전 `ap-northeast-2`)을 기준으로 한 실제 값입니다. 도메인/ACM 적용 전이므로
> ALB DNS 직접 접근(평문 HTTP)이며, 운영 출시 전에는 HTTPS+도메인으로 교체
> 됩니다.

### B.1 prod (default — 신규 사용자에게 안내하는 값)

```bash
# Cognito (OIDC IDP)
export OIDC_ISSUER_URL="https://cognito-idp.ap-northeast-2.amazonaws.com/ap-northeast-2_XXXXXXXXX"
export OIDC_CLIENT_ID="<COGNITO_APP_CLIENT_ID>"

# Hosted UI (브라우저 로그인 페이지)
# https://llm-gateway-prod-vanilla-auth-123456789012.auth.ap-northeast-2.amazoncognito.com/login

# ALB endpoints (도메인 적용 전 — 평문 HTTP)
export ANTHROPIC_BASE_URL="http://<ALB_DNS>"
export ADMIN_API_URL="http://<ALB_DNS>"

# Admin UI (관리자 대시보드 — 운영자만)
# http://<ALB_DNS>
```

### B.2 dev (개발/검증용 — 일반 사용자는 사용하지 않음)

```bash
export OIDC_ISSUER_URL="https://cognito-idp.ap-northeast-2.amazonaws.com/ap-northeast-2_XXXXXXXXX"
export OIDC_CLIENT_ID="<COGNITO_APP_CLIENT_ID>"
export ANTHROPIC_BASE_URL="http://<ALB_DNS>"
export ADMIN_API_URL="http://<ALB_DNS>"
```

> 두 환경 모두 동일한 Cognito 그룹 컨벤션을 사용합니다 (`ClaudeAdmin`,
> `Claude_<team>`, `Claude_<dept>_<team>`). 이메일/그룹은 운영자가 부여합니다.
