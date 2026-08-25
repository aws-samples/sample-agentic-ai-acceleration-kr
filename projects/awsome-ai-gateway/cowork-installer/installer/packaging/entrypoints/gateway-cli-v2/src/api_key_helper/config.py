# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""GatewayConfig loader for api-key-helper (LP-04 — independent copy, LP-01)."""

from __future__ import annotations

import os
from dataclasses import dataclass

import yaml
from platformdirs import user_config_dir

# Cowork data-dir (side-by-side row 3.1): match the renamed cowork app-name so
# the helper's config.yaml is isolated from Claude Code's gateway-cli config dir.
_DEFAULT_CONFIG_DIR = user_config_dir("gateway-cli-cowork")
DEFAULT_CONFIG_PATH = os.path.join(_DEFAULT_CONFIG_DIR, "config.yaml")

ENV_MAP = {
    "GATEWAY_CLI_GATEWAY_URL": "gateway_url",
    "GATEWAY_CLI_VERBOSE": "verbose",
}


@dataclass
class HelperConfig:
    gateway_url: str = ""
    connect_timeout: int = 5
    read_timeout: int = 10
    verbose: bool = False
    config_path: str = ""


def load_config(
    config_path: str | None = None,
    cli_overrides: dict | None = None,
) -> HelperConfig:
    """Load config: YAML > env > CLI (LP-01)."""
    path = config_path or os.environ.get("GATEWAY_CLI_CONFIG", DEFAULT_CONFIG_PATH)
    raw: dict = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

    for env_key, config_key in ENV_MAP.items():
        val = os.environ.get(env_key)
        if val is not None:
            if config_key == "verbose":
                raw[config_key] = val.lower() in ("1", "true", "yes")
            else:
                raw[config_key] = val

    if cli_overrides:
        for key, val in cli_overrides.items():
            if val is not None:
                raw[key] = val

    return HelperConfig(
        gateway_url=raw.get("gateway_url", ""),
        connect_timeout=int(raw.get("connect_timeout", 5)),
        read_timeout=int(raw.get("read_timeout", 10)),
        verbose=bool(raw.get("verbose", False)),
        config_path=str(path),
    )


# OIDC / admin-api identifiers the helper needs to refresh a Virtual Key. On a
# Start-menu MSIX launch (Cowork/Claude Desktop) the app process does NOT inherit
# ``HKCU\Environment``, so these env vars are absent and an OIDC-mode VK refresh
# would fail. When the CLI was built with baked corporate defaults
# (``cli._site_config`` via ``cli.site_defaults``), fall back to those so the
# helper works in that clean environment. Env vars, when present, always win.
_BAKED_ENV_FALLBACKS = {
    "OIDC_ISSUER_URL": "DEFAULT_OIDC_ISSUER_URL",
    "OIDC_CLIENT_ID": "DEFAULT_OIDC_CLIENT_ID",
    "ADMIN_API_URL": "DEFAULT_ADMIN_API_URL",
}


def apply_baked_env_fallback() -> None:
    """Seed missing OIDC/admin env vars from the build-time baked defaults.

    Only fills a variable that is unset or blank, so an explicitly-exported env
    var (e.g. a staging override) is never clobbered. A no-op in a dev build with
    no baked ``cli._site_config`` (the defaults resolve to blank and are skipped).
    """
    try:
        from cli import site_defaults
    except ImportError:  # pragma: no cover - cli always ships alongside the helper
        return
    for env_key, attr in _BAKED_ENV_FALLBACKS.items():
        if (os.environ.get(env_key) or "").strip():
            continue
        baked = (getattr(site_defaults, attr, "") or "").strip()
        if baked:
            os.environ[env_key] = baked
