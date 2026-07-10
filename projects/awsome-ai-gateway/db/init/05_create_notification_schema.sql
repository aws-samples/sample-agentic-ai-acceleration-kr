-- Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

-- 05_create_notification_schema.sql
-- LLM Gateway — Notification Worker schema
-- Owner: U3 Notification Worker

CREATE SCHEMA IF NOT EXISTS notification;

-- ============================================================
-- notification_configs: per-event-type recipient configuration
-- ============================================================

CREATE TABLE IF NOT EXISTS notification.notification_configs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type      VARCHAR(50)  NOT NULL UNIQUE,
    recipient_roles JSONB        NOT NULL DEFAULT '[]',
    enabled         BOOLEAN      NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- ============================================================
-- notification_logs: delivery history (all attempts)
-- ============================================================

CREATE TABLE IF NOT EXISTS notification.notification_logs (
    id                 UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id           VARCHAR(100) NOT NULL,
    event_type         VARCHAR(50)  NOT NULL,
    channel            VARCHAR(20)  NOT NULL DEFAULT 'email',
    recipient_email    VARCHAR(255) NOT NULL,
    recipient_user_id  VARCHAR(100),
    subject            VARCHAR(500) NOT NULL,
    status             VARCHAR(20)  NOT NULL DEFAULT 'pending',
    attempt_count      INTEGER      NOT NULL DEFAULT 0,
    last_attempt_at    TIMESTAMPTZ,
    error_message      TEXT,
    event_payload      JSONB        NOT NULL,
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
    resolved_at        TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_notification_logs_event_id
    ON notification.notification_logs(event_id);

CREATE INDEX IF NOT EXISTS ix_notification_logs_status
    ON notification.notification_logs(status);

CREATE INDEX IF NOT EXISTS ix_notification_logs_created_at
    ON notification.notification_logs(created_at);

-- NOTE: auth.teams.leader_user_id는 02_create_tables.sql에서 이미 선언됨.
