#!/usr/bin/env bash
# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
#
# gw.sh — 로컬 격리 컨테이너로 LLM Gateway 에 붙어 claude-code / codex 를 쓰는 헬퍼.
# 호스트 맥 환경(~/.claude, ~/.codex, 셸 설정)을 전혀 건드리지 않는다.
#
# 사용법:
#   ./gw.sh build                 # claude-box / codex-box 이미지 빌드 (최초 1회)
#   ./gw.sh vk                    # 호스트에서 VK 발급(OIDC 로그인) → ~/.gateway-vk 에 저장
#   ./gw.sh claude [args...]      # claude-box 컨테이너에서 claude 실행 (현재 디렉터리 마운트)
#   ./gw.sh codex  [args...]      # codex-box 컨테이너에서 codex 실행
#   ./gw.sh shell claude|codex    # 컨테이너 셸 진입(디버그)
#
# VK 는 ~/.gateway-vk 파일(plain VK 문자열, 600)에서 읽는다. 1시간 만료 → 만료 시 `gw.sh vk` 재실행.
set -euo pipefail

# ── 설정 (게이트웨이 dev ALB) ──────────────────────────────────────────────
GW_URL="${GW_URL:-http://<ALB_DNS>}"
ADMIN_URL="${ADMIN_URL:-http://<ALB_DNS>}"
OIDC_ISSUER_URL="${OIDC_ISSUER_URL:-https://cognito-idp.ap-northeast-2.amazonaws.com/ap-northeast-2_XXXXXXXXX}"
OIDC_CLIENT_ID="${OIDC_CLIENT_ID:-<COGNITO_APP_CLIENT_ID>}"
VK_FILE="$HOME/.gateway-vk"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# docker 또는 finch
ENGINE="${GW_ENGINE:-docker}"; command -v "$ENGINE" >/dev/null 2>&1 || ENGINE=finch

read_vk() {
  [ -f "$VK_FILE" ] || { echo "VK 없음. 먼저 './gw.sh vk' 실행." >&2; exit 1; }
  cat "$VK_FILE"
}

# 게이트웨이 /v1/usage/me 폴링 → 예산/사용량 한 줄 출력(stderr). claude/codex 실행 전 표시.
# Codex CLI 는 statusline 훅이 없어, 이 방식으로 예산을 보여준다(claude-box 는 자체 statusline 도 있음).
print_budget() {
  local VK; VK="$(cat "$VK_FILE" 2>/dev/null)"; [ -z "$VK" ] && return 0
  # 예산 한 줄을 stderr 로 출력(claude/codex 출력과 안 섞이게). 내부 트레이스만 버린다.
  python3 - "$GW_URL" "$VK" <<'PY' || true
import sys, json, urllib.request
gw, vk = sys.argv[1], sys.argv[2]
try:
    req = urllib.request.Request(gw.rstrip('/')+"/v1/usage/me", headers={"Authorization":"Bearer "+vk})
    d = json.load(urllib.request.urlopen(req, timeout=4))
except Exception:
    sys.exit(0)
u=d.get("usage",{}); b=d.get("budget",{})
cost=float(u.get("total_cost_usd",0) or 0); tok=int(u.get("total_tokens",0) or 0)
maxb=float(b.get("max_usd",0) or 0); rem=float(b.get("remaining_usd",0) or 0)
seg = (f"예산 ${rem:.2f}/${maxb:.0f} 남음 ({b.get('pct',0):.0f}% 사용)" if maxb>0
       else f"소진 ${cost:.4f} (예산 미설정)")
print(f"🛡 gateway {d.get('period','')} · {seg} · {tok:,} tok", file=sys.stderr)
PY
}

case "${1:-}" in
  budget)
    [ -f "$VK_FILE" ] || { echo "VK 없음. './gw.sh vk' 먼저." >&2; exit 1; }
    print_budget
    # 모델별 breakdown 도 같이
    VK="$(cat "$VK_FILE")" python3 - "$GW_URL" <<'PY' 2>/dev/null || true
