#!/bin/sh
# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
#
# GW_URL(게이트웨이 ALB) + GATEWAY_VK(VK) env 로부터 codex config 를 생성하고 codex 실행.
set -eu

: "${GW_URL:?GW_URL 환경변수가 필요합니다 (게이트웨이 ALB)}"
: "${GATEWAY_VK:?GATEWAY_VK 환경변수가 필요합니다 (Virtual Key)}"
# Codex CLI 가 내장 메타데이터(context window 등)를 인식하는 실제 모델명을 준다.
# 실측: Codex 내장 테이블 키는 'gpt-5.5'(또는 'gpt-5.5-codex'). 'codex-gpt'(게이트웨이 alias)나
# 'openai.gpt-5.5'(Mantle provider 접두사형)는 테이블에 없어 "Model metadata not found" 경고 +
# fallback(성능저하). 'gpt-5.5' 를 주면 경고 없이 정확한 메타데이터 사용(실호출 200 확인).
# 게이트웨이는 codex client 를 routing_profile(default_model=codex-gpt)로 강제 라우팅하므로,
# 클라이언트 model 문자열은 무시되고 usage_logs·대시보드엔 codex-gpt 로 일관 기록(집계 영향 0).
MODEL="${GW_MODEL:-gpt-5.5}"

mkdir -p "$HOME/.codex"
cat > "$HOME/.codex/config.toml" <<TOML
model = "${MODEL}"
model_provider = "gateway"

[model_providers.gateway]
name = "LLM Gateway (Mantle GPT-5.5)"
base_url = "${GW_URL}/v1"
wire_api = "responses"
env_key = "GATEWAY_VK"
TOML

# codex 가 originator 헤더(codex_cli_rs)를 자동으로 보냄 → 게이트웨이가 client=codex 로 식별.
exec codex "$@"
