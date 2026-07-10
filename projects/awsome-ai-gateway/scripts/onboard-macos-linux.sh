#!/usr/bin/env bash
# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
#
# LLM Gateway 온보딩 (macOS / Linux) — gateway-cli 설치 + OIDC 로그인 + (선택) Claude Code 연동.
# 세 클라이언트(Claude Code/Codex/Cowork) 공통의 "1단계"를 자동화한다. Codex/Cowork 별 설정은
# guides/QUICKSTART.md §2-B/§2-C 참고.
#
# 사용:
#   OIDC_ISSUER_URL=... OIDC_CLIENT_ID=... ADMIN_API_URL=... ANTHROPIC_BASE_URL=... \
#     ./scripts/onboard-macos-linux.sh [--setup-claude-code]
#
# 안전: 이 스크립트는 gateway-cli 설치와 `gateway-cli login` 만 수행한다(=~/.gateway-cli/ 에만 기록).
#       --setup-claude-code 를 줄 때만 Claude Code settings 를 건드린다(gateway-cli setup).
set -euo pipefail

log()  { printf "\033[1;36m[onboard]\033[0m %s\n" "$*"; }
err()  { printf "\033[1;31m[onboard:err]\033[0m %s\n" "$*" >&2; }

# ── 0. 필수 env 확인 ─────────────────────────────────────────────
missing=()
for v in OIDC_ISSUER_URL OIDC_CLIENT_ID ADMIN_API_URL ANTHROPIC_BASE_URL; do
  [ -n "${!v:-}" ] || missing+=("$v")
done
if [ "${#missing[@]}" -gt 0 ]; then
  err "다음 환경변수가 필요합니다(운영자에게 문의): ${missing[*]}"
  err "예) export OIDC_ISSUER_URL=... ; export OIDC_CLIENT_ID=... ; export ADMIN_API_URL=... ; export ANTHROPIC_BASE_URL=..."
  exit 1
fi

SETUP_CC=0
[ "${1:-}" = "--setup-claude-code" ] && SETUP_CC=1

# ── 1. gateway-cli 설치 (uv 격리 우선, 없으면 안내) ──────────────
if command -v gateway-cli >/dev/null 2>&1; then
  log "gateway-cli 이미 설치됨: $(gateway-cli version 2>/dev/null || echo '?')"
elif command -v uv >/dev/null 2>&1 && [ -d "./gateway-cli" ]; then
  log "uv 로 gateway-cli 격리 설치 (제거: uv tool uninstall gateway-cli)"
  uv tool install --from ./gateway-cli gateway-cli
else
  err "gateway-cli 미설치 + 자동 설치 불가."
  err "  옵션 A) 운영자 패키지: curl -L <URL> -o gw.tgz && tar xzf gw.tgz && sudo mv gateway-cli api-key-helper /usr/local/bin/"
  err "  옵션 B) 소스+uv: 저장소 루트에서 uv tool install --from ./gateway-cli gateway-cli"
  exit 1
fi

# ── 2. 게이트웨이 헬스 확인 (비치명적) ──────────────────────────
code="$(curl -s -o /dev/null -w '%{http_code}' "${ANTHROPIC_BASE_URL%/}/health" || echo 000)"
if [ "$code" = "200" ]; then log "게이트웨이 health: 200 OK"; else err "게이트웨이 health 응답: $code (계속 진행하나 확인 권장)"; fi

# ── 3. OIDC 로그인 (브라우저 PKCE) ──────────────────────────────
log "OIDC 로그인 — 브라우저가 열립니다..."
gateway-cli login --issuer-url "$OIDC_ISSUER_URL" --client-id "$OIDC_CLIENT_ID"
log "로그인 완료. 토큰 캐시: ~/.gateway-cli/oidc-tokens.json"

# ── 4. (선택) Claude Code 연동 ─────────────────────────────────
if [ "$SETUP_CC" = "1" ]; then
  log "Claude Code 연동 (gateway-cli setup) — sudo 암호를 물을 수 있습니다"
  gateway-cli setup --gateway-url "$ANTHROPIC_BASE_URL" --admin-api-url "$ADMIN_API_URL"
  log "완료. Claude Code 재시작 후 'claude' 실행하면 게이트웨이로 갑니다. 원복: gateway-cli disable"
else
  log "공통 1단계(로그인) 완료. Claude Code 자동연동을 원하면 --setup-claude-code 로 재실행."
  log "Codex/Cowork 는 guides/QUICKSTART.md §2-B/§2-C 참고 (VK 는 부록 스니펫으로 발급)."
fi
