"""Slack DM integration: one Slack app per Harness, over Socket Mode.

    service.py     admin API behind routes/slack.py: test, create, delete
    connection.py  one Socket Mode connection per integration, and the manager
    handler.py     a DM event -> one turn through StreamingService
    identity.py    Slack user -> platform user (Cognito / OIDC)
    repository.py  the `${project}-slack` table
    api.py         Slack Web API over httpx
    manifest.py    the DM-only app manifest
    formatting.py  Slack text in, Markdown chunks out

Tokens are read from Secrets Manager when needed and never logged, returned or
written to the table. See docs in server/README.md ("Slack DM 연동").
"""
