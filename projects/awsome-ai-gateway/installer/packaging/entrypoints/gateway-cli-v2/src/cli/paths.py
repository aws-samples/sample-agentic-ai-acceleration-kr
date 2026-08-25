# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""OS-specific data directory paths for gateway-cli-v2.

All runtime files (token cache, VK cache, config) live in the platform-native
user-data directory so they end up in the right place on every OS:

  macOS   ~/Library/Application Support/gateway-cli/
  Linux   ~/.local/share/gateway-cli/
  Windows C:\\Users\\<user>\\AppData\\Local\\gateway-cli\\

Individual paths can be overridden by environment variables (useful for tests
and for pointing at the legacy ~/.gateway-cli/ location when migrating).
"""

from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_data_dir

_APP_NAME = "gateway-cli"
_APP_AUTHOR = False  # do not create a vendor subdirectory on Windows


def data_dir() -> Path:
    """Return the platform-native user data directory for gateway-cli."""
    override = os.environ.get("GATEWAY_CLI_DATA_DIR")
    if override:
        return Path(override)
    return Path(user_data_dir(_APP_NAME, appauthor=_APP_AUTHOR))


def oidc_tokens_path() -> Path:
    """Path to the cached OIDC tokens file."""
    override = os.environ.get("GATEWAY_CLI_OIDC_CACHE")
    if override:
        return Path(override)
    return data_dir() / "oidc-tokens.json"


def vk_cache_path() -> Path:
    """Path to the cached Virtual Key file."""
    override = os.environ.get("GATEWAY_CLI_VK_CACHE")
    if override:
        return Path(override)
    return data_dir() / "vk-cache.json"
