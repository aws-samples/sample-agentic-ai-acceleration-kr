# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# Build stage
FROM public.ecr.aws/docker/library/python:3.11-slim AS builder

# 빌드 도구도 고정한다 — :latest 는 움직이는 태그라 같은 커밋이라도 빌드마다 다른 uv 가 들어온다.
# 올릴 때는 태그와 digest 를 함께 바꾼다(docker buildx imagetools inspect ghcr.io/astral-sh/uv:<버전>).
COPY --from=ghcr.io/astral-sh/uv:0.13.4@sha256:9f130421e930bfbdc563c00740466603f8723158e90297d07957c2294640262b /uv /usr/local/bin/uv
WORKDIR /app

COPY ca-certificates.cr[t] /etc/ssl/certs/ca-certificates.crt
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev 2>/dev/null || uv sync --no-dev

# Runtime stage
FROM public.ecr.aws/docker/library/python:3.11-slim

RUN groupadd -g 1000 appuser && useradd -u 1000 -g appuser appuser

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY src/ ./src/
COPY ca-certificates.cr[t] /etc/ssl/certs/ca-certificates.crt

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app/src"
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

USER appuser

EXPOSE 8000

# ⚠️ 워커 수는 WORKERS env 로 제어(chart values 에서 환경별 조정). exec-form
# CMD ["...","--workers","4"] 는 env 확장이 안 돼 chart 의 WORKERS 설정이 죽은
# 설정이 됐었음 → shell-form + ${WORKERS:-4} 로 살림(미설정 시 기본 4 = 무회귀).
# Fargate 0.5 vCPU 에서 4워커 = 워커당 0.125 vCPU CPU 기아 → values 에서 1~2 권장.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers ${WORKERS:-4}"]
