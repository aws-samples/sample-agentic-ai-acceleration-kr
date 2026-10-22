# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# ---- Builder Stage ----
FROM public.ecr.aws/docker/library/python:3.11-slim AS builder

WORKDIR /build

# 빌드 도구도 고정한다 — :latest 는 움직이는 태그라 같은 커밋이라도 빌드마다 다른 uv 가 들어온다.
# 올릴 때는 태그와 digest 를 함께 바꾼다(docker buildx imagetools inspect ghcr.io/astral-sh/uv:<버전>).
COPY --from=ghcr.io/astral-sh/uv:0.13.4@sha256:9f130421e930bfbdc563c00740466603f8723158e90297d07957c2294640262b /uv /usr/local/bin/uv

COPY ca-certificates.cr[t] /etc/ssl/certs/ca-certificates.crt
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

# 의존성은 uv.lock 에 고정된 버전·해시로만 설치한다. pyproject.toml 을 그대로 풀면(하한만 있다)
# 빌드 시점의 최신 PyPI 릴리스가 들어와 CI 가 테스트한 버전과 달라지고, 갓 올라온 악성 릴리스도
# 그대로 들어온다. --locked: lock 이 pyproject 와 어긋나면 빌드를 멈춘다. --require-hashes: 해시가
# 다르면 설치를 거부한다. dev extra(pytest 등)는 export 에 넣지 않는다.
COPY pyproject.toml uv.lock ./

# cache/bytecode/tests 제거 → 이미지 슬림화
RUN uv export --locked --no-dev --no-emit-project --format requirements-txt -o /tmp/requirements.txt \
    && uv pip install --system --no-cache --require-hashes -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt \
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
