-- Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

-- 07_grant_notification_privileges.sql
-- Must run AFTER 05_create_notification_schema.sql creates the tables
-- and AFTER 04_create_users.sql creates the notification_worker_user role

-- notification: SELECT + INSERT + UPDATE (NotificationConfig read, NotificationLog write)
GRANT USAGE ON SCHEMA notification TO notification_worker_user;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA notification TO notification_worker_user;
