# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# Build stage
FROM public.ecr.aws/docker/library/python:3.11-slim AS builder

# 빌드 도구도 고정한다 — :latest 는 움직이는 태그라 같은 커밋이라도 빌드마다 다른 uv 가 들어온다.
# 올릴 때는 태그와 digest 를 함께 바꾼다(docker buildx imagetools inspect ghcr.io/astral-sh/uv:<버전>).
COPY --from=ghcr.io/astral-sh/uv:0.13.4@sha256:9f130421e930bfbdc563c00740466603f8723158e90297d07957c2294640262b /uv /usr/local/bin/uv
WORKDIR /app

COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev --extra http 2>/dev/null || uv sync --no-dev --extra http

# Runtime stage
FROM public.ecr.aws/docker/library/python:3.11-slim

RUN groupadd -r appuser && useradd -r -g appuser appuser

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY src/ ./src/

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app/src"

USER appuser

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import redis, os; r=redis.Redis.from_url(os.environ['REDIS_URL']); r.ping()" || exit 1

CMD ["python", "-m", "worker.main"]
