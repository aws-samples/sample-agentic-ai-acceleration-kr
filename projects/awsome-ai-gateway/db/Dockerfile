# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

FROM public.ecr.aws/docker/library/python:3.11-slim

# 빌드 도구도 고정한다 — :latest 는 움직이는 태그라 같은 커밋이라도 빌드마다 다른 uv 가 들어온다.
# 올릴 때는 태그와 digest 를 함께 바꾼다(docker buildx imagetools inspect ghcr.io/astral-sh/uv:<버전>).
COPY --from=ghcr.io/astral-sh/uv:0.13.4@sha256:9f130421e930bfbdc563c00740466603f8723158e90297d07957c2294640262b /uv /usr/local/bin/uv
WORKDIR /app

# psql-client 필요 — 클라우드 모드에서 run_migration.sh 가 init/*.sql 을 직접 실행할 때 사용.
# 로컬 docker-compose 에서는 init SQL 을 postgres 컨테이너가 실행하므로 이 psql 은 안 쓰이지만,
# 이미지를 한 가지로 유지하기 위해 설치.
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Optional: copy custom CA certs if present (for corporate proxy)
COPY ca-certificates.cr[t] /etc/ssl/certs/ca-certificates.crt
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

# 의존성은 requirements.lock 에 고정된 버전·해시로만 설치한다(직접 의존성과 갱신 방법은 requirements.in).
# 이 Job 은 DB master 자격증명을 받으므로, 빌드 시점의 최신 PyPI 릴리스를 그대로 받지 않는다.
# env.py 가 create_async_engine 을 쓰므로 greenlet 이 필요하다. SQLAlchemy 2.1 부터 greenlet 은
# [asyncio] extra 로만 설치된다(2.0.x 는 x86_64 에서 자동 설치됐다) — lock 에 greenlet 이 들어 있어야 한다.
# --no-build: wheel 만 설치한다(sdist 빌드 의존성은 해시 검사 없이 받아 실행된다).
COPY requirements.lock ./
RUN uv pip install --system --no-cache --require-hashes --no-build -r requirements.lock

COPY alembic.ini env.py ./
COPY versions/ ./versions/
# init/*.sql 을 이미지에 포함 — 클라우드 모드에서 run_migration.sh 가 직접 실행.
# 로컬 docker-compose 는 별도로 ./db/init 을 postgres 컨테이너 볼륨에 마운트.
COPY init/ ./init/
COPY run_migration.sh ./
RUN chmod +x run_migration.sh

# Run as a non-root user.
# EN: Even a one-shot migration Job benefits from least-privilege. Running as
#     a dedicated UID also lets the cluster enforce PodSecurity `restricted`
#     (`runAsNonRoot: true`) without an explicit `securityContext` override.
#     `alembic` was installed system-wide above, so it is on PATH for any
#     UID; the chown ensures the migration binary is executable by `appuser`.
# KO: 일회성 migration Job 이라도 최소 권한 원칙을 따릅니다. 전용 UID 로
#     실행하면 클러스터의 PodSecurity `restricted` (`runAsNonRoot: true`)
#     정책을 별도 `securityContext` override 없이 만족합니다. `alembic` 은
#     위에서 system-wide 로 설치되어 어떤 UID 에서도 PATH 에 있으며,
#     chown 으로 `appuser` 가 마이그레이션 스크립트를 실행할 수 있도록 합니다.
RUN groupadd --system --gid 10001 appgroup \
    && useradd  --system --uid 10001 --gid appgroup --create-home --home-dir /home/appuser appuser \
    && chown -R appuser:appgroup /app
USER 10001:10001

CMD ["./run_migration.sh"]
