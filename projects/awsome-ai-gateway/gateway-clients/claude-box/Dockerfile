# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
#
# claude-box — Claude Code CLI 를 LLM Gateway 로 붙이는 격리 컨테이너.
# 호스트 맥 환경(~/.claude 등)을 전혀 건드리지 않는다. VK 는 런타임 env 로 주입.
FROM public.ecr.aws/docker/library/node:20-slim

# 편의 도구(작업용) + python3(statusline 의 usage 폴링용). git 은 repo 컨텍스트.
RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates ripgrep less python3 \
    && rm -rf /var/lib/apt/lists/*

# Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code && npm cache clean --force

# statusline: Claude Code 하단에 게이트웨이 예산/사용량 한 줄 표시(/v1/usage/me 폴링).
# settings.json 의 statusLine.command 가 statusline.sh 를 매 턴 실행한다.
COPY --chown=node:node statusline.sh /home/node/.claude/statusline.sh
COPY --chown=node:node claude-settings.json /home/node/.claude/settings.json
RUN chmod +x /home/node/.claude/statusline.sh

# node:20-slim 은 uid 1000 'node' 유저를 기본 제공 — 그걸 비root 로 사용(호스트 볼륨 권한 호환).
USER node
WORKDIR /work

# ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN 은 docker run -e 로 주입(이미지에 굽지 않음).
# 기본은 셸 — gwclaude 스크립트가 `claude ...` 인자를 넘긴다.
ENTRYPOINT ["claude"]
