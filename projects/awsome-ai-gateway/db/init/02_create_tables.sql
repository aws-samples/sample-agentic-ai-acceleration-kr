-- Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

-- 02_create_tables.sql
-- LLM Gateway — Table creation (all schemas)
-- Shared by: U1 Gateway Proxy, U2 Admin API
--
-- Idempotent: 매 migration Job 재실행에도 안전.
--   - CREATE TYPE: PostgreSQL 이 IF NOT EXISTS 미지원 → DO block + EXCEPTION
--   - CREATE TABLE / INDEX / UNIQUE INDEX: IF NOT EXISTS
--   - ALTER TABLE ADD CONSTRAINT: DO block + EXCEPTION

-- ============================================================
-- auth schema — Enums
-- ============================================================

DO $$ BEGIN CREATE TYPE auth.user_role AS ENUM ('ADMIN', 'TEAM_LEADER', 'DEVELOPER'); EXCEPTION WHEN duplicate_object THEN null; END $$;
DO $$ BEGIN CREATE TYPE auth.key_status AS ENUM ('ACTIVE', 'EXPIRED', 'REVOKED'); EXCEPTION WHEN duplicate_object THEN null; END $$;
DO $$ BEGIN CREATE TYPE auth.rotation_scope AS ENUM ('GLOBAL', 'TEAM', 'USER'); EXCEPTION WHEN duplicate_object THEN null; END $$;

-- ============================================================
-- auth schema — Tables
-- ============================================================

