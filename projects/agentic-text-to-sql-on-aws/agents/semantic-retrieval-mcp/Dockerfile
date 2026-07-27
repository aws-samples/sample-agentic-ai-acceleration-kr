# semantic-retrieval-mcp — AgentCore Runtime MCP 호스팅용 컨테이너 (ARM64)
# multi-stage: uv로 의존성 설치(빌드) → slim 런타임에 복사.
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.11-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY README.md ./README.md
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.11-slim AS runtime

RUN useradd --create-home --uid 1000 appuser

WORKDIR /app

COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv
COPY --from=builder --chown=appuser:appuser /app/src /app/src

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

USER appuser

# AgentCore Runtime MCP 규격: 0.0.0.0:8000/mcp, stateless streamable-HTTP.
EXPOSE 8000

CMD ["python", "-m", "semantic_retrieval_mcp.server"]
