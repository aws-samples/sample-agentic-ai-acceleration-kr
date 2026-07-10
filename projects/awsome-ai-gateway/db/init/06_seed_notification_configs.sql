-- Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

-- 06_seed_notification_configs.sql
-- LLM Gateway — Notification Worker default configs
-- Seeds 10 event types with default recipient roles

INSERT INTO notification.notification_configs (event_type, recipient_roles, enabled)
VALUES
    ('budget_threshold',    '["affected_user", "team_leader"]', true),
    ('key_expiring',        '["affected_user"]',                true),
    ('key_expired',         '["affected_user"]',                true),
    ('key_revoked',         '["affected_user", "admin"]',       true),
    ('auth_failure_spike',  '["admin"]',                        true),
    ('permission_violation','["admin"]',                        true),
    ('suspicious_usage',    '["admin"]',                        true),
    ('degradation_mode',    '["admin"]',                        true),
    ('provider_error',      '["admin"]',                        true),
    ('service_health_change','["admin"]',                       true)
ON CONFLICT (event_type) DO NOTHING;
