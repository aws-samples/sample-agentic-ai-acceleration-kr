# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Bedrock + apiKeyHelper configuration for Claude Code (LP-03, US-01)."""

from __future__ import annotations

import os
import sys

import structlog

from cli.config import (
    ComponentResult,
    ComponentStatus,
    DetectedTool,
    GatewayConfig,
    SetupComponentType,
)
from cli.utils.config_rw import atomic_write_json, read_json

log = structlog.get_logger(component="cli")


def resolve_helper_path() -> str:
    """Resolve the api-key-helper binary path.

    Looks for binary next to gateway-cli executable, or in PATH.
    """
    if getattr(sys, "frozen", False):
        # PyInstaller bundle — binary is next to this exe
        exe_dir = os.path.dirname(sys.executable)
    else:
        exe_dir = os.path.dirname(os.path.abspath(__file__))

    name = "api-key-helper.exe" if sys.platform == "win32" else "api-key-helper"
    candidate = os.path.join(exe_dir, name)
    if os.path.isfile(candidate):
        return candidate

    # Fallback: assume it's in PATH
    return name


def apply_bedrock_config(original: dict, gateway_url: str, helper_path: str) -> dict:
    """Merge Bedrock settings into Claude Code settings.json (LP-03).

    Only touches gateway-related keys; preserves all other keys.
    """
    updated = dict(original)
    updated["apiKeyHelper"] = helper_path
    updated.setdefault("env", {})
    updated["env"]["ANTHROPIC_BASE_URL"] = gateway_url
    return updated


def setup_bedrock(tool: DetectedTool, config: GatewayConfig) -> ComponentResult:
    """Set up apiKeyHelper + Bedrock endpoint for Claude Code."""
    try:
        original = read_json(tool.config_path)
    except ValueError as exc:
        return ComponentResult(
            component=SetupComponentType.API_KEY_HELPER,
            status=ComponentStatus.FAILED,
            message="Config file parse error",
            error=str(exc),
        )

    helper_path = resolve_helper_path()
    updated = apply_bedrock_config(original, config.gateway_url, helper_path)
    atomic_write_json(tool.config_path, updated)

    log.info(
        "bedrock_configured",
        tool=tool.name,
        helper_path=helper_path,
        gateway_url=config.gateway_url,
    )
    return ComponentResult(
        component=SetupComponentType.API_KEY_HELPER,
        status=ComponentStatus.SUCCESS,
        message=f"apiKeyHelper configured: {helper_path}",
    )
