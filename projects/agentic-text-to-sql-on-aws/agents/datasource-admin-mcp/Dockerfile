# datasource-admin-mcp — AgentCore Runtime MCP 호스팅용 컨테이너 (ARM64)
#
# ⚠️ 빌드 컨텍스트는 **레포 루트**다 (semantic-layer 로컬 경로 의존성 때문).
#   scripts/build-and-push.sh datasource-admin-mcp
#   (수동: docker build --platform linux/arm64 -f agents/datasource-admin-mcp/Dockerfile .)
#
# multi-stage: uv로 의존성 설치(빌드) → slim 런타임에 복사.
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.11-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# 경로 의존성(semantic-layer)의 상대 경로(../../semantic-layer)를 보존하려면
# admin-mcp 는 /app/agents/datasource-admin-mcp 에, semantic-layer 는 /app/semantic-layer 에
# 놓여야 한다(레포 레이아웃 그대로).
WORKDIR /app/agents/datasource-admin-mcp

# 의존성 레이어 캐싱: 락파일·매니페스트 먼저 복사 후 설치.
# semantic-layer 는 경로 의존성이라 매니페스트+소스가 함께 필요하다(빌드 백엔드가 wheel 생성).
COPY semantic-layer /app/semantic-layer
COPY agents/datasource-admin-mcp/pyproject.toml agents/datasource-admin-mcp/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# 소스 복사 후 프로젝트 설치(README는 pyproject의 readme 필드가 참조).
COPY agents/datasource-admin-mcp/src ./src
COPY agents/datasource-admin-mcp/README.md ./README.md
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.11-slim AS runtime

RUN useradd --create-home --uid 1000 appuser

# venv 내부 절대 경로(pyvenv.cfg·editable .pth)가 유효하도록 builder 와 **동일한 경로**를
# 유지한다(venv 재배치 금지). 다른 MCP 서버는 /app 하나뿐이라 문제가 없었지만, 여기서는
# 경로 의존성 때문에 builder 가 /app/agents/datasource-admin-mcp 를 쓴다.
WORKDIR /app/agents/datasource-admin-mcp

COPY --from=builder --chown=appuser:appuser \
    /app/agents/datasource-admin-mcp/.venv ./.venv
COPY --from=builder --chown=appuser:appuser \
    /app/agents/datasource-admin-mcp/src ./src

ENV PATH="/app/agents/datasource-admin-mcp/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/agents/datasource-admin-mcp/src

USER appuser

# AgentCore Runtime MCP 규격: 0.0.0.0:8000/mcp, stateless streamable-HTTP.
EXPOSE 8000

CMD ["python", "-m", "datasource_admin_mcp.server"]
