# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""add team leader_user_id and notification schema

Revision ID: 0001
Revises:
Create Date: 2026-04-10

Changes:
- auth.teams: add leader_user_id (nullable FK → auth.users)
- notification schema: notification_configs, notification_logs tables
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # auth.teams: add leader_user_id
    # ------------------------------------------------------------------
    op.add_column(
        "teams",
        sa.Column("leader_user_id", UUID(as_uuid=False), nullable=True),
        schema="auth",
    )
    op.create_foreign_key(
        "fk_teams_leader_user_id",
        "teams",
        "users",
        ["leader_user_id"],
        ["id"],
        source_schema="auth",
        referent_schema="auth",
    )

    # ------------------------------------------------------------------
    # notification schema
    # ------------------------------------------------------------------
    op.execute("CREATE SCHEMA IF NOT EXISTS notification")

    op.create_table(
        "notification_configs",
        sa.Column("id", UUID(as_uuid=False), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("event_type", sa.String(50), nullable=False, unique=True),
        sa.Column("recipient_roles", JSONB, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        schema="notification",
    )

    op.create_table(
        "notification_logs",
        sa.Column("id", UUID(as_uuid=False), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("event_id", sa.String(100), nullable=False),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False, server_default=sa.text("'email'")),
        sa.Column("recipient_email", sa.String(255), nullable=False),
        sa.Column("recipient_user_id", sa.String(100), nullable=True),
        sa.Column("subject", sa.String(500), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("attempt_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("event_payload", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        schema="notification",
    )

    op.create_index(
        "ix_notification_logs_event_id",
        "notification_logs",
        ["event_id"],
        schema="notification",
    )
    op.create_index(
        "ix_notification_logs_status",
        "notification_logs",
        ["status"],
        schema="notification",
    )
    op.create_index(
        "ix_notification_logs_created_at",
        "notification_logs",
        ["created_at"],
        schema="notification",
    )


def downgrade() -> None:
    op.drop_index("ix_notification_logs_created_at", "notification_logs", schema="notification")
    op.drop_index("ix_notification_logs_status", "notification_logs", schema="notification")
    op.drop_index("ix_notification_logs_event_id", "notification_logs", schema="notification")
    op.drop_table("notification_logs", schema="notification")
    op.drop_table("notification_configs", schema="notification")
    op.execute("DROP SCHEMA IF EXISTS notification")

    op.drop_constraint("fk_teams_leader_user_id", "teams", schema="auth", type_="foreignkey")
    op.drop_column("teams", "leader_user_id", schema="auth")
