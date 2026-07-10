# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Client identification from request headers — fixtures are REAL captured UAs.

Source of truth: COWORK-vs-CLAUDE-CODE.md §B (실측). Cowork must be checked
before claude-code because both send the `claude-cli/` prefix.
"""

import pytest

from app.services.client_identifier import identify_client

CLAUDE_CODE_CLI = {"user-agent": "claude-cli/2.1.173 (external, cli)"}
CLAUDE_CODE_SDK = {"user-agent": "claude-cli/2.1.173 (external, sdk-cli)"}
COWORK_DESKTOP3P = {"user-agent": "claude-cli/2.1.177 (external, claude-desktop-3p)"}
COWORK_LOCAL_AGENT = {
    "user-agent": "claude-cli/2.1.177 (external, local-agent, agent-sdk/1.0)"
}
COWORK_PLATFORM_HEADER = {
    "user-agent": "claude-cli/2.1.177 (external, cli)",
    "anthropic-client-platform": "desktop_app",
}
COWORK_HEALTHCHECK = {"user-agent": "Electron/42.4.0 Claude/1.13576.4"}
# Codex (OpenAI Codex CLI on Bedrock) — distinct OpenAI client family. Self-identifies
# via the `originator` header (default codex_cli_rs) and/or a codex UA token. NOT
# claude-cli/, so it never collides with the Anthropic clients above.
CODEX_ORIGINATOR = {
    "user-agent": "codex_cli_rs/0.141.0",
    "originator": "codex_cli_rs",
}
CODEX_UA_ONLY = {"user-agent": "codex_cli_rs/0.141.0 (external)"}
CODEX_ORIGINATOR_GENERIC = {"user-agent": "python-httpx/0.27", "originator": "codex_exec"}


@pytest.mark.parametrize(
    "headers,expected",
    [
        (CLAUDE_CODE_CLI, "claude-code"),
        (CLAUDE_CODE_SDK, "claude-code"),
        (COWORK_DESKTOP3P, "cowork"),
        (COWORK_LOCAL_AGENT, "cowork"),
        (COWORK_PLATFORM_HEADER, "cowork"),
        (COWORK_HEALTHCHECK, "cowork"),
        (CODEX_ORIGINATOR, "codex"),
        (CODEX_UA_ONLY, "codex"),
        (CODEX_ORIGINATOR_GENERIC, "codex"),
        ({"user-agent": "claude-cli/9.9.9 (external, something-new)"}, "claude-code"),
        ({"user-agent": "curl/8.1.0"}, "other"),
        ({}, "other"),
    ],
)
def test_identify_client(headers, expected):
    assert identify_client(headers) == expected


def test_header_keys_are_case_insensitive():
    # ASGI lowercases header names, but guard against callers passing mixed case.
    assert identify_client({"User-Agent": "claude-cli/2.1 (external, cli)"}) == "claude-code"