CREATE TABLE IF NOT EXISTS auth.organizations (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        VARCHAR(255) NOT NULL,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS auth.departments (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      UUID         NOT NULL REFERENCES auth.organizations(id),
    name        VARCHAR(255) NOT NULL,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS auth.teams (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dept_id         UUID         NOT NULL REFERENCES auth.departments(id),
    name            VARCHAR(255) NOT NULL,
    leader_user_id  UUID,  -- FK added after auth.users is created
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS auth.users (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    team_id       UUID         REFERENCES auth.teams(id),
    email         VARCHAR(320) NOT NULL UNIQUE,
    display_name  VARCHAR(255) NOT NULL,
    role          auth.user_role NOT NULL DEFAULT 'DEVELOPER',
    sso_subject   VARCHAR(512) NOT NULL UNIQUE,
    -- Auth origin: 'sts' (legacy) 또는 'oidc:<idp>' (예: 'oidc:cognito').
    -- 다중 IDP 동시 운영 시 식별자.
    provider      VARCHAR(64)  NOT NULL DEFAULT 'sts',
    is_active     BOOLEAN      NOT NULL DEFAULT true,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- Deferred FK: teams.leader_user_id -> users.id
DO $$ BEGIN
    ALTER TABLE auth.teams
        ADD CONSTRAINT fk_teams_leader_user
        FOREIGN KEY (leader_user_id) REFERENCES auth.users(id);
EXCEPTION WHEN duplicate_object THEN null; END $$;

CREATE TABLE IF NOT EXISTS auth.virtual_keys (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id              UUID         NOT NULL REFERENCES auth.users(id),
    key_value_encrypted  BYTEA        NOT NULL,
    key_prefix           VARCHAR(16)  NOT NULL,
    status               auth.key_status NOT NULL DEFAULT 'ACTIVE',
    issued_at            TIMESTAMPTZ  NOT NULL DEFAULT now(),
    expires_at           TIMESTAMPTZ  NOT NULL,
    last_used_at         TIMESTAMPTZ,
    revoked_at           TIMESTAMPTZ,
    revoked_by           UUID         REFERENCES auth.users(id),
    created_at           TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_virtual_keys_user_status ON auth.virtual_keys (user_id, status);
CREATE INDEX IF NOT EXISTS idx_virtual_keys_expires_at  ON auth.virtual_keys (expires_at) WHERE status = 'ACTIVE';

CREATE TABLE IF NOT EXISTS auth.rotation_policies (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope               auth.rotation_scope NOT NULL,
    scope_id            UUID,
    expiry_days         INTEGER      NOT NULL DEFAULT 90,
    auto_renew          BOOLEAN      NOT NULL DEFAULT true,
    notify_days_before  INTEGER[]    NOT NULL DEFAULT '{7,1}',
    is_active           BOOLEAN      NOT NULL DEFAULT true,
    created_by          UUID         NOT NULL REFERENCES auth.users(id),
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_rotation_policies_scope ON auth.rotation_policies (scope, scope_id) WHERE is_active = true;

CREATE TABLE IF NOT EXISTS auth.admin_jwt_configs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    issuer          VARCHAR(512) NOT NULL,
    audience        VARCHAR(512) NOT NULL,
    public_key_pem  TEXT         NOT NULL,
    algorithm       VARCHAR(16)  NOT NULL DEFAULT 'RS256',
    is_active       BOOLEAN      NOT NULL DEFAULT true,
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- ============================================================
-- budget schema — Enums
-- ============================================================

DO $$ BEGIN CREATE TYPE budget.budget_scope  AS ENUM ('TEAM', 'USER'); EXCEPTION WHEN duplicate_object THEN null; END $$;
DO $$ BEGIN CREATE TYPE budget.period_type   AS ENUM ('MONTHLY'); EXCEPTION WHEN duplicate_object THEN null; END $$;
DO $$ BEGIN CREATE TYPE budget.budget_policy AS ENUM ('HARD_BLOCK', 'SOFT_WARNING', 'THROTTLE'); EXCEPTION WHEN duplicate_object THEN null; END $$;

-- ============================================================
-- budget schema — Tables
-- ============================================================

CREATE TABLE IF NOT EXISTS budget.budget_configs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope           budget.budget_scope  NOT NULL,
    scope_id        UUID                 NOT NULL,
    client          VARCHAR(32)          NULL,
    max_budget_usd  NUMERIC(12,4)        NOT NULL,
    period_type     budget.period_type   NOT NULL DEFAULT 'MONTHLY',
    policy          budget.budget_policy NOT NULL DEFAULT 'HARD_BLOCK',
    allocated_by    UUID                 NOT NULL REFERENCES auth.users(id),
    effective_from  DATE                 NOT NULL,
    is_active       BOOLEAN              NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ          NOT NULL DEFAULT now(),
    CONSTRAINT ck_budget_configs_client CHECK (client IS NULL OR client IN ('claude-code','cowork','codex'))
);

-- ⚠️ 이 backfill 은 아래 COALESCE(client, ...) 인덱스보다 **반드시 먼저** 와야 한다.
-- CREATE TABLE IF NOT EXISTS 는 테이블이 이미 있으면 통째로 no-op 이라 위의 인라인
-- `client` 컬럼이 안 생긴다(라이브 DB 업그레이드). 그 상태에서 COALESCE 인덱스를 만들면
-- "column client does not exist" 로 init SQL 이 죽고 → 마이그레이션 Job 실패 →
-- helm pre-upgrade hook 실패로 배포 전체가 중단된다.
-- 실제로 prod(revision 0004, client 컬럼 도입 전) 배포에서 이 순서 때문에 실패했다.
-- fresh DB 에서는 컬럼이 이미 인라인으로 있어 no-op 이므로 양쪽 모두 안전(멱등).
-- 기존 테이블의 인덱스 교체 + CHECK 제약 추가는 alembic 0011 이 담당한다(init SQL 이후 실행).
ALTER TABLE budget.budget_configs ADD COLUMN IF NOT EXISTS client VARCHAR(32);

CREATE INDEX IF NOT EXISTS idx_budget_configs_scope ON budget.budget_configs (scope, scope_id) WHERE is_active = true;

-- 같은 키에 is_active=true 행이 2개 생기면 이후 모든 조회가 scalar_one_or_none() 에서
-- MultipleResultsFound → 500 이 된다 (migration 0024 참조). COALESCE 는 필수 —
-- client IS NULL 인 조직 전체 예산 행은 그냥 (scope, scope_id, client) 로는 보호되지 않는다.
CREATE UNIQUE INDEX IF NOT EXISTS uq_budget_configs_active
    ON budget.budget_configs (scope, scope_id, COALESCE(client, ''))
    WHERE is_active = true;

CREATE TABLE IF NOT EXISTS budget.budget_usages (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope                   budget.budget_scope NOT NULL,
    scope_id                UUID                NOT NULL,
    period                  VARCHAR(7)          NOT NULL,  -- YYYY-MM
    client                  VARCHAR(32)         NULL,
    used_usd                NUMERIC(12,4)       NOT NULL DEFAULT 0,
    limit_usd               NUMERIC(12,4)       NOT NULL,
    last_updated            TIMESTAMPTZ         NOT NULL DEFAULT now(),
    threshold_notified_pcts INTEGER[]           NOT NULL DEFAULT '{}',
    CONSTRAINT ck_budget_usages_client CHECK (client IS NULL OR client IN ('claude-code','cowork','codex'))
);

-- budget_usages 쪽 backfill 도 아래 COALESCE 인덱스보다 먼저 와야 한다(위 budget_configs 와 동일 이유).
ALTER TABLE budget.budget_usages ADD COLUMN IF NOT EXISTS client VARCHAR(32);

CREATE UNIQUE INDEX IF NOT EXISTS idx_budget_usages_unique ON budget.budget_usages (scope, scope_id, period, COALESCE(client,''));

-- NOTE: budget.downgrade_policies는 model.model_aliases FK를 참조하므로
--       model 스키마 블록 이후에 정의됨 (파일 하단 참조).

-- ============================================================
-- model schema — Enums
-- ============================================================

-- ⚠️ fresh-init DB 와 migrated DB 는 **같은 라벨 집합**으로 수렴해야 한다
-- (03_seed_data.sql:91 의 수렴 규약). 아래 두 enum 은 마이그레이션에서도 라벨이 늘어난다:
--   provider    +BEDROCK_MANTLE(0008) +BEDROCK_MANTLE_OPENAI(0016) +BEDROCK_RUNTIME_OPENAI(0031)
--   api_format  +ANTHROPIC_MESSAGES(0008) +OPENAI_RESPONSES(0016)
-- 세 마이그레이션 모두 `ADD VALUE IF NOT EXISTS` 라서, 여기서 미리 만들어 두어도 재실행이 안전하다
-- (fresh-init → 5라벨 생성 후 ADD VALUE 는 no-op / 기존 DB → CREATE TYPE 자체가 no-op).
--
-- 여기 빠뜨리면 조용히 갈린다: fresh-init 뒤 alembic 을 아직 안 돌린 DB 에 새 plane 행을 넣는
-- 순간 `invalid input value for enum` 이고, 반대로 SQLAlchemy 미러는 **읽을 때** 검증하므로
-- 라벨이 실제로 쓰이기 전까지는 아무 증상이 없다(tests/regression/test_high_db_enum_mirror_drift.py).
DO $$ BEGIN CREATE TYPE model.provider          AS ENUM ('BEDROCK', 'OPENMODEL', 'BEDROCK_MANTLE', 'BEDROCK_MANTLE_OPENAI', 'BEDROCK_RUNTIME_OPENAI'); EXCEPTION WHEN duplicate_object THEN null; END $$;
DO $$ BEGIN CREATE TYPE model.api_format        AS ENUM ('BEDROCK_NATIVE', 'OPENAI_COMPATIBLE', 'ANTHROPIC_MESSAGES', 'OPENAI_RESPONSES'); EXCEPTION WHEN duplicate_object THEN null; END $$;
DO $$ BEGIN CREATE TYPE model.model_status      AS ENUM ('ACTIVE', 'INACTIVE'); EXCEPTION WHEN duplicate_object THEN null; END $$;
DO $$ BEGIN CREATE TYPE model.rate_limit_scope  AS ENUM ('USER', 'TEAM', 'GLOBAL'); EXCEPTION WHEN duplicate_object THEN null; END $$;

-- ============================================================
-- model schema — Tables
-- ============================================================

CREATE TABLE IF NOT EXISTS model.model_aliases (
    alias              VARCHAR(128)     PRIMARY KEY,
    provider           model.provider   NOT NULL,
    provider_model_id  VARCHAR(512)     NOT NULL,
    endpoint_url       VARCHAR(1024),
    api_format         model.api_format NOT NULL,
    status             model.model_status NOT NULL DEFAULT 'ACTIVE',
    description        TEXT,
    display_name       VARCHAR(128),
    created_by         UUID             NOT NULL REFERENCES auth.users(id),
    created_at         TIMESTAMPTZ      NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ      NOT NULL DEFAULT now()
);

-- display_name added by migration 0012; ADD COLUMN IF NOT EXISTS keeps init SQL idempotent
-- on pre-existing tables (CREATE TABLE IF NOT EXISTS above is a no-op there).
ALTER TABLE model.model_aliases ADD COLUMN IF NOT EXISTS display_name VARCHAR(128);

-- allowed_clients added by migration 0035 (model × app allow-list).
--
-- ⚠️ This ALTER is load-bearing, not tidiness. Both gateway-proxy and admin-api declare
--    the column on their ModelAlias ORM class, so it appears in EVERY SELECT of
--    model_aliases. A database built from this init SQL alone — compose, a local dev
--    stack, any environment where alembic has not reached 0035 — would raise
--    UndefinedColumn on every model resolution, i.e. 500 on the entire inference path,
--    not just on the allow-list feature.
--
-- No DEFAULT. NULL means "unrestricted"; DEFAULT '{}' would mean "no app may use this
-- model" and would deny every app on every existing alias the moment it applied.
ALTER TABLE model.model_aliases ADD COLUMN IF NOT EXISTS allowed_clients TEXT[];
CREATE TABLE IF NOT EXISTS model.model_pricings (
    id                                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    model_alias                         VARCHAR(128)  NOT NULL REFERENCES model.model_aliases(alias),
    input_price_per_1k_tokens           NUMERIC(10,6) NOT NULL,
    output_price_per_1k_tokens          NUMERIC(10,6) NOT NULL,
    cache_creation_5m_price_per_1k_tokens NUMERIC(10,6) NOT NULL DEFAULT 0,
    cache_creation_1h_price_per_1k_tokens NUMERIC(10,6) NOT NULL DEFAULT 0,
    cache_read_price_per_1k_tokens      NUMERIC(10,6) NOT NULL DEFAULT 0,
    effective_from                      TIMESTAMPTZ   NOT NULL,
    effective_until                     TIMESTAMPTZ,
    created_by                          UUID          NOT NULL REFERENCES auth.users(id)
);

CREATE INDEX IF NOT EXISTS idx_model_pricings_alias ON model.model_pricings (model_alias, effective_from DESC);

CREATE TABLE IF NOT EXISTS model.rate_limit_configs (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope         model.rate_limit_scope NOT NULL,
    scope_id      UUID,
    model_alias   VARCHAR(128)  REFERENCES model.model_aliases(alias),
    rpm_limit     INTEGER,
    tpm_limit     INTEGER,
    cpm_limit_usd NUMERIC(10,4),
    cph_limit_usd NUMERIC(10,4),
    is_active     BOOLEAN       NOT NULL DEFAULT true,
    created_by    UUID          NOT NULL REFERENCES auth.users(id),
    created_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_rate_limit_configs_scope ON model.rate_limit_configs (scope, scope_id) WHERE is_active = true;

-- migration 0024 와 동일. model_alias 는 키에 넣지 않는다 — upsert 가 alias 무관하게
-- 해당 scope 의 활성 행 전부를 비활성화하므로, alias 를 넣으면 실제 중복 창이 열린다.
-- COALESCE(scope_id, ...) 는 필수 — GLOBAL scope 는 scope_id IS NULL 이고,
-- unique index 에서 NULL 은 서로 구별되므로 그냥 (scope, scope_id) 로는 보호되지 않는다.
CREATE UNIQUE INDEX IF NOT EXISTS uq_rate_limit_configs_active
    ON model.rate_limit_configs (scope, COALESCE(scope_id, '00000000-0000-0000-0000-000000000000'::uuid))
    WHERE is_active = true;

-- ------------------------------------------------------------
-- 팀별 모델 접근 제어
-- 정책: team_id 기준 행 0개 = 전체 허용 (하위 호환), 행 존재 = 화이트리스트
-- ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS model.team_allowed_models (
    team_id      UUID         NOT NULL REFERENCES auth.teams(id) ON DELETE CASCADE,
    model_alias  VARCHAR(128) NOT NULL REFERENCES model.model_aliases(alias),
    created_by   UUID         NOT NULL REFERENCES auth.users(id),
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (team_id, model_alias)
);

CREATE INDEX IF NOT EXISTS idx_team_allowed_models_team ON model.team_allowed_models (team_id);

-- 사용자별 모델 접근 제어(팀 정책의 개별 사용자 override). 우선순위: user > team > none.
--   user 행 존재  → 이 화이트리스트만 적용(팀 무시).
--   user 행 0개   → team_allowed_models 로 폴백(override 해제).
-- ★ team_allowed_models 와 달리 "0행=전체허용"이 아니라 "0행=팀 폴백"이다(스냅샷 시점 해결).
--   국가핵심기술 등 동일 팀 내 개별 인원 제한용. (260626_comm_customer 항목2)
CREATE TABLE IF NOT EXISTS model.user_allowed_models (
    user_id      UUID         NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    model_alias  VARCHAR(128) NOT NULL REFERENCES model.model_aliases(alias),
    created_by   UUID         NOT NULL REFERENCES auth.users(id),
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, model_alias)
);
CREATE INDEX IF NOT EXISTS idx_user_allowed_models_user ON model.user_allowed_models (user_id);

-- 사용자별 허용 클라이언트(앱) 정책. 행 0개 = 전체 허용(claude-code + cowork + codex).
-- team_allowed_models 와 동일 관례(0행=allow-all). client 는 client_identifier 토큰만.
-- codex 는 opt-in 권장: allow-all(0행) 이면 누구나 UA 만으로 GPT-5.5 접근 가능하므로
-- 운영에서는 명시 행으로 entitlement 제어(신뢰축 = allowed_clients/model allowlist).
CREATE TABLE IF NOT EXISTS auth.user_allowed_clients (
    user_id    UUID        NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    client     VARCHAR(32) NOT NULL CHECK (client IN ('claude-code','cowork','codex')),
    created_by UUID        NOT NULL REFERENCES auth.users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, client)
);
CREATE INDEX IF NOT EXISTS idx_user_allowed_clients_user ON auth.user_allowed_clients (user_id);

-- ------------------------------------------------------------
-- Budget-aware 모델 다운그레이드 (model.model_aliases FK 때문에 model 블록 이후 정의)
-- 사용자/팀의 월 예산 소진율이 임계치(threshold_pct) 초과 시 상위→하위 모델 자동 전환.
-- One-hop only (chain follow 없음). scope+scope_id 패턴 일관.
-- ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS budget.downgrade_policies (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope             budget.budget_scope NOT NULL,
    scope_id          UUID                NOT NULL,
    threshold_pct     INTEGER             NOT NULL,
    from_model_alias  VARCHAR(128)        NOT NULL REFERENCES model.model_aliases(alias),
    to_model_alias    VARCHAR(128)        NOT NULL REFERENCES model.model_aliases(alias),
    is_active         BOOLEAN             NOT NULL DEFAULT true,
    created_by        UUID                NOT NULL REFERENCES auth.users(id),
    created_at        TIMESTAMPTZ         NOT NULL DEFAULT now(),
    CONSTRAINT ck_downgrade_threshold_range CHECK (threshold_pct BETWEEN 1 AND 100),
    CONSTRAINT ck_downgrade_no_self_loop   CHECK (from_model_alias <> to_model_alias)
);

CREATE INDEX IF NOT EXISTS idx_downgrade_policies_scope
    ON budget.downgrade_policies (scope, scope_id)
    WHERE is_active;

CREATE UNIQUE INDEX IF NOT EXISTS idx_downgrade_policies_unique_active
    ON budget.downgrade_policies (scope, scope_id, from_model_alias)
    WHERE is_active;

-- ============================================================
-- usage schema — Enums
-- ============================================================

DO $$ BEGIN CREATE TYPE usage.usage_status AS ENUM ('SUCCESS', 'ERROR', 'TIMEOUT'); EXCEPTION WHEN duplicate_object THEN null; END $$;
DO $$ BEGIN CREATE TYPE usage.roi_scope    AS ENUM ('USER', 'TEAM', 'DEPT', 'GLOBAL'); EXCEPTION WHEN duplicate_object THEN null; END $$;

-- ============================================================
-- usage schema — Tables
-- ============================================================

CREATE TABLE IF NOT EXISTS usage.usage_logs (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id             VARCHAR(128)  NOT NULL UNIQUE,
    user_id                UUID          NOT NULL REFERENCES auth.users(id),
    team_id                UUID          NOT NULL REFERENCES auth.teams(id),
    dept_id                UUID          NOT NULL REFERENCES auth.departments(id),
    model_alias            VARCHAR(128)  NOT NULL,
    provider               VARCHAR(32)   NOT NULL,
    input_tokens           INTEGER       NOT NULL,
    output_tokens          INTEGER       NOT NULL,
    -- Anthropic Prompt Caching 비용 정확 추적 (FR-3.3). cache_creation 1.25x, cache_read 0.1x 단가.
    cache_creation_tokens  INTEGER       NOT NULL DEFAULT 0,
    cache_read_tokens      INTEGER       NOT NULL DEFAULT 0,
    -- reasoning 토큰 가시화 서브메트릭(0019). 청구 입력 아님 — OpenAI/GPT-5.5 는 reasoning 을
    -- output_tokens 안에 이미 포함하므로 cost/total/TPM 에 재가산 금지. Anthropic extended
    -- thinking 토큰도 여기 기록(없으면 0). 3-client(claude-code/cowork/codex) 공통.
    reasoning_tokens       INTEGER       NOT NULL DEFAULT 0,
    -- 서버사이드 web search 호출 횟수(0020). 청구/토큰 아님 — 가시화·귀속 메트릭(reasoning_tokens
    -- 와 동일 취급, cost/total/TPM 에서 제외). web_search_loop 가 성공 검색마다 1 증가. AgentCore
    -- WebSearch($7/1k) 를 client 별로 귀속하는 용도. 검색 없거나 기능 off 면 0.
    web_search_count       INTEGER       NOT NULL DEFAULT 0,
    cost_usd               NUMERIC(10,6) NOT NULL,
    latency_ms             INTEGER       NOT NULL,
    -- TTFT(time to first token) in ms. 스트리밍 첫 콘텐츠 델타까지의 시간.
    -- 비스트리밍/미검출은 latency_ms와 동일. 과거 데이터는 NULL.
    ttft_ms                INTEGER,
    status                 usage.usage_status NOT NULL,
    requested_at           TIMESTAMPTZ   NOT NULL,
    completed_at           TIMESTAMPTZ   NOT NULL,
    -- FR-3.3 리팩터 (2026-04-20): 스트리밍/추정/다운그레이드 플래그.
    -- is_streaming: SSE 스트리밍 여부(관측성). estimated_usage: KI-08 tokenizer 역산 플래그.
    -- downgraded_from: FR-3.6 budget-aware downgrade 시 원래 요청 모델 alias.
    -- availability_fallback_from: 모델 가용성 fallback 시 원래 요청 모델 alias (availability_fallback).
    is_streaming           BOOLEAN       NOT NULL DEFAULT false,
    estimated_usage        BOOLEAN       NOT NULL DEFAULT false,
    downgraded_from        VARCHAR(128),
    availability_fallback_from VARCHAR(128),
    sso_subject            VARCHAR(512),
    bedrock_request_id     VARCHAR(128)
);

CREATE INDEX IF NOT EXISTS idx_usage_logs_user_time   ON usage.usage_logs (user_id, requested_at DESC);
CREATE INDEX IF NOT EXISTS idx_usage_logs_team_time   ON usage.usage_logs (team_id, requested_at DESC);
CREATE INDEX IF NOT EXISTS idx_usage_logs_model_time  ON usage.usage_logs (model_alias, requested_at DESC);
CREATE INDEX IF NOT EXISTS idx_usage_logs_bedrock_req ON usage.usage_logs (bedrock_request_id);

-- 일별 사용량 집계 (FR-4a.2, /v1/usage/me 의 과거 일자 breakdown 데이터 소스).
-- Granularity: (date, user_id, model_alias). 매일 KST 00:10 scheduler 잡으로 전일
-- usage_logs → 이 테이블 INSERT (idempotent, ON CONFLICT DO NOTHING).
CREATE TABLE IF NOT EXISTS usage.daily_aggregates (
    id                     UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    date                   DATE          NOT NULL,
    user_id                UUID          NOT NULL REFERENCES auth.users(id),
    team_id                UUID          NOT NULL REFERENCES auth.teams(id),
    dept_id                UUID          NOT NULL REFERENCES auth.departments(id),
    model_alias            VARCHAR(128)  NOT NULL,
    input_tokens           INTEGER       NOT NULL DEFAULT 0,
    output_tokens          INTEGER       NOT NULL DEFAULT 0,
    cache_creation_tokens  INTEGER       NOT NULL DEFAULT 0,
    cache_read_tokens      INTEGER       NOT NULL DEFAULT 0,
    total_tokens           INTEGER       NOT NULL DEFAULT 0,
    total_cost_usd         NUMERIC(12,6) NOT NULL DEFAULT 0,
    request_count          INTEGER       NOT NULL DEFAULT 0,
    created_at             TIMESTAMPTZ   NOT NULL DEFAULT now(),
    UNIQUE (date, user_id, model_alias)
);

CREATE INDEX IF NOT EXISTS idx_daily_aggregates_user_date ON usage.daily_aggregates (user_id, date DESC);
CREATE INDEX IF NOT EXISTS idx_daily_aggregates_team_date ON usage.daily_aggregates (team_id, date DESC);
CREATE INDEX IF NOT EXISTS idx_daily_aggregates_date      ON usage.daily_aggregates (date DESC);

-- 요청별 추론/도구 추적 (observability). gateway-proxy 가 응답 본문에서 trace_extractor
-- 로 뽑은 PII-safe 요약(thinking 요약/tool_use/redacted 카운트)을 cost:stream 에 실어
-- cost-recorder-worker 가 INSERT. usage_logs 와 1:1(request_id FK), 별도 테이블인 이유:
--   (1) 본문이 커서 hot-path usage_logs row 를 부풀리면 안 됨,
--   (2) reasoning/tool input 은 민감 → 보존정책·접근권한을 usage_logs 와 분리,
--   (3) trace 는 opt-in/샘플링 대상이라 누락이 정상(LEFT JOIN).
-- 보존정책: requested_at 기준 N일 후 파기 권장(cron). raw chain-of-thought 는 저장 안 함
--   (API 가 summary 만 제공). redacted_thinking 은 암호화라 카운트만(내용 없음).
CREATE TABLE IF NOT EXISTS usage.request_traces (
    request_id               VARCHAR(128)  PRIMARY KEY REFERENCES usage.usage_logs(request_id) ON DELETE CASCADE,
    model_alias              VARCHAR(128)  NOT NULL,
    -- trace_extractor.extract_trace() 출력 전체(thinking_summary/tool_uses/
    -- redacted_thinking_count/text_preview/block_types). 스키마 진화에 유연하도록 JSONB.
    trace                    JSONB         NOT NULL,
    -- 빠른 필터/집계용 비정규화 컬럼(JSONB 안 파지 않고 인덱스 가능).
    tool_use_count           INTEGER       NOT NULL DEFAULT 0,
    has_thinking             BOOLEAN       NOT NULL DEFAULT false,
    redacted_thinking_count  INTEGER       NOT NULL DEFAULT 0,
    requested_at             TIMESTAMPTZ   NOT NULL,
    created_at               TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_request_traces_requested_at ON usage.request_traces (requested_at DESC);
-- "도구를 실제로 쓴 요청만" 류 조회 가속(partial index).
CREATE INDEX IF NOT EXISTS idx_request_traces_tool_use     ON usage.request_traces (requested_at DESC)
    WHERE tool_use_count > 0;
-- tool_use name 같은 JSONB 내부 필드 검색용(GIN).
CREATE INDEX IF NOT EXISTS idx_request_traces_trace_gin    ON usage.request_traces USING GIN (trace);

CREATE TABLE IF NOT EXISTS usage.roi_aggregations (
    id                           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    period                       VARCHAR(7)    NOT NULL,  -- YYYY-MM
    scope                        usage.roi_scope NOT NULL,
    scope_id                     UUID,

    -- Cost metrics
    total_cost_usd               NUMERIC(12,4) NOT NULL DEFAULT 0,
    cost_per_user_usd            NUMERIC(10,4) NOT NULL DEFAULT 0,
    budget_utilization_pct       NUMERIC(5,2)  NOT NULL DEFAULT 0,
    cost_by_model                JSONB         NOT NULL DEFAULT '{}',

    -- Activity metrics
    active_users                 INTEGER       NOT NULL DEFAULT 0,
    active_user_rate_pct         NUMERIC(5,2)  NOT NULL DEFAULT 0,
    requests_per_user_per_day    NUMERIC(8,2)  NOT NULL DEFAULT 0,
    activation_gap_pct           NUMERIC(5,2)  NOT NULL DEFAULT 0,

    -- Productivity metrics (Post-MVP, nullable)
    code_acceptance_rate_pct     NUMERIC(5,2),
    cost_per_accepted_code_usd   NUMERIC(10,4),
    generated_lines_per_session  NUMERIC(8,2),

    -- ROI index (Post-MVP, nullable)
    roi_index                    NUMERIC(8,4),

    aggregated_at                TIMESTAMPTZ   NOT NULL DEFAULT now(),
    aggregated_by                VARCHAR(64)   NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_roi_aggregations_unique ON usage.roi_aggregations (period, scope, scope_id);

-- ============================================================
-- usage schema — Productivity & Git Events (ROI 생산성 추적)
-- ============================================================

DO $$ BEGIN CREATE TYPE usage.productivity_event_type AS ENUM ('CODE_GENERATED', 'CODE_ACCEPTED', 'CODE_REJECTED'); EXCEPTION WHEN duplicate_object THEN null; END $$;
DO $$ BEGIN CREATE TYPE usage.git_event_type AS ENUM ('COMMIT', 'PR_OPENED', 'PR_MERGED'); EXCEPTION WHEN duplicate_object THEN null; END $$;

CREATE TABLE IF NOT EXISTS usage.productivity_events (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id          UUID          NOT NULL REFERENCES auth.users(id),
    team_id          UUID          REFERENCES auth.teams(id),
    event_type       usage.productivity_event_type NOT NULL,
    session_id       VARCHAR(128),
    model_alias      VARCHAR(128),
    lines_generated  INTEGER       NOT NULL DEFAULT 0,
    lines_accepted   INTEGER       NOT NULL DEFAULT 0,
    language         VARCHAR(64),
    created_at       TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_productivity_events_user_time ON usage.productivity_events (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_productivity_events_team_time ON usage.productivity_events (team_id, created_at DESC);

CREATE TABLE IF NOT EXISTS usage.git_events (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id        UUID          REFERENCES auth.users(id),
    user_email     VARCHAR(320)  NOT NULL,
    event_type     usage.git_event_type NOT NULL,
    repo           VARCHAR(512)  NOT NULL,
    ref            VARCHAR(256),
    commit_count   INTEGER       NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_git_events_user_time ON usage.git_events (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_git_events_email_time ON usage.git_events (user_email, created_at DESC);

-- ============================================================
-- audit schema — Tables
-- ============================================================

CREATE TABLE IF NOT EXISTS audit.audit_logs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    timestamp       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    actor_user_id   UUID         NOT NULL,
    actor_role      VARCHAR(32)  NOT NULL,
    action          VARCHAR(128) NOT NULL,
    resource_type   VARCHAR(64)  NOT NULL,
    resource_id     VARCHAR(256) NOT NULL,
    changes         JSONB        NOT NULL DEFAULT '{}',
    result          VARCHAR(32)  NOT NULL,  -- SUCCESS / FAILURE
    ip_address      VARCHAR(45)  NOT NULL,
    request_id      VARCHAR(128) NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_logs_actor   ON audit.audit_logs (actor_user_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_audit_logs_action  ON audit.audit_logs (action, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_audit_logs_resource ON audit.audit_logs (resource_type, resource_id, timestamp DESC);

CREATE TABLE IF NOT EXISTS audit.cache_invalidation_failures (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cache_key     VARCHAR(512) NOT NULL,
    failed_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    retry_count   INTEGER      NOT NULL DEFAULT 0,
    last_retry_at TIMESTAMPTZ,
    resolved_at   TIMESTAMPTZ,
    context       JSONB        NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_cache_inv_failures_unresolved ON audit.cache_invalidation_failures (failed_at)
    WHERE resolved_at IS NULL;

-- ============================================================
-- auth schema — Service tokens (외부 시스템 admin-api 인증)
-- ============================================================

-- service_tokens: external-system bearer tokens for admin-api (svc-...).
-- sha256 hash 만 저장하고 plaintext 는 issue/rotate 시 1회만 반환. (alembic 0015)
CREATE TABLE IF NOT EXISTS auth.service_tokens (
    id           UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    name         VARCHAR(128) NOT NULL,
    token_hash   VARCHAR(64)  NOT NULL UNIQUE,
    token_prefix VARCHAR(16)  NOT NULL,
    created_by   UUID         NOT NULL REFERENCES auth.users(id),
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ  NOT NULL,
    revoked_at   TIMESTAMPTZ,
    rotated_from UUID         REFERENCES auth.service_tokens(id)
);

CREATE INDEX IF NOT EXISTS idx_service_tokens_hash ON auth.service_tokens (token_hash);


-- ⚠️ 아래 블록은 **선택적 문서화가 아니라 필수**다.
--
-- ⚠️ 위치가 중요하다 — 이 블록은 파일 **맨 끝**에 있어야 한다. 참조하는 테이블
--    (usage.usage_logs, usage.productivity_events)이 위에서 만들어진 뒤여야 ALTER 가
--    성립하고, init SQL 은 `ON_ERROR_STOP=1` 로 실행되므로 한 문장이 실패하면 **파일의
--    나머지가 통째로 적용되지 않는다**(실제로 이 블록을 중간에 넣었다가 생성 컬럼이
--    237개→84개로 줄었다).
--
-- 서비스들의 ORM 이 선언한 컬럼은 그 테이블의 **모든 SELECT/INSERT** 에 들어간다. 그래서
-- init SQL 로만 만든 DB(compose, 로컬 개발, alembic 이 아직 닿지 않은 환경)에 컬럼이 없으면
-- 그 기능만 죽는 것이 아니라 그 테이블을 쓰는 경로 전체가 UndefinedColumn 으로 죽는다.
-- 실제로 그런 상태였다: `usage.usage_logs.client` 가 없어 cost-recorder-worker 의 배치
-- INSERT 가 전부 실패했고(사용량 행 0개), `model.routing_profiles` 와
-- `public.system_settings` 는 테이블 자체가 없었다.
--
-- 이 파일과 ORM 의 차집합은 admin-api 의
-- tests/regression/test_high_init_sql_matches_orm.py 가 실 PG 로 자동 대조한다 —
-- 새 마이그레이션을 넣고 여기를 빠뜨리면 그 테스트가 잡는다.

-- migration 0007: 요청을 발생시킨 앱 태그(claude-code | cowork | codex | other | NULL).
ALTER TABLE usage.usage_logs ADD COLUMN IF NOT EXISTS client TEXT;
CREATE INDEX IF NOT EXISTS ix_usage_logs_client ON usage.usage_logs (client);

-- migration 0034: 생산성 이벤트 멱등키. 부분 유니크 인덱스라 NULL 은 중복을 허용한다
-- (멱등키를 보내지 않는 옛 클라이언트를 막지 않기 위해서다).
ALTER TABLE usage.productivity_events ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(256);
CREATE UNIQUE INDEX IF NOT EXISTS idx_productivity_events_idempotency
    ON usage.productivity_events (idempotency_key)
    WHERE idempotency_key IS NOT NULL;

-- migration 0009(+0021 web_search_enabled, +0032 backend CHECK 확장): 앱별 라우팅 프로파일.
-- ⚠️ CHECK 값은 **0032 적용 후의 최종 상태**로 만든다. 0009 시점의 ('invoke','mantle') 로
--    만들면 codex 의 bedrock_openai 백엔드 행 삽입이 CHECK 위반으로 거부된다.
CREATE TABLE IF NOT EXISTS model.routing_profiles (
    client              TEXT        PRIMARY KEY,
    backend             TEXT        NOT NULL,
    account_role_arn    TEXT,
    region              TEXT        NOT NULL,
    default_model       TEXT,
    external_id         TEXT,
    enabled             BOOLEAN     NOT NULL DEFAULT true,
    web_search_enabled  BOOLEAN     NOT NULL DEFAULT false,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT routing_profiles_backend_check
        CHECK (backend IN ('invoke','mantle','bedrock_openai'))
);
-- 이미 존재하는 테이블(0009~0021 사이에서 만들어진 DB)에도 컬럼이 있도록.
ALTER TABLE model.routing_profiles
    ADD COLUMN IF NOT EXISTS web_search_enabled BOOLEAN NOT NULL DEFAULT false;

-- migration 0036: 런타임에 바꿀 수 있는 전역 설정. 행이 없으면 "미설정" 이고 소비자가
-- 자기 기본값을 쓴다(body logging 은 OFF).
CREATE TABLE IF NOT EXISTS public.system_settings (
    key         TEXT        PRIMARY KEY,
    value       JSONB       NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by  UUID        REFERENCES auth.users(id)
);

-- migration 0037: 운영자가 설정한 예산 알림 임계값. UI/API/Lua 는 이미 이 값을 다루는데
-- 저장할 컬럼이 없어서, Redis 설정 키(ex=300)가 만료되면 gateway-proxy 의 재수화가
-- 기본값으로 되돌려 놓았다 — 운영자 설정이 5분만 살아 있었다.
--
-- ⚠️ NOT NULL DEFAULT '{80,90,100}' 은 기존 행에 지금 코드가 쓰는 값과 같은 값을 채운다.
--    NULL 을 허용하면 읽는 쪽마다 "NULL=기본값" 을 구현해야 하고, 한 곳이 빠지면 임계값이
--    빈 목록으로 읽혀 알림이 조용히 사라진다.
-- 1..100 은 UI/API 를 거치지 않는 경로(직접 SQL, 시드)도 막아야 한다. 넣힌 0 은
-- "매 첫 요청마다 알림"(old_pct=0 이고 new_pct>=0), 150 은 "절대 발동 안 함" 이 되고
-- 둘 다 조용하다.
-- 1..100 을 도메인으로 강제한다. CHECK 절에는 서브쿼리를 쓸 수 없어서
-- ``<@ ARRAY(SELECT generate_series(1,100))`` 는 문법 오류이고(실측: ON_ERROR_STOP 로 파일
-- 나머지가 버려진다), PostgreSQL 은 **도메인 제약을 배열 원소마다** 적용한다(실측: '{150}'
-- 과 '{0,80}' 거부, '{}' 와 기본값 허용).
DO $$ BEGIN
    CREATE DOMAIN budget.alert_pct AS INTEGER CHECK (VALUE BETWEEN 1 AND 100);
EXCEPTION WHEN duplicate_object THEN null; END $$;
ALTER TABLE budget.budget_configs
    ADD COLUMN IF NOT EXISTS alert_thresholds budget.alert_pct[]
    NOT NULL DEFAULT '{80,90,100}';


-- migration 0038: context 크기축 요금제(272K long-context). GPT-5.6 은 프롬프트가 임계를
-- 넘으면 요청 전체를 long 요율로 청구한다. threshold NULL = 티어 없음(오늘 동작·Claude).
-- 배수(long=short×2)가 아니라 명시 요율 컬럼을 두는 이유는 AWS Price List 자동연동 때문이다
-- (Price List 는 달러 요율을 준다). 컬럼은 전부 NULLABLE(NULL='이 버킷 long 미설정').
ALTER TABLE model.model_pricings ADD COLUMN IF NOT EXISTS long_context_threshold_tokens INTEGER;
ALTER TABLE model.model_pricings ADD COLUMN IF NOT EXISTS long_context_input_price_per_1k_tokens NUMERIC(10,6);
ALTER TABLE model.model_pricings ADD COLUMN IF NOT EXISTS long_context_output_price_per_1k_tokens NUMERIC(10,6);
ALTER TABLE model.model_pricings ADD COLUMN IF NOT EXISTS long_context_cache_creation_5m_price_per_1k_tokens NUMERIC(10,6);
ALTER TABLE model.model_pricings ADD COLUMN IF NOT EXISTS long_context_cache_creation_1h_price_per_1k_tokens NUMERIC(10,6);
ALTER TABLE model.model_pricings ADD COLUMN IF NOT EXISTS long_context_cache_read_price_per_1k_tokens NUMERIC(10,6);

-- 값-가드 시딩: 열린 gpt-5.6 행의 현재 short 요율에 카드 배수(in×2·out×1.5·cache×2)를 곱해
-- long 을 채운다. alias 이름/plane 무관하게 pub 의 실제 short 요율을 추적한다.
-- ⚠️ 이 시드 데이터를 쓰는 gpt-5.6 행 자체는 마이그레이션 0025/0032 에서만 생기므로, 이
--    UPDATE 는 init-only DB 에서는 매칭 0건(무해)이고 마이그레이션까지 적용된 DB 에서만 실효.
UPDATE model.model_pricings p
SET long_context_threshold_tokens = 272000,
    long_context_input_price_per_1k_tokens = p.input_price_per_1k_tokens * 2,
    long_context_output_price_per_1k_tokens = p.output_price_per_1k_tokens * 1.5,
    long_context_cache_creation_5m_price_per_1k_tokens = p.cache_creation_5m_price_per_1k_tokens * 2,
    long_context_cache_creation_1h_price_per_1k_tokens = p.cache_creation_1h_price_per_1k_tokens * 2,
    long_context_cache_read_price_per_1k_tokens = p.cache_read_price_per_1k_tokens * 2
FROM model.model_aliases a
WHERE a.alias = p.model_alias
  AND p.effective_until IS NULL
  AND p.long_context_threshold_tokens IS NULL
  AND a.provider_model_id LIKE '%openai.gpt-5.6-%';

-- migration 0039: 요청별 청구 티어 감사. 게이트웨이 resolve_context_tier 가 청구와 이 값을
-- 같은 판정으로 채운다(청구=long/기록=short 불일치 방지). 'short'|'long'|NULL(티어없는 모델).
ALTER TABLE usage.usage_logs ADD COLUMN IF NOT EXISTS context_tier VARCHAR(8);
