# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""GatewayConfig loader for statusline (LP-04 — independent copy, LP-01)."""

from __future__ import annotations

import os
from dataclasses import dataclass

import yaml
from platformdirs import user_config_dir

_DEFAULT_CONFIG_DIR = user_config_dir("gateway-cli")
DEFAULT_CONFIG_PATH = os.path.join(_DEFAULT_CONFIG_DIR, "config.yaml")

ENV_MAP = {
    "ANTHROPIC_BASE_URL": "gateway_url",  # /v1/usage/me is on gateway-proxy, not admin-api
    "GATEWAY_CLI_VERBOSE": "verbose",
}


@dataclass
class StatuslineConfig:
    gateway_url: str = ""
    interval: int = 30
    connect_timeout: int = 5
    read_timeout: int = 10
    verbose: bool = False
    usage_endpoint: str = "/v1/usage/me"


def load_config(
    config_path: str | None = None,
    cli_overrides: dict | None = None,
) -> StatuslineConfig:
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

    # Flatten nested statusline section
    sl = raw.get("statusline", {})
    if isinstance(sl, dict):
        interval = sl.get("interval_seconds", 30)
        usage_ep = sl.get("usage_endpoint", "/v1/usage/me")
    else:
        interval = 30
        usage_ep = "/v1/usage/me"

    return StatuslineConfig(
        gateway_url=raw.get("gateway_url", ""),
        interval=int(raw.get("interval", interval)),
        connect_timeout=int(raw.get("connect_timeout", 5)),
        read_timeout=int(raw.get("read_timeout", 10)),
        verbose=bool(raw.get("verbose", False)),
        usage_endpoint=usage_ep,
    )
