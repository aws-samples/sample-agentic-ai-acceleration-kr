-- Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

-- 08_create_chat_reader.sql
-- LLM Gateway — gateway_chat_reader read-only role for admin-chat-agent (Phase 2)
--
-- query_db Lambda 가 사용하는 read-only DB user. SELECT 만 가능, schema
-- whitelist 에 따른 컬럼만 노출. password_hash / virtual_keys.value 같은
-- 민감 컬럼은 명시적으로 제외.
--
-- 비밀번호: ESO 가 Secrets Manager 에서 동기화. 실제 password 는 init 시
-- placeholder 로 두고 Terraform 또는 운영자가 ALTER ROLE 로 갱신.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gateway_chat_reader') THEN
        CREATE ROLE gateway_chat_reader LOGIN PASSWORD 'placeholder_change_me' NOINHERIT;
    END IF;
END
$$;

-- DB connect
GRANT CONNECT ON DATABASE gateway TO gateway_chat_reader;

-- Schema USAGE — SELECT 가능하기 위한 prerequisite
GRANT USAGE ON SCHEMA auth, public, model, budget, usage, chat_agent TO gateway_chat_reader;

-- 명시적 SELECT — 화이트리스트에 있는 테이블만. 컬럼명은 ground truth
-- (db/init/02_create_tables.sql) 기준. 새 테이블 생기면 여기에 추가 +
-- spec §2.5.3 의 schema_whitelist.yaml 도 갱신.

-- auth schema (last_login_at 컬럼 없음 → updated_at. dept_id (NOT department_id).
-- virtual_keys 는 status (NOT is_active/ttl_seconds), value(key_value_encrypted) 미부여.)
-- ⚠️ GRANT 컬럼 목록은 schema_whitelist.yaml 과 정확히 일치해야 함 — whitelist 에
--    있는데 GRANT 누락이면 sqlglot 통과 후 DB permission denied 로 query_db 실패.
GRANT SELECT (id, team_id, email, display_name, role, provider, is_active, created_at, updated_at)
    ON auth.users TO gateway_chat_reader;
GRANT SELECT (id, dept_id, name, leader_user_id, created_at, updated_at)
    ON auth.teams TO gateway_chat_reader;
-- virtual_keys: key_value_encrypted (키 원문) 절대 미부여 (보안 핵심)
GRANT SELECT (id, user_id, key_prefix, status, issued_at, expires_at,
              last_used_at, revoked_at, revoked_by, created_at)
    ON auth.virtual_keys TO gateway_chat_reader;

-- 운영 데이터 — 실제 스키마는 usage/budget/model (NOT public).
-- 테이블/컬럼은 db/init/02_create_tables.sql 과 schema_whitelist.yaml 에 일치.
GRANT SELECT (id, request_id, user_id, team_id, dept_id, model_alias, provider,
              input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
              cost_usd, latency_ms, status, requested_at, completed_at,
              is_streaming, estimated_usage, downgraded_from)
    ON usage.usage_logs TO gateway_chat_reader;
GRANT SELECT (id, scope, scope_id, max_budget_usd, period_type, policy,
              allocated_by, effective_from, is_active, created_at)
    ON budget.budget_configs TO gateway_chat_reader;
GRANT SELECT (id, scope, scope_id, model_alias, rpm_limit, tpm_limit,
              cpm_limit_usd, cph_limit_usd, is_active, created_by, created_at, updated_at)
    ON model.rate_limit_configs TO gateway_chat_reader;

-- chat_agent schema (자기 작성 history 조회 가능)
-- ⚠️ 이 테이블들은 마이그레이션(chat_agent 스키마 생성) 이후에야 존재한다. clean 볼륨
--    첫 부팅에서는 아직 없을 수 있으므로, 테이블 존재 시에만 GRANT 한다. guard 없이
--    무조건 GRANT 하면 첫 부팅 init 이 여기서 실패(exit)해 컨테이너가 죽는다.
--    (재부팅/마이그레이션 이후 재실행 시 정상 부여됨.)
DO $$
BEGIN
    IF to_regclass('chat_agent.sessions') IS NOT NULL THEN
        GRANT SELECT ON chat_agent.sessions, chat_agent.messages TO gateway_chat_reader;
    END IF;
    IF to_regclass('chat_agent.schema_embeddings') IS NOT NULL THEN
        GRANT SELECT ON chat_agent.schema_embeddings, chat_agent.golden_examples TO gateway_chat_reader;
    END IF;
END $$;

-- audit schema 는 super-admin 만. gateway_chat_reader 는 미부여.

-- statement_timeout 강제 — 무한 query 방지 (spec §3.1.6)
ALTER ROLE gateway_chat_reader SET statement_timeout = '10s';

-- 새 테이블 생성 시 자동 SELECT 부여 안 함 (default privileges 변경 안 함).
-- 새 테이블이 생기면 명시적으로 GRANT 후 schema_whitelist.yaml 도 갱신.
