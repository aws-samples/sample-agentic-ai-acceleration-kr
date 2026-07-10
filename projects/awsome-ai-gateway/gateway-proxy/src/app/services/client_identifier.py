# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Identify the calling client (Claude Code / Cowork / Codex) from request headers.

Claude Code and Cowork both send a `claude-cli/X.Y.Z` User-Agent prefix; the ONLY
reliable differentiator is the surface token in parentheses. Cowork is checked
first so it is never misclassified as claude-code. See COWORK-vs-CLAUDE-CODE.md §B.

Codex (OpenAI Codex CLI on Bedrock) is a DIFFERENT client family — it speaks the
OpenAI Responses API, not the Anthropic `claude-cli/` wire, and self-identifies via
the `originator` header (default `codex_cli_rs`) and a `codex` User-Agent token.
It is checked before the `claude-cli/` fallback; the families don't overlap.

This is a pure function with no I/O — the UA/originator are untrusted, spoofable
tags used for logging/analytics + routing-profile selection, NOT an authorization
signal. The trust axis stays VK + team/org + allowed_clients allow-list; a missed
classification only degrades to 'other' (no routing profile), never a privilege.
"""

from __future__ import annotations

CLIENT_CLAUDE_CODE = "claude-code"
CLIENT_COWORK = "cowork"
CLIENT_CODEX = "codex"
CLIENT_OTHER = "other"


def identify_client(headers: dict[str, str]) -> str:
    """Return 'claude-code', 'cowork', 'codex', or 'other' from request headers.

    `headers` keys may be any case; we normalize to lowercase.
    """
    h = {k.lower(): v for k, v in headers.items()}
    ua = h.get("user-agent", "")
    platform = h.get("anthropic-client-platform", "")
    # OpenAI Codex CLI tags every request with an originator header (default
    # `codex_cli_rs`); newer/older builds may also surface a `codex` UA token.
    originator = h.get("originator", "")

    # Cowork first — both Anthropic clients carry the claude-cli/ prefix.
    if (
        platform == "desktop_app"
        or "claude-desktop-3p" in ua
        or "local-agent" in ua
        or ("Electron/" in ua and "Claude/" in ua)  # Cowork healthcheck UA
    ):
        return CLIENT_COWORK
    # Codex — distinct OpenAI client family (Responses API). Match the originator
    # header first (most reliable), then a UA token as a fallback.
    if (
        originator.startswith("codex")
        or "codex_cli_rs" in ua
        or ua.startswith("codex/")
        or "codex-cli" in ua
    ):
        return CLIENT_CODEX
    if ua.startswith("claude-cli/"):
        return CLIENT_CLAUDE_CODE
    return CLIENT_OTHER
