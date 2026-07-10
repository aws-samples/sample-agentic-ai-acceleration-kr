# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Remove user_arn columns and rename to sso_subject in usage logs.

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-28

Changes:
- ALTER TABLE auth.users DROP COLUMN user_arn
- ALTER TABLE usage.usage_logs RENAME COLUMN user_arn TO sso_subject

Rationale:
With OIDC authentication, we no longer use AWS IAM ARNs. Instead, we store
the OIDC `sub` claim (already in auth.users.sso_subject) and use it for
Bedrock metadata. The usage_logs.user_arn column is renamed to sso_subject
to reflect this change while preserving historical data (NULL for OIDC users
before this migration).
"""
from alembic import op


# revision identifiers
revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Drop user_arn column from auth.users (redundant with sso_subject)
    op.execute("ALTER TABLE auth.users DROP COLUMN IF EXISTS user_arn")

    # 2. Rename user_arn -> sso_subject in usage.usage_logs (idempotent).
    #    Clean-slate 배포에서는 02_create_tables.sql 이 이미 sso_subject 로 생성해
    #    user_arn 컬럼 자체가 없음 → rename skip.
    #    기존 DB 에서만 실제 rename 수행.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'usage'
                  AND table_name   = 'usage_logs'
                  AND column_name  = 'user_arn'
            ) THEN
                ALTER TABLE usage.usage_logs RENAME COLUMN user_arn TO sso_subject;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    # Reverse: rename back and restore column (idempotent).
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'usage'
                  AND table_name   = 'usage_logs'
                  AND column_name  = 'sso_subject'
            ) THEN
                ALTER TABLE usage.usage_logs RENAME COLUMN sso_subject TO user_arn;
            END IF;
        END $$;
        """
    )
    op.execute("ALTER TABLE auth.users ADD COLUMN IF NOT EXISTS user_arn VARCHAR(512)")
