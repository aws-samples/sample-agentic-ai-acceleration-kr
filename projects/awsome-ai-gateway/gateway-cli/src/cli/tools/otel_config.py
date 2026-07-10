# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""OTel endpoint configuration for AI tools (US-04c, BR-OTEL-01~04)."""

from __future__ import annotations

import requests as req_lib
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


# ---------------------------------------------------------------------------
# Per-tool OTel config apply (LP-03)
# ---------------------------------------------------------------------------

def _apply_claude_code_otel(original: dict, config: GatewayConfig) -> dict:
    """Set OTel env vars in Claude Code settings.json."""
    updated = dict(original)
    updated.setdefault("env", {})
    updated["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] = config.otel_endpoint
    if config.otel_auth_token:
        updated["env"]["OTEL_EXPORTER_OTLP_HEADERS"] = (
            f"Authorization=Bearer {config.otel_auth_token}"
        )
    return updated


def _apply_opencode_otel(original: dict, config: GatewayConfig) -> dict:
    """Set OTel config in OpenCode config.json."""
    updated = dict(original)
    updated.setdefault("telemetry", {})
    updated["telemetry"]["endpoint"] = config.otel_endpoint
    if config.otel_auth_token:
        updated["telemetry"]["auth_token"] = config.otel_auth_token
    return updated


def _apply_cline_otel(original: dict, config: GatewayConfig) -> dict:
    """Set OTel config in Cline (VS Code settings)."""
    updated = dict(original)
    updated["cline.telemetryEndpoint"] = config.otel_endpoint
    if config.otel_auth_token:
        updated["cline.telemetryAuthToken"] = config.otel_auth_token
    return updated


_OTEL_APPLY_FUNCS = {
    ToolType.CLAUDE_CODE: _apply_claude_code_otel,
    ToolType.OPENCODE: _apply_opencode_otel,
    ToolType.CLINE: _apply_cline_otel,
}


# ---------------------------------------------------------------------------
# OTel connection validation (BR-OTEL-04)
# ---------------------------------------------------------------------------

def _validate_otel_connection(config: GatewayConfig) -> bool:
    """Test OTel endpoint connectivity."""
    if not config.otel_endpoint:
        return False
    try:
        headers = {}
        if config.otel_auth_token:
            headers["Authorization"] = f"Bearer {config.otel_auth_token}"
        resp = req_lib.get(
            config.otel_endpoint,
            headers=headers,
            timeout=(config.connect_timeout, config.read_timeout),
        )
        return resp.status_code < 500
    except req_lib.RequestException:
        return False


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def setup_otel(tool: DetectedTool, config: GatewayConfig) -> ComponentResult:
    """Set up OTel endpoint for a detected AI tool (US-04c)."""
    if not config.otel_endpoint:
        return ComponentResult(
            component=SetupComponentType.OTEL,
            status=ComponentStatus.SKIPPED,
            message="OTel endpoint not configured in config.yaml",
        )

    apply_fn = _OTEL_APPLY_FUNCS.get(tool.tool_type)
    if not apply_fn:
        return ComponentResult(
            component=SetupComponentType.OTEL,
            status=ComponentStatus.SKIPPED,
            message=f"OTel not supported for {tool.name}",
        )

    try:
        original = read_json(tool.config_path)
    except ValueError as exc:
        return ComponentResult(
            component=SetupComponentType.OTEL,
            status=ComponentStatus.FAILED,
            message="Config file parse error",
            error=str(exc),
        )

    updated = apply_fn(original, config)
    atomic_write_json(tool.config_path, updated)

    # Validate connection (BR-OTEL-04)
    connected = _validate_otel_connection(config)
    if not connected:
        log.warning("otel_connection_failed", endpoint=config.otel_endpoint, tool=tool.name)
        return ComponentResult(
            component=SetupComponentType.OTEL,
            status=ComponentStatus.FAILED,
            message="OTel config saved but collector unreachable",
            error=f"Cannot connect to {config.otel_endpoint}",
        )

    log.info("otel_configured", tool=tool.name, endpoint=config.otel_endpoint)
    return ComponentResult(
        component=SetupComponentType.OTEL,
        status=ComponentStatus.SUCCESS,
        message=f"OTel endpoint configured: {config.otel_endpoint}",
    )
