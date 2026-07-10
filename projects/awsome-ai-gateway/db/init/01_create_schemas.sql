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
-- 테이블은 alembic 마이그레이션이 생성하지만, run_migration.sh 의 SCHEMAS GRANT 루프와
-- 08_create_chat_reader.sql 이 이 스키마 존재를 전제하므로 여기서 먼저 만든다.
-- (누락 시 부분-마이그레이션 DB(prod 0004 등)에서 "schema chat_agent does not exist" 로 init 실패.)
CREATE SCHEMA IF NOT EXISTS chat_agent;

-- pgcrypto extension (UUID 생성용)
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
