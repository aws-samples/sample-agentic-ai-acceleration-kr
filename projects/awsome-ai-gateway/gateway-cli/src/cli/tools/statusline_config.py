# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Claude Code statusline configuration (US-04a)."""

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
    ToolType,
)
from cli.utils.config_rw import atomic_write_json, read_json

log = structlog.get_logger(component="cli")


def _resolve_statusline_path() -> str:
    """Resolve the statusline binary path."""
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
    else:
        exe_dir = os.path.dirname(os.path.abspath(__file__))

    name = "statusline.exe" if sys.platform == "win32" else "statusline"
    candidate = os.path.join(exe_dir, name)
    if os.path.isfile(candidate):
        return candidate
    return name


def apply_statusline_config(
    original: dict, statusline_path: str, gateway_url: str
) -> dict:
    """Merge statusline settings into Claude Code settings.json (LP-03)."""
    updated = dict(original)
    updated["statusline"] = statusline_path
    updated.setdefault("env", {})
    updated["env"]["GATEWAY_CLI_GATEWAY_URL"] = gateway_url
    return updated


def setup_statusline(tool: DetectedTool, config: GatewayConfig) -> ComponentResult:
    """Set up statusline for Claude Code (US-04a).

    Only applies to Claude Code — other tools return SKIPPED.
    """
    if tool.tool_type != ToolType.CLAUDE_CODE:
        return ComponentResult(
            component=SetupComponentType.STATUSLINE,
            status=ComponentStatus.SKIPPED,
            message=f"Statusline not applicable for {tool.name}",
        )

    try:
        original = read_json(tool.config_path)
    except ValueError as exc:
        return ComponentResult(
            component=SetupComponentType.STATUSLINE,
            status=ComponentStatus.FAILED,
            message="Config file parse error",
            error=str(exc),
        )

    statusline_path = _resolve_statusline_path()
    updated = apply_statusline_config(original, statusline_path, config.gateway_url)
    atomic_write_json(tool.config_path, updated)

    log.info("statusline_configured", tool=tool.name, statusline_path=statusline_path)
    return ComponentResult(
        component=SetupComponentType.STATUSLINE,
        status=ComponentStatus.SUCCESS,
        message=f"Statusline configured: {statusline_path}",
    )
