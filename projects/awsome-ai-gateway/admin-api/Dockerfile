# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ---- Builder Stage ----
FROM public.ecr.aws/docker/library/python:3.11-slim AS builder

WORKDIR /build

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY ca-certificates.cr[t] /etc/ssl/certs/ca-certificates.crt
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

# uv.lock* 가 있으면 사용, 없어도 동작 (glob)
COPY pyproject.toml uv.lock* ./

# dev 의존성 제외 + cache/bytecode/tests 제거 → 이미지 슬림화
RUN uv pip install --system --no-cache -r pyproject.toml \
    && find /usr/local/lib/python3.11/site-packages -type d -name '__pycache__' -exec rm -rf {} + \
    && find /usr/local/lib/python3.11/site-packages -type f -name '*.pyc' -delete \
    && find /usr/local/lib/python3.11/site-packages -type d -name 'tests' -exec rm -rf {} + \
    && find /usr/local/lib/python3.11/site-packages -type d -name 'test' -exec rm -rf {} +

COPY src/ src/

# ---- Runtime Stage ----
FROM public.ecr.aws/docker/library/python:3.11-slim AS runtime

RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /sbin/nologin appuser

WORKDIR /app

COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /build/src /app/src
COPY ca-certificates.cr[t] /etc/ssl/certs/ca-certificates.crt

RUN chown -R appuser:appuser /app
USER appuser

ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

EXPOSE 8080

# workers 1 (§56): in-memory _StreamRelay(핸드오프 재구독)가 프로세스 로컬이라
# 멀티워커면 POST(워커A)/재구독 GET(워커B)이 갈라져 404. 단일 워커로 일관성 보장.
# (멀티 replica/워커 확장 시 relay 를 Redis pub/sub 로 교체 후 되돌릴 것.)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
