-- Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

-- 03_seed_data.sql
-- LLM Gateway — Initial seed data
-- Run after table creation
--
-- Idempotent: 모든 INSERT 가 `ON CONFLICT DO NOTHING` 또는 `WHERE NOT EXISTS` 로 보호됨.
-- 재실행 안전 (이미 seed 된 데이터는 건너뜀). 사용자가 admin UI 에서 수정한 값도 보존.

-- ============================================================
-- Default Organization / Department / Team
-- ============================================================

INSERT INTO auth.organizations (id, name) VALUES
    ('00000000-0000-4000-a000-000000000001', 'Default Organization')
ON CONFLICT (id) DO NOTHING;

INSERT INTO auth.departments (id, org_id, name) VALUES
    ('00000000-0000-4000-a000-000000000002', '00000000-0000-4000-a000-000000000001', 'Default Department')
ON CONFLICT (id) DO NOTHING;

INSERT INTO auth.teams (id, dept_id, name) VALUES
    ('00000000-0000-4000-a000-000000000003', '00000000-0000-4000-a000-000000000002', 'Default Team')
ON CONFLICT (id) DO NOTHING;

-- NOTE: DEFAULT_TEAM_ID env var should be set to '00000000-0000-4000-a000-000000000003'

-- ============================================================
-- System Admin User (bootstrap — update email/sso_subject after first login)
-- ============================================================

INSERT INTO auth.users (id, team_id, email, display_name, role, sso_subject) VALUES
    ('00000000-0000-4000-a000-000000000010',
     '00000000-0000-4000-a000-000000000003',
     'admin@example.com',
     'System Admin',
     'ADMIN',
     'bootstrap-admin-placeholder')
ON CONFLICT (id) DO NOTHING;

-- ============================================================
-- Default Rotation Policy (GLOBAL, 90 days)
-- ============================================================

INSERT INTO auth.rotation_policies (id, scope, scope_id, expiry_days, auto_renew, notify_days_before, is_active, created_by) VALUES
    ('00000000-0000-4000-a000-000000000020',
     'GLOBAL', NULL, 90, true, '{7,1}', true,
     '00000000-0000-4000-a000-000000000010')
ON CONFLICT (id) DO NOTHING;

-- ============================================================
-- Admin JWT Config (placeholder — replace public_key_pem with actual key)
-- ============================================================

INSERT INTO auth.admin_jwt_configs (id, issuer, audience, public_key_pem, algorithm, is_active) VALUES
    ('00000000-0000-4000-a000-000000000030',
     'ds-gateway-admin',
     'ds-gateway-admin-api',
     '-----BEGIN PUBLIC KEY-----
REPLACE_WITH_ACTUAL_RS256_PUBLIC_KEY
-----END PUBLIC KEY-----',
     'RS256',
     true)
ON CONFLICT (id) DO NOTHING;

-- ============================================================
-- Default Bedrock Models (Global, ap-northeast-2 region)
-- ============================================================

