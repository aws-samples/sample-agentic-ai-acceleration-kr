#!/bin/sh
# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# LLM Gateway — Migration runner
#
# 두 가지 모드로 동작:
#
# [로컬 docker-compose]
#   - postgres 컨테이너가 자체적으로 /docker-entrypoint-initdb.d 의 *.sql 을 자동 실행.
#   - 이 스크립트는 DB_MASTER_URL 이 없는 상태로 호출됨 → init SQL 건너뛰고 alembic upgrade 만 수행.
#
# [클라우드 (EKS + Aurora 등)]
#   - Aurora 는 docker-entrypoint-initdb.d 메커니즘 없음 → 이 스크립트가 직접 수행.
#   - DB_MASTER_URL (master user 권한) 필수.
#   - APP_DB_USER/APP_DB_PASSWORD 제공 시 application user 생성/업데이트 + 모든 schema 에 GRANT.
#   - 순서: init/*.sql 실행 → application user 처리 → alembic upgrade head.
#   - 모든 SQL 은 idempotent (IF NOT EXISTS / ON CONFLICT / DO IF NOT EXISTS) — 재실행 안전.
#
# NOTE: 0001_baseline 은 no-op (DDL 은 init SQL 이 수행). 0002 부터 실제 DML/DDL 포함.
#       alembic upgrade head 는 이미 head 인 경우 no-op 이므로 재실행 안전.
set -e

MASTER_URL="${DB_MASTER_URL:-}"
APP_USER="${APP_DB_USER:-}"
APP_PASSWORD="${APP_DB_PASSWORD:-}"
SCHEMAS="auth model budget usage audit notification public"

# psql용: password에 특수문자가 있으면 URL 파싱이 깨지므로 PGPASSWORD 환경변수 사용.
# DB_MASTER_PASSWORD는 K8s Secret에서 직접 주입됨 (Helm template env).
# URL에서 password 부분을 제거하고 PGPASSWORD로 분리.
if [ -n "${DB_MASTER_PASSWORD:-}" ]; then
    export PGPASSWORD="$DB_MASTER_PASSWORD"
fi
# password 없는 connection string (user@host:port/db?sslmode=require)
MASTER_URL_NO_PASS=$(echo "$MASTER_URL" | sed 's|://[^:]*:[^@]*@|://'"${DB_MASTER_USER:-postgres_admin}"'@|')

if [ -n "$MASTER_URL" ]; then
    echo "[migration] cloud mode — DB_MASTER_URL detected, applying init SQL"
    for f in /app/init/01_*.sql /app/init/02_*.sql /app/init/03_*.sql /app/init/04_*.sql /app/init/05_*.sql /app/init/06_*.sql /app/init/07_*.sql; do
        [ -f "$f" ] || continue
        echo "[migration]   applying $(basename "$f")"
        psql "$MASTER_URL_NO_PASS" -v ON_ERROR_STOP=1 -f "$f"
    done

    if [ -n "$APP_USER" ] && [ -n "$APP_PASSWORD" ]; then
        echo "[migration] creating/updating application user '$APP_USER'"
        psql "$MASTER_URL_NO_PASS" -v ON_ERROR_STOP=1 <<EOSQL
DO \$\$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${APP_USER}') THEN
        CREATE ROLE "${APP_USER}" WITH LOGIN PASSWORD '${APP_PASSWORD}';
    ELSE
        ALTER ROLE "${APP_USER}" WITH LOGIN PASSWORD '${APP_PASSWORD}';
    END IF;
END \$\$;
EOSQL

        echo "[migration] granting privileges on all schemas to '$APP_USER'"
        for schema in $SCHEMAS; do
            psql "$MASTER_URL_NO_PASS" -v ON_ERROR_STOP=1 -c "GRANT USAGE, CREATE ON SCHEMA \"$schema\" TO \"$APP_USER\";"
            psql "$MASTER_URL_NO_PASS" -v ON_ERROR_STOP=1 -c "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA \"$schema\" TO \"$APP_USER\";"
            psql "$MASTER_URL_NO_PASS" -v ON_ERROR_STOP=1 -c "GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA \"$schema\" TO \"$APP_USER\";"
            psql "$MASTER_URL_NO_PASS" -v ON_ERROR_STOP=1 -c "ALTER DEFAULT PRIVILEGES IN SCHEMA \"$schema\" GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO \"$APP_USER\";"
            psql "$MASTER_URL_NO_PASS" -v ON_ERROR_STOP=1 -c "ALTER DEFAULT PRIVILEGES IN SCHEMA \"$schema\" GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO \"$APP_USER\";"
        done
        echo "[migration]   privileges granted on schemas: $SCHEMAS"
    else
        echo "[migration] WARNING: APP_DB_USER/APP_DB_PASSWORD 없음 — application user 처리 건너뜀"
    fi
else
    echo "[migration] local mode — DB_MASTER_URL 없음, init SQL 은 postgres 컨테이너가 실행했을 것"
fi

echo "[migration] upgrading to head"
alembic upgrade head
echo "[migration] done."