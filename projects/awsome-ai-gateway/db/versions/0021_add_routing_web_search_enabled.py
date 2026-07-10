# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""add web_search_enabled to routing_profiles + enable for all 3 clients

Revision ID: 0021
Revises: 0020
Create Date: 2026-07-01

Per-client toggle for the server-side web-search loop (AgentCore Gateway). The
gateway injects a web_search tool, intercepts tool_use, calls AgentCore's managed
WebSearch connector, and stitches results back — 1P-style server-side search.
Only clients with web_search_enabled=true get the tool injected.

claude-code has NO routing_profiles row today (it falls through _select_backend to
the plain Bedrock path). To gate web search uniformly by profile across ALL three
clients, we ADD a claude-code row here with backend='invoke' (Bedrock native path)
and NO default_model — so _select_backend Rule A/B still do NOT fire (both require a
Mantle default_model / Mantle alias), preserving the existing Bedrock routing. The
row exists purely to carry web_search_enabled (and future per-client flags).

Additive + backward-compatible: column DEFAULT false; the seed UPDATE/INSERT are
idempotent (ON CONFLICT DO NOTHING / WHERE guards).
"""
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1) Column (idempotent — the ORM model also declares server_default false).
    op.execute(
        "ALTER TABLE model.routing_profiles "
        "ADD COLUMN IF NOT EXISTS web_search_enabled boolean NOT NULL DEFAULT false"
    )

    # 2) claude-code routing_profiles row. backend='invoke' + default_model NULL keeps
    #    it on the Bedrock native path (Rule A/B in _select_backend require a Mantle
    #    default_model / alias, so they won't fire). region is required by the schema;
    #    ap-northeast-2 matches the pod home region (unused for the Bedrock path).
    op.execute(
        """
        INSERT INTO model.routing_profiles
            (client, backend, account_role_arn, region, default_model, external_id,
             enabled, web_search_enabled)
        VALUES
            ('claude-code', 'invoke', NULL, 'ap-northeast-2', NULL, NULL, true, true)
        ON CONFLICT (client) DO UPDATE SET web_search_enabled = true
        """
    )

    # 3) Enable web search for the existing cowork / codex rows.
    op.execute(
        "UPDATE model.routing_profiles SET web_search_enabled = true "
        "WHERE client IN ('cowork', 'codex')"
    )


def downgrade() -> None:
    # Remove the claude-code row we added (leave cowork/codex rows; just drop the flag).
    op.execute("DELETE FROM model.routing_profiles WHERE client = 'claude-code'")
    op.execute(
        "ALTER TABLE model.routing_profiles DROP COLUMN IF EXISTS web_search_enabled"
    )