import sys, os, json, urllib.request
gw=sys.argv[1]; vk=os.environ["VK"]
req=urllib.request.Request(gw.rstrip('/')+"/v1/usage/me", headers={"Authorization":"Bearer "+vk})
d=json.load(urllib.request.urlopen(req,timeout=5))
for m in d.get("model_breakdown",[]):
    print(f"   {m['model']}: ${float(m['cost_usd']):.4f} ({m['requests']}회, in {m['input_tokens']} / out {m['output_tokens']})")
PY
    ;;
  build)
    echo "[build] claude-box ..."; "$ENGINE" build -t claude-box "$HERE/claude-box"
    echo "[build] codex-box ...";  "$ENGINE" build -t codex-box  "$HERE/codex-box"
    echo "✅ 빌드 완료 (claude-box, codex-box)"
    ;;

  vk)
    # 호스트(맥)에서 OIDC 로그인 → VK 발급. gateway-cli 가 있으면 그걸로, 없으면 안내.
    if command -v gateway-cli >/dev/null 2>&1; then
      gateway-cli login --issuer-url "$OIDC_ISSUER_URL" --client-id "$OIDC_CLIENT_ID"
      VK="$(python3 - "$ADMIN_URL" <<'PY'
import json,os,sys,urllib.request
tok=json.load(open(os.path.expanduser("~/.gateway-cli/oidc-tokens.json")))["access_token"]
req=urllib.request.Request(sys.argv[1]+"/v1/auth/exchange",data=b"{}",method="POST",
  headers={"Authorization":"Bearer "+tok,"Content-Type":"application/json"})
print(json.load(urllib.request.urlopen(req,timeout=15))["virtual_key"])
PY
)"
      printf '%s' "$VK" > "$VK_FILE"; chmod 600 "$VK_FILE"
      echo "✅ VK 발급·저장 ($VK_FILE). 길이 ${#VK}. (1시간 만료 — 만료 시 './gw.sh vk' 재실행)"
    else
      echo "gateway-cli 가 호스트에 없습니다. 설치: uv tool install --from <repo>/gateway-cli gateway-cli" >&2
      echo "또는 발급한 VK 문자열을 직접 저장: printf '%s' '<VK>' > $VK_FILE && chmod 600 $VK_FILE" >&2
      exit 1
    fi
    ;;

  claude)
    shift
    VK="$(read_vk)"
    print_budget   # 실행 전 예산 한 줄 + claude-box 자체 statusline 도 표시됨
    exec "$ENGINE" run -it --rm \
      -e ANTHROPIC_BASE_URL="$GW_URL" \
      -e ANTHROPIC_AUTH_TOKEN="$VK" \
      -v "$PWD":/work \
      claude-box "$@"
    ;;

  codex)
    shift
    VK="$(read_vk)"
    print_budget   # Codex 는 statusline 훅이 없어 실행 전 예산을 여기서 표시
    exec "$ENGINE" run -it --rm \
      -e GW_URL="$GW_URL" \
      -e GATEWAY_VK="$VK" \
      -v "$PWD":/work \
      codex-box "$@"
    ;;

  shell)
    box="${2:-claude}"
    VK="$(read_vk)"
    if [ "$box" = "codex" ]; then
      exec "$ENGINE" run -it --rm --entrypoint /bin/sh -e GW_URL="$GW_URL" -e GATEWAY_VK="$VK" -v "$PWD":/work codex-box
    else
      exec "$ENGINE" run -it --rm --entrypoint /bin/bash -e ANTHROPIC_BASE_URL="$GW_URL" -e ANTHROPIC_AUTH_TOKEN="$VK" -v "$PWD":/work claude-box
    fi
    ;;

  *)
    sed -n '4,18p' "$HERE/gw.sh"
    ;;
esac