-- Claude Code 가 실제로 `/v1/messages` 에 보내는 model 이름 매핑:
--   Haiku 4.5            → "claude-haiku-4-5-20251001"
--   Sonnet 4.6 (default) → "claude-sonnet-4-6"
--   Sonnet 4.6 (1M ctx)  → "claude-sonnet-4-6[1m]"
--   Opus 4.6  (1M ctx)   → "global.anthropic.claude-opus-4-6-v1"  ← full model ID 그대로 전송
--   Opus 4.7  (1M ctx)   → "claude-opus-4-7"                      ← alias 로 전송
-- provider_model_id 는 Bedrock global inference profile 로 통일.
-- ⚠️ display_name 동기화: 아래 inline display_name 은 migration 0012 의 _BACKFILL dict 와
--    동일해야 한다(fresh-init DB vs migrated DB 수렴). cowork-opus(BEDROCK_MANTLE)는 여기 없고
--    migration 0009 가 생성 + 0012 가 라벨 백필한다. 라벨 수정 시 두 곳 모두 갱신할 것.
-- ⚠️ BEDROCK_MANTLE alias 는 init SQL 에 없고 라이브-시드 전용(cowork-opus 와 동일 기조):
--    'claude-opus-4-8-mantle'(Claude Code · Opus 4.8 (Mantle), 374 in-account, Tokyo) +
--    model.routing_profiles 의 'claude-code'(backend=mantle, account_role_arn=NULL=in-account,
--    region=ap-northeast-1, default_model=NULL) 도 라이브에서 직접 INSERT (2026-06-24).
INSERT INTO model.model_aliases (alias, provider, provider_model_id, endpoint_url, api_format, status, description, display_name, created_by) VALUES
    ('claude-haiku-4-5-20251001', 'BEDROCK', 'global.anthropic.claude-haiku-4-5-20251001-v1:0', NULL, 'BEDROCK_NATIVE', 'ACTIVE',
     'Claude Haiku 4.5 (Global)',
     'Claude Code · Haiku 4.5',
     '00000000-0000-4000-a000-000000000010'),
    ('claude-sonnet-4-6', 'BEDROCK', 'global.anthropic.claude-sonnet-4-6', NULL, 'BEDROCK_NATIVE', 'ACTIVE',
     'Claude Sonnet 4.6 default (Global)',
     'Claude Code · Sonnet 4.6',
     '00000000-0000-4000-a000-000000000010'),
    ('claude-sonnet-4-6[1m]', 'BEDROCK', 'global.anthropic.claude-sonnet-4-6', NULL, 'BEDROCK_NATIVE', 'ACTIVE',
     'Claude Sonnet 4.6 1M context (Global)',
     'Claude Code · Sonnet 4.6 (1M)',
     '00000000-0000-4000-a000-000000000010'),
    ('global.anthropic.claude-opus-4-6-v1', 'BEDROCK', 'global.anthropic.claude-opus-4-6-v1', NULL, 'BEDROCK_NATIVE', 'ACTIVE',
     'Claude Opus 4.6 1M context (Global)',
     'Claude Code · Opus 4.6',
     '00000000-0000-4000-a000-000000000010'),
    ('claude-opus-4-7', 'BEDROCK', 'global.anthropic.claude-opus-4-7', NULL, 'BEDROCK_NATIVE', 'ACTIVE',
     'Claude Opus 4.7 1M context (Global)',
     'Claude Code · Opus 4.7',
     '00000000-0000-4000-a000-000000000010'),
    ('claude-opus-4-8', 'BEDROCK', 'global.anthropic.claude-opus-4-8', NULL, 'BEDROCK_NATIVE', 'ACTIVE',
     'Claude Opus 4.8 1M context (Global)',
     'Claude Code · Opus 4.8',
     '00000000-0000-4000-a000-000000000010'),
    ('global.anthropic.claude-opus-4-8', 'BEDROCK', 'global.anthropic.claude-opus-4-8', NULL, 'BEDROCK_NATIVE', 'ACTIVE',
     'Claude Opus 4.8 1M context (Global, full ID)',
     'Claude Code · Opus 4.8',
     '00000000-0000-4000-a000-000000000010')
ON CONFLICT (alias) DO NOTHING;

-- Cache 단가는 Bedrock Prompt Caching 공시값:
--   5-min cache write = input × 1.25,  1-hour cache write = input × 2.0,  cache read = input × 0.1
-- Opus 4.7 단가: $5/$25 per 1M (2026-04-16 출시, Opus 4.6과 동일 가격).
-- PK 는 gen_random_uuid() 라 매번 새 → WHERE NOT EXISTS 로 중복 방지
-- (model_alias, effective_from) 조합이 동일한 row 가 없을 때만 INSERT.
-- Pricing seed는 migration 0003에서 처리 (DROP + 재생성 + INSERT).
-- 새 DB (clean-slate)에서도 02_create_tables.sql이 테이블을 만들고,
-- 0003이 DROP + recreate하면서 seed를 넣으므로 여기서는 skip.
-- (init SQL은 alembic보다 먼저 실행되어 컬럼명 충돌 방지)

-- ============================================================
-- Default Budget (Admin user + Default Team)
-- ============================================================
-- PK 는 gen_random_uuid() 라 WHERE NOT EXISTS 로 (scope, scope_id) 중복 방지.

INSERT INTO budget.budget_configs (id, scope, scope_id, max_budget_usd, policy, allocated_by, effective_from)
SELECT gen_random_uuid(), v.scope::budget.budget_scope, v.scope_id::uuid,
    v.max_budget, v.policy::budget.budget_policy,
    '00000000-0000-4000-a000-000000000010', CURRENT_DATE
FROM (VALUES
    ('TEAM', '00000000-0000-4000-a000-000000000003', 5000.0000, 'HARD_BLOCK'),
    ('USER', '00000000-0000-4000-a000-000000000010', 1000.0000, 'HARD_BLOCK')
) AS v(scope, scope_id, max_budget, policy)
WHERE NOT EXISTS (
    SELECT 1 FROM budget.budget_configs b
    WHERE b.scope = v.scope::budget.budget_scope
      AND b.scope_id = v.scope_id::uuid
      AND b.is_active = true
);
