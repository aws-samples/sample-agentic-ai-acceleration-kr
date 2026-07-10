# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
#
# codex-box — OpenAI Codex CLI 를 LLM Gateway(Mantle GPT-5.5, Responses API)로 붙이는 격리 컨테이너.
# 호스트 맥 환경(~/.codex 등)을 건드리지 않는다. VK 는 런타임 env(GATEWAY_VK)로 주입,
# 게이트웨이 주소(GW_URL)도 env 로 받아 entrypoint 가 ~/.codex/config.toml 을 생성한다.
FROM public.ecr.aws/docker/library/node:20-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates ripgrep less gettext-base \
    && rm -rf /var/lib/apt/lists/*

# Codex CLI (OpenAI)
RUN npm install -g @openai/codex && npm cache clean --force

# node:20-slim 은 uid 1000 'node' 유저를 기본 제공 — 그걸 비root 로 사용.
USER node
WORKDIR /work

# entrypoint: env(GW_URL, GATEWAY_VK)로 codex config 를 생성한 뒤 codex 실행.
# config.toml: 게이트웨이를 OpenAI provider 로, wire_api=responses(GPT-5.x Responses API),
# env_key=GATEWAY_VK 로 Bearer 인증. model 은 게이트웨이 alias 'codex-gpt'(또는 openai.gpt-5.5).
COPY --chown=node:node entrypoint.sh /home/node/entrypoint.sh
ENTRYPOINT ["/home/node/entrypoint.sh"]
