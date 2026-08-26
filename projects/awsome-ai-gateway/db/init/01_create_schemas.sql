-- Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

-- 01_create_schemas.sql
-- LLM Gateway — Schema creation
-- Shared by: U1 Gateway Proxy, U2 Admin API

CREATE SCHEMA IF NOT EXISTS auth;
CREATE SCHEMA IF NOT EXISTS budget;
CREATE SCHEMA IF NOT EXISTS model;
CREATE SCHEMA IF NOT EXISTS usage;
CREATE SCHEMA IF NOT EXISTS audit;
CREATE SCHEMA IF NOT EXISTS notification;

-- chat_agent: admin-chat-agent(BI) 의 sessions/messages/embeddings 스키마.
-- 테이블은 alembic 마이그레이션(0005)이 생성하지만, run_migration.sh 의 SCHEMAS GRANT 루프와
-- 08_create_chat_reader.sql 이 이 스키마 존재를 전제하므로 여기서 먼저 만든다.
-- (누락 시 부분-마이그레이션 DB(prod 0004 등)에서 "schema chat_agent does not exist" 로 init 실패.)
--
-- ⚠️ 이 순서가 앱유저 권한의 전제다: 스키마가 GRANT 루프보다 **먼저** 존재해야
--    run_migration.sh 가 ALTER DEFAULT PRIVILEGES 를 걸 수 있고, 그래야 뒤이어
--    alembic(master 권한)이 만드는 chat_agent 테이블에 gateway 권한이 자동 적용된다.
--    run_migration.sh 의 SCHEMAS 에서 chat_agent 를 빼면 런타임에
--    `permission denied for schema chat_agent` 로 chat 세션 생성이 500 이 된다.
CREATE SCHEMA IF NOT EXISTS chat_agent;

-- pgcrypto extension (UUID 생성용)
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
