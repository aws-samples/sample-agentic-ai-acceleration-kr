# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Domain entities and configuration loader for Gateway CLI (LP-01)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from datetime import datetime
from pathlib import Path
from typing import Optional

from platformdirs import user_config_dir

from cli.utils.config_rw import read_yaml


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ToolType(str, Enum):
    CLAUDE_CODE = "claude-code"
    OPENCODE = "opencode"
    CLINE = "cline"


class AuthMode(str, Enum):
    BEDROCK_VK = "bedrock_vk"
    JWT = "jwt"


class SetupComponentType(str, Enum):
    API_KEY_HELPER = "api-key-helper"
    STATUSLINE = "statusline"
    OTEL = "otel"
    JWT_AUTH = "jwt-auth"


class ComponentStatus(str, Enum):
    SUCCESS = "success"
    SKIPPED = "skipped"
    FAILED = "failed"


class StatuslineSeverity(str, Enum):
    NORMAL = "normal"
    WARNING = "warning"
    CRITICAL = "critical"
    OFFLINE = "offline"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class DetectedTool:
    tool_type: ToolType
    name: str
    config_path: str
    auth_mode: AuthMode
    is_configured: bool = False
    version: Optional[str] = None


@dataclass
class ToolDetectionRule:
    tool_type: ToolType
    config_paths: list[str]
    auth_mode: AuthMode
    gateway_config_keys: list[str]


@dataclass
class GatewayConfig:
    gateway_url: str = ""
    otel_endpoint: str = ""
    otel_auth_token: str = ""
    statusline_interval: int = 30
    config_path: str = ""
    jwt_auth: dict = field(default_factory=dict)
    connect_timeout: int = 5
    read_timeout: int = 10
    lang: str = "en"
    verbose: bool = False


@dataclass
class ComponentResult:
    component: SetupComponentType
    status: ComponentStatus
    message: str
    error: Optional[str] = None


@dataclass
class SetupResult:
    tool: DetectedTool
    components: list[ComponentResult] = field(default_factory=list)
    backup_path: Optional[str] = None


@dataclass
class VirtualKeyResponse:
    virtual_key: str
    expires_at: datetime
    gateway_endpoint: str = ""
    otel_endpoint: str = ""
    user_id: str = ""
    team_id: str = ""
    max_budget_usd: Decimal = Decimal("0")
    used_usd: Decimal = Decimal("0")
    tpm_limit: int = 0
    rpm_limit: int = 0


@dataclass
class UsageInfo:
    used: Decimal = Decimal("0")
    limit: Decimal = Decimal("0")
    remaining: Decimal = Decimal("0")
    percentage: float = 0.0
    period: str = ""
    fetched_at: Optional[datetime] = None


@dataclass
class StatuslineState:
    current: Optional[UsageInfo] = None
    severity: StatuslineSeverity = StatuslineSeverity.OFFLINE
    is_online: bool = False
    last_success_at: Optional[datetime] = None
    error_count: int = 0


@dataclass
class BackupEntry:
    original_path: str
    backup_path: str
    created_at: datetime
    tool_type: ToolType


@dataclass
class OAuthSession:
    authorization_url: str = ""
    redirect_uri: str = ""
    state: str = ""
    jwt_token: Optional[str] = None
    expires_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Config loader — Hierarchical merge: YAML > env > CLI (LP-01)
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG_DIR = user_config_dir("gateway-cli")
DEFAULT_CONFIG_PATH = os.path.join(_DEFAULT_CONFIG_DIR, "config.yaml")

ENV_MAP = {
    "GATEWAY_CLI_GATEWAY_URL": "gateway_url",
    "GATEWAY_CLI_OTEL_ENDPOINT": "otel_endpoint",
    "GATEWAY_CLI_LANG": "lang",
    "GATEWAY_CLI_VERBOSE": "verbose",
}


def load_config(
    config_path: str | None = None,
    cli_overrides: dict | None = None,
) -> GatewayConfig:
    """Load GatewayConfig with hierarchical merge (LP-01).

    Priority (low → high): config.yaml → environment variables → CLI options.
    """
    # [1] Resolve config file path
    path = config_path or os.environ.get("GATEWAY_CLI_CONFIG", DEFAULT_CONFIG_PATH)
    raw = read_yaml(path)

    # [2] Environment variable overrides
    for env_key, config_key in ENV_MAP.items():
        val = os.environ.get(env_key)
        if val is not None:
            if config_key == "verbose":
                raw[config_key] = val.lower() in ("1", "true", "yes")
            else:
                raw[config_key] = val

    # [3] CLI option overrides (None values skipped)
    if cli_overrides:
        for key, val in cli_overrides.items():
            if val is not None:
                raw[key] = val

    # Flatten nested statusline config
    sl = raw.pop("statusline", {})
    if isinstance(sl, dict):
        raw.setdefault("statusline_interval", sl.get("interval_seconds", 30))

    # Build GatewayConfig
    return GatewayConfig(
        gateway_url=raw.get("gateway_url", ""),
        otel_endpoint=raw.get("otel_endpoint", ""),
        otel_auth_token=raw.get("otel_auth_token", ""),
        statusline_interval=int(raw.get("statusline_interval", 30)),
        config_path=str(path),
        jwt_auth=raw.get("jwt_auth", {}),
        connect_timeout=int(raw.get("connect_timeout", 5)),
        read_timeout=int(raw.get("read_timeout", 10)),
        lang=raw.get("lang", "en"),
        verbose=bool(raw.get("verbose", False)),
    )
