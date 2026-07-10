# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""GatewayConfig loader for api-key-helper (LP-04 — independent copy, LP-01)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from platformdirs import user_config_dir

_DEFAULT_CONFIG_DIR = user_config_dir("gateway-cli")
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
