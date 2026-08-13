# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Managed settings file management for Claude Code.

Writes gateway configuration to /etc/claude-code/managed-settings.d/
which has the highest priority in Claude Code's settings hierarchy.

Linux/WSL: /etc/claude-code/managed-settings.d/50-gateway.json
Windows:   C:\\Program Files\\ClaudeCode\\managed-settings.d\\50-gateway.json
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import structlog

log = structlog.get_logger(component="cli")

GATEWAY_SETTINGS_FILENAME = "50-gateway.json"


def _managed_dir() -> Path:
    """Return the managed-settings.d directory path for the current platform."""
    if sys.platform == "win32":
        return Path(r"C:\Program Files\ClaudeCode\managed-settings.d")
    return Path("/etc/claude-code/managed-settings.d")


def _managed_file() -> Path:
    return _managed_dir() / GATEWAY_SETTINGS_FILENAME


def is_gateway_enabled() -> bool:
    """Check if gateway managed settings file exists."""
    return _managed_file().is_file()


def read_gateway_settings() -> dict | None:
    """Read the current gateway managed settings, or None if not present."""
    path = _managed_file()
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def build_gateway_settings(
    gateway_url: str,
    admin_api_url: str,
    api_key_helper_path: str,
    otel_endpoint: str | None = None,
    otel_auth_token: str | None = None,
    oidc_issuer_url: str | None = None,
    oidc_client_id: str | None = None,
    oidc_audience: str | None = None,
) -> dict:
    """Assemble the managed-settings document (pure — no filesystem, no sudo).

    Split out of :func:`write_gateway_settings` so the env block can be unit
    tested without sudo; the write path is a thin wrapper around this.

    gateway_url:   Gateway proxy URL (ANTHROPIC_BASE_URL — for Claude Code API calls)
    admin_api_url: Admin API URL (GATEWAY_CLI_GATEWAY_URL — for api-key-helper VK issuance)

    OIDC_ISSUER_URL / OIDC_CLIENT_ID are written because ``apiKeyHelper`` runs as a
    *child of Claude Code*, so its only configuration channel is this ``env`` block —
    it does not inherit the operator's shell. api_key_helper._detect_mode requires
    BOTH vars to pick OIDC mode; with neither present it silently selects STS(IAM)
    mode, which either issues a VK against the caller's AWS IAM ARN (wrong user —
    admin-api provisions ``<session_name>@unknown``) or, with no SSO session, prints
    nothing to stdout and exits 1. Claude Code then has no credential and falls back
    to its own first-party login prompt ("Not logged in · Please run /login") — i.e.
    the redirect-to-1P symptom. Omitting these two keys was the defect;
    guides/user-guide.md §5.2 has always told manual users to set all four.
    """
    env: dict[str, str] = {
        "ANTHROPIC_BASE_URL": gateway_url,
        "GATEWAY_CLI_GATEWAY_URL": admin_api_url,
    }

    # OIDC (IDP) — the two vars api-key-helper requires to operate in OIDC mode.
    # ADMIN_API_URL is not written separately: gateway_cli_oidc.load_oidc_config_from_env
    # falls back to ``ADMIN_API_URL or GATEWAY_CLI_GATEWAY_URL``, so the key above already
    # covers that role (writing the same value under two keys drifts once only one is updated).
    if oidc_issuer_url and oidc_client_id:
        env["OIDC_ISSUER_URL"] = oidc_issuer_url.rstrip("/")
        env["OIDC_CLIENT_ID"] = oidc_client_id
        # audience is optional — needed only when admin-api's OIDC_AUDIENCE check is enabled.
        # An empty string is omitted instead: audience handling differs per IDP and can cause 401.
        if oidc_audience:
            env["OIDC_AUDIENCE"] = oidc_audience

    # Claude Code client-side OTEL (metrics + traces + code activity)
    if otel_endpoint:
        env["OTEL_EXPORTER_OTLP_ENDPOINT"] = otel_endpoint
        env["OTEL_EXPORTER_OTLP_PROTOCOL"] = "grpc"
        env["CLAUDE_CODE_ENABLE_TELEMETRY"] = "1"
        env["OTEL_METRICS_EXPORTER"] = "otlp"
        env["OTEL_TRACES_EXPORTER"] = "otlp"
        env["CLAUDE_CODE_ENHANCED_TELEMETRY_BETA"] = "1"
        if otel_auth_token:
            env["OTEL_EXPORTER_OTLP_HEADERS"] = f"Authorization=Bearer {otel_auth_token}"

    return {
        "_comment": "LLM Gateway — managed by gateway-cli",
        "env": env,
        "apiKeyHelper": api_key_helper_path,
        "statusLine": {"type": "command", "command": "statusline"},
    }


def write_gateway_settings(
    gateway_url: str,
    admin_api_url: str,
    api_key_helper_path: str,
    otel_endpoint: str | None = None,
    otel_auth_token: str | None = None,
    oidc_issuer_url: str | None = None,
    oidc_client_id: str | None = None,
    oidc_audience: str | None = None,
) -> Path:
    """Write gateway managed settings file.

    Requires root/admin — uses sudo on Linux/WSL.
    Returns the path of the written file.
    """
    settings = build_gateway_settings(
        gateway_url=gateway_url,
        admin_api_url=admin_api_url,
        api_key_helper_path=api_key_helper_path,
        otel_endpoint=otel_endpoint,
        otel_auth_token=otel_auth_token,
        oidc_issuer_url=oidc_issuer_url,
        oidc_client_id=oidc_client_id,
        oidc_audience=oidc_audience,
    )

    content = json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
    target = _managed_file()

    if sys.platform == "win32":
        _write_windows(target, content)
    else:
        _write_unix(target, content)

    log.info("managed_settings_written", path=str(target))
    return target


def remove_gateway_settings() -> bool:
    """Remove the gateway managed settings file.

    Returns True if removed, False if it didn't exist.
    """
    target = _managed_file()
    if not target.is_file():
        return False

    if sys.platform == "win32":
        _remove_windows(target)
    else:
        _remove_unix(target)

    log.info("managed_settings_removed", path=str(target))
    return True


# ---------------------------------------------------------------------------
# Platform-specific write/remove (requires elevated privileges)
# ---------------------------------------------------------------------------

def _write_unix(target: Path, content: str) -> None:
    """Write via sudo tee (no temp file needed, atomic enough)."""
    parent = target.parent
    # Ensure directory exists
    subprocess.run(
        ["sudo", "mkdir", "-p", str(parent)],
        check=True,
    )
    # Write content via sudo tee
    subprocess.run(
        ["sudo", "tee", str(target)],
        input=content.encode(),
        stdout=subprocess.DEVNULL,
        check=True,
    )
    # Set permissions: root-owned, world-readable
    subprocess.run(["sudo", "chmod", "644", str(target)], check=True)
    group = "wheel" if sys.platform == "darwin" else "root"
    subprocess.run(["sudo", "chown", f"root:{group}", str(target)], check=True)


def _remove_unix(target: Path) -> None:
    subprocess.run(["sudo", "rm", "-f", str(target)], check=True)


def _write_windows(target: Path, content: str) -> None:
    """Write directly — requires running as admin on Windows."""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _remove_windows(target: Path) -> None:
    target.unlink(missing_ok=True)
