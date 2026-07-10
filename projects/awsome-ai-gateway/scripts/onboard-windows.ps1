# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
#
# LLM Gateway 온보딩 (Windows PowerShell) — gateway-cli 확인 + OIDC 로그인 + (선택) Claude Code 연동.
# 세 클라이언트 공통의 "1단계"를 자동화한다. Codex/Cowork 설정은 guides\QUICKSTART.md 참고.
#
# 사용 (PowerShell):
#   $env:OIDC_ISSUER_URL="..."; $env:OIDC_CLIENT_ID="..."; $env:ADMIN_API_URL="..."; $env:ANTHROPIC_BASE_URL="..."
#   .\scripts\onboard-windows.ps1 [-SetupClaudeCode]
#
# 안전: 기본은 gateway-cli login 만 수행(=%USERPROFILE%\.gateway-cli 에만 기록).
#       -SetupClaudeCode 를 줄 때만 Claude Code 설정을 변경(gateway-cli setup).
param([switch]$SetupClaudeCode)
$ErrorActionPreference = "Stop"

function Log($m) { Write-Host "[onboard] $m" -ForegroundColor Cyan }
function ErrLog($m) { Write-Host "[onboard:err] $m" -ForegroundColor Red }

# 0. 필수 env 확인
$need = "OIDC_ISSUER_URL","OIDC_CLIENT_ID","ADMIN_API_URL","ANTHROPIC_BASE_URL"
$missing = $need | Where-Object { -not [Environment]::GetEnvironmentVariable($_) }
if ($missing) {
  ErrLog "다음 환경변수가 필요합니다(운영자 문의): $($missing -join ', ')"
  ErrLog '예) $env:OIDC_ISSUER_URL="..." ; $env:OIDC_CLIENT_ID="..." ; $env:ADMIN_API_URL="..." ; $env:ANTHROPIC_BASE_URL="..."'
  exit 1
}

# 1. gateway-cli 설치 확인 (Windows 는 운영자 패키지/uv 로 사전 설치 가정)
if (Get-Command gateway-cli -ErrorAction SilentlyContinue) {
  Log "gateway-cli 확인됨: $(gateway-cli version 2>$null)"
} else {
  ErrLog "gateway-cli 미설치. 먼저 설치하세요:"
  ErrLog '  옵션 A) Invoke-WebRequest -Uri <URL> -OutFile gw.zip; Expand-Archive gw.zip -DestinationPath "$env:ProgramFiles\GatewayCLI"; PATH 등록'
  ErrLog '  옵션 B) uv tool install --from .\gateway-cli gateway-cli'
  exit 1
}

# 2. 게이트웨이 헬스 확인 (비치명적)
try {
  $r = Invoke-WebRequest -Uri "$($env:ANTHROPIC_BASE_URL.TrimEnd('/'))/health" -UseBasicParsing -TimeoutSec 10
  Log "게이트웨이 health: $($r.StatusCode)"
} catch { ErrLog "게이트웨이 health 확인 실패(계속 진행): $($_.Exception.Message)" }

# 3. OIDC 로그인
Log "OIDC 로그인 — 브라우저가 열립니다..."
gateway-cli login --issuer-url $env:OIDC_ISSUER_URL --client-id $env:OIDC_CLIENT_ID
Log "로그인 완료. 토큰 캐시: $env:USERPROFILE\.gateway-cli\oidc-tokens.json"

# 4. (선택) Claude Code 연동
if ($SetupClaudeCode) {
  Log "Claude Code 연동 (gateway-cli setup)"
  gateway-cli setup --gateway-url $env:ANTHROPIC_BASE_URL --admin-api-url $env:ADMIN_API_URL
  Log "완료. Claude Code 재시작 후 claude 실행. 원복: gateway-cli disable"
} else {
  Log "공통 1단계(로그인) 완료. Claude Code 자동연동은 -SetupClaudeCode 로 재실행."
  Log "Codex/Cowork 는 guides\QUICKSTART.md 참고. Cowork/대량배포는 운영자 MDM/.reg 권장."
}
