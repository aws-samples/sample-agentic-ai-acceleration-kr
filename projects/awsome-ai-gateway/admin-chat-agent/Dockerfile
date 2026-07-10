# syntax=docker/dockerfile:1.7
# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# AgentCore Runtime 컨테이너 contract:
# - 8080 포트에 /invocations + /ping HTTP endpoint 노출 (BedrockAgentCoreApp 자동 처리)
# - non-root user 권장
# - linux/arm64 필수 — AgentCore Runtime 은 Graviton(arm64) microVM 에서만 실행.
#   amd64 image 를 push 하면 CreateAgentRuntime 이 ValidationException
#   ("Architecture incompatible ... Supported platforms: [arm64]") 반환.
#   빌드: finch build --platform linux/arm64 ...

FROM public.ecr.aws/docker/library/python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 최소 system 패키지 — strands 가 grpc/protobuf 빌드에 필요할 수 있음
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates \
        build-essential \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src

RUN pip install --upgrade pip \
 && pip install -e .

# AgentCore HTTP contract
EXPOSE 8080

# non-root
RUN useradd -m -u 1000 agent && chown -R agent:agent /app
USER agent

CMD ["python", "-m", "agent.main"]
