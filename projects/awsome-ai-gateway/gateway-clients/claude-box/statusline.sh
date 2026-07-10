#!/bin/sh
# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
#
# Claude Code statusline — 게이트웨이 /v1/usage/me 를 폴링해 예산/사용량을 한 줄로 출력.
# settings.json 의 statusLine.command 가 매 턴 이걸 실행하고 stdout 을 하단에 표시한다.
# env: ANTHROPIC_BASE_URL(게이트웨이), ANTHROPIC_AUTH_TOKEN(VK). (claude-box 가 주입)
GW="${ANTHROPIC_BASE_URL:-}"
VK="${ANTHROPIC_AUTH_TOKEN:-}"
[ -z "$GW" ] || [ -z "$VK" ] && { printf 'gateway: (env 없음)'; exit 0; }

python3 - "$GW" "$VK" <<'PY' 2>/dev/null || printf 'gateway: usage 조회 실패'
import sys, json, urllib.request
gw, vk = sys.argv[1], sys.argv[2]
try:
    req = urllib.request.Request(gw.rstrip('/') + "/v1/usage/me",
                                 headers={"Authorization": "Bearer " + vk})
    d = json.load(urllib.request.urlopen(req, timeout=4))
except Exception:
    print("gateway: usage 조회 실패"); sys.exit(0)
u = d.get("usage", {}); b = d.get("budget", {})
cost = float(u.get("total_cost_usd", 0) or 0)
tok = int(u.get("total_tokens", 0) or 0)
maxb = float(b.get("max_usd", 0) or 0)
rem = float(b.get("remaining_usd", 0) or 0)
period = d.get("period", "")
# 예산 설정돼 있으면 남은/한도, 아니면 소진만.
if maxb > 0:
    pct = b.get("pct", 0)
    seg = f"예산 ${rem:.2f}/${maxb:.0f} 남음 ({pct:.0f}% 사용)"
else:
    seg = f"소진 ${cost:.4f} (예산 미설정)"
print(f"🛡 gateway {period} · {seg} · {tok:,} tok")
PY
