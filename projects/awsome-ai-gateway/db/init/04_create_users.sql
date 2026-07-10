-- Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

-- 04_create_users.sql
-- LLM Gateway — Database users and schema-level privileges
-- Based on shared-infrastructure.md Section 2.2 principle: least privilege

-- ============================================================
-- Database users (passwords supplied via environment variables)
-- ============================================================
--
-- [SECURITY / 보안 경고]
-- EN: The literal passwords below (e.g. 'proxy_password_change_me') are
--     placeholders intended for local development / docker-compose only.
--     For ANY non-local environment you MUST override these via the
--     POSTGRES_* / *_PASSWORD environment variables wired into your
--     container orchestrator (K8s Secret, ExternalSecret, Vault, ...).
--     Deploying these defaults to a shared environment is a security
--     incident. See SECURITY_REVIEW.md (category A).
-- KO: 아래 리터럴 비밀번호('proxy_password_change_me' 등)는 로컬 개발 /
--     docker-compose 전용 플레이스홀더입니다. 로컬이 아닌 모든 환경에서는
--     반드시 컨테이너 오케스트레이터(K8s Secret, ExternalSecret, Vault 등)에
--     연결된 POSTGRES_* / *_PASSWORD 환경변수로 override 해야 합니다.
--     이 기본값을 공유 환경에 배포하면 보안 사고에 해당합니다.
--     자세한 내용은 SECURITY_REVIEW.md (카테고리 A) 참조.
-- ============================================================


-- proxy_user: Used by U1 Gateway Proxy
-- Reads: auth, model (config lookup), budget (usage check)
-- Writes: usage (usage_logs), budget (budget_usages atomic update)
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'proxy_user') THEN
        CREATE ROLE proxy_user WITH LOGIN PASSWORD 'proxy_password_change_me';
    END IF;
END
$$;

-- admin_api_user: Used by U2 Admin API + Scheduler
-- Full CRUD on auth, budget, model, audit schemas
-- Read on usage schema (analytics queries)
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'admin_api_user') THEN
        CREATE ROLE admin_api_user WITH LOGIN PASSWORD 'admin_api_password_change_me';
    END IF;
END
$$;

-- ============================================================
-- proxy_user privileges
-- ============================================================

-- auth: SELECT only (user/key lookup)
GRANT USAGE ON SCHEMA auth TO proxy_user;
GRANT SELECT ON ALL TABLES IN SCHEMA auth TO proxy_user;
-- virtual_keys.last_used_at update
GRANT UPDATE (last_used_at) ON auth.virtual_keys TO proxy_user;

-- model: SELECT only (model config, pricing, rate limits)
GRANT USAGE ON SCHEMA model TO proxy_user;
GRANT SELECT ON ALL TABLES IN SCHEMA model TO proxy_user;

-- budget: SELECT + UPDATE on budget_usages (atomic budget deduction)
GRANT USAGE ON SCHEMA budget TO proxy_user;
GRANT SELECT ON ALL TABLES IN SCHEMA budget TO proxy_user;
GRANT UPDATE ON budget.budget_usages TO proxy_user;
GRANT INSERT ON budget.budget_usages TO proxy_user;

-- usage: INSERT on usage_logs (write usage records)
GRANT USAGE ON SCHEMA usage TO proxy_user;
GRANT INSERT ON usage.usage_logs TO proxy_user;
GRANT SELECT ON usage.usage_logs TO proxy_user;

-- ============================================================
-- admin_api_user privileges
-- ============================================================

-- auth: full CRUD
GRANT USAGE ON SCHEMA auth TO admin_api_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA auth TO admin_api_user;

-- budget: full CRUD
GRANT USAGE ON SCHEMA budget TO admin_api_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA budget TO admin_api_user;

-- model: full CRUD
GRANT USAGE ON SCHEMA model TO admin_api_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA model TO admin_api_user;

-- usage: SELECT (analytics) + INSERT/UPDATE on roi_aggregations (scheduler)
GRANT USAGE ON SCHEMA usage TO admin_api_user;
GRANT SELECT ON ALL TABLES IN SCHEMA usage TO admin_api_user;
GRANT INSERT, UPDATE ON usage.roi_aggregations TO admin_api_user;

-- audit: full CRUD (audit logs + cache invalidation failures)
GRANT USAGE ON SCHEMA audit TO admin_api_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA audit TO admin_api_user;

-- notification_worker_user: Used by U3 Notification Worker
-- Reads: auth (recipient lookup), notification (config/log)
-- Writes: notification (delivery log)
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'notification_worker_user') THEN
        CREATE ROLE notification_worker_user WITH LOGIN PASSWORD 'notification_worker_password_change_me';
    END IF;
END
$$;

-- ============================================================
-- notification_worker_user privileges (auth only)
-- notification schema grants are in 07_grant_notification_privileges.sql
-- (must run after 05_create_notification_schema.sql creates the tables)
-- ============================================================

-- auth: SELECT only (users, teams — recipient resolution)
GRANT USAGE ON SCHEMA auth TO notification_worker_user;
GRANT SELECT ON auth.users, auth.teams TO notification_worker_user;

-- ============================================================
-- Default privileges for future tables
-- ============================================================

ALTER DEFAULT PRIVILEGES IN SCHEMA auth  GRANT SELECT ON TABLES TO proxy_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA model GRANT SELECT ON TABLES TO proxy_user;

ALTER DEFAULT PRIVILEGES IN SCHEMA auth   GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO admin_api_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA budget GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO admin_api_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA model  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO admin_api_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA audit  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO admin_api_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA usage  GRANT SELECT ON TABLES TO admin_api_user;
