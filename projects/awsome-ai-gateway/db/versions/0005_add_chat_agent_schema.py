# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Add chat_agent schema for admin BI assistant (Phase 2).

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-28

docs/admin-chat-agent-spec.md §4.3, §2.5 의 admin-chat-agent 인프라:
- chat_agent.sessions / messages — 대화 history
- chat_agent.schema_embeddings — Schema Linking RAG (pgvector)
- chat_agent.golden_examples — Few-shot retrieval (DAIL-SQL)
- audit.chat_agent_queries — admin 도구 사용 audit log

pgvector extension 도 활성화. Aurora PostgreSQL 16 에서 지원.
"""

from alembic import op


revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ─── Extensions ───
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ─── chat_agent schema ───
    op.execute("CREATE SCHEMA IF NOT EXISTS chat_agent")

    # 4.3.1 sessions
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_agent.sessions (
            id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id         uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
            agentcore_id    text,
            title           text,
            status          text NOT NULL DEFAULT 'active',
            created_at      timestamptz NOT NULL DEFAULT now(),
            updated_at      timestamptz NOT NULL DEFAULT now(),
            expires_at      timestamptz,
            message_count   integer NOT NULL DEFAULT 0,
            total_cost_usd  numeric(10,6) NOT NULL DEFAULT 0,
            CONSTRAINT chat_agent_sessions_status_check
                CHECK (status IN ('active', 'expired', 'archived'))
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS chat_agent_sessions_user_idx "
        "ON chat_agent.sessions (user_id, updated_at DESC)"
    )

    # 4.3.1 messages
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_agent.messages (
            id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            session_id      uuid NOT NULL REFERENCES chat_agent.sessions(id) ON DELETE CASCADE,
            role            text NOT NULL,
            content         text,
            tool_calls      jsonb,
            charts          jsonb,
            validator       jsonb,
            cost_usd        numeric(10,6),
            duration_ms     integer,
            created_at      timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT chat_agent_messages_role_check
                CHECK (role IN ('user', 'assistant', 'tool'))
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS chat_agent_messages_session_idx "
        "ON chat_agent.messages (session_id, created_at)"
    )

    # 2.5.1 schema_embeddings — Schema Linking RAG
    # 1024-dim vector (Bedrock Titan Text Embedding v2)
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_agent.schema_embeddings (
            id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            schema_name     text NOT NULL,
            table_name      text NOT NULL,
            column_name     text NOT NULL,
            description     text,
            sample_values   jsonb,
            embedding       vector(1024),
            created_at      timestamptz NOT NULL DEFAULT now(),
            UNIQUE (schema_name, table_name, column_name)
        )
        """
    )
    # HNSW index for efficient cosine similarity search
    op.execute(
        "CREATE INDEX IF NOT EXISTS chat_agent_schema_embeddings_hnsw_idx "
        "ON chat_agent.schema_embeddings USING hnsw (embedding vector_cosine_ops)"
    )

    # 2.5.2 golden_examples — Few-shot retrieval (DAIL-SQL)
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_agent.golden_examples (
            id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            question        text NOT NULL,
            sql             text NOT NULL,
            embedding       vector(1024),
            used_count      integer NOT NULL DEFAULT 0,
            success_rate    numeric(3,2),
            created_at      timestamptz NOT NULL DEFAULT now(),
            created_by      text NOT NULL DEFAULT 'manual'
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS chat_agent_golden_examples_hnsw_idx "
        "ON chat_agent.golden_examples USING hnsw (embedding vector_cosine_ops)"
    )

    # 6.4 audit table
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS audit.chat_agent_queries (
            id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            session_id        uuid NOT NULL,
            message_id        uuid,
            user_id           uuid NOT NULL,
            user_email        text NOT NULL,
            user_question     text NOT NULL,
            agent_path        text[],
            generated_sql     text,
            validator_verdict text,
            validator_reason  text,
            row_count         integer,
            columns_seen      text[],
            schemas_seen      text[],
            pii_columns_seen  text[],
            s3_staging_uri    text,
            code_executed     text,
            total_cost_usd    numeric(10,6),
            duration_ms       integer,
            created_at        timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS audit_chat_agent_queries_user_idx "
        "ON audit.chat_agent_queries (user_email, created_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS audit_chat_agent_queries_created_idx "
        "ON audit.chat_agent_queries (created_at)"
    )

    # ─── chat_reader role 의 chat_agent schema 권한 ───
    # gateway_chat_reader role 은 별도 sql 스크립트 (db/init/08_create_chat_reader.sql) 또는
    # Terraform Aurora 모듈에서 생성. 여기선 schema 만 만들고 권한은 별도.


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS audit.chat_agent_queries")
    op.execute("DROP TABLE IF EXISTS chat_agent.golden_examples")
    op.execute("DROP TABLE IF EXISTS chat_agent.schema_embeddings")
    op.execute("DROP TABLE IF EXISTS chat_agent.messages")
    op.execute("DROP TABLE IF EXISTS chat_agent.sessions")
    op.execute("DROP SCHEMA IF EXISTS chat_agent")
    # vector extension 은 다른 곳에서 쓸 수 있으니 그대로
