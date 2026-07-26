# syntax=docker/dockerfile:1
# AgentCore Runtime 컨테이너 규격: ARM64, Host 0.0.0.0, 포트 8080.
# multi-stage: uv 로 의존성 설치(--frozen --no-dev) 후 slim 런타임에 복사, non-root 실행.

FROM public.ecr.aws/docker/library/python:3.12-slim AS builder

# uv 바이너리 복사 (공식 이미지에서).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# 의존성 레이어 캐시: 락파일/매니페스트 먼저 복사 후 설치.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# 소스 복사 후 프로젝트 자체 설치.
# README.md 는 pyproject.toml 의 `readme` 필드가 참조하므로 프로젝트 설치 전 반드시 복사.
COPY src ./src
COPY README.md ./README.md
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


FROM public.ecr.aws/docker/library/python:3.12-slim AS runtime

# non-root 사용자.
RUN groupadd --system app && useradd --system --gid app --home /app app

WORKDIR /app

# 가상환경 + 소스만 복사.
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/src /app/src

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    AWS_REGION=us-west-2

USER app

# AgentCore Runtime 규격: 0.0.0.0:8080, POST /invocations(SSE), GET /ping.
EXPOSE 8080

CMD ["python", "-m", "orchestrator.app"]
