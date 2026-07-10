# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""AI tool auto-detection (US-03, BR-DET-01~04)."""

from __future__ import annotations

import glob as glob_mod
import os
import sys
from pathlib import Path

import structlog

from cli.config import AuthMode, DetectedTool, ToolDetectionRule, ToolType
from cli.utils.config_rw import read_json

log = structlog.get_logger(component="cli")

# ---------------------------------------------------------------------------
# Detection rules — 3 tools (BR-DET-01, BR-DET-03)
# ---------------------------------------------------------------------------

def _home() -> str:
    return os.path.expanduser("~")


def _claude_code_paths() -> list[str]:
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        return [os.path.join(appdata, "Claude", "settings.json")] if appdata else []
    return [os.path.join(_home(), ".claude", "settings.json")]


def _opencode_paths() -> list[str]:
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        return [os.path.join(appdata, "opencode", "config.json")] if appdata else []
    return [os.path.join(_home(), ".config", "opencode", "config.json")]


def _cline_paths() -> list[str]:
    """Cline uses VS Code extensions directory with versioned folder (glob)."""
    if sys.platform == "win32":
        base = os.path.join(
            os.environ.get("USERPROFILE", _home()),
            ".vscode", "extensions",
        )
    else:
        base = os.path.join(_home(), ".vscode", "extensions")
    pattern = os.path.join(base, "saoudrizwan.claude-dev-*", "config.json")
    return glob_mod.glob(pattern)


DETECTION_RULES: list[ToolDetectionRule] = [
    ToolDetectionRule(
        tool_type=ToolType.CLAUDE_CODE,
        config_paths=[],  # filled dynamically
        auth_mode=AuthMode.BEDROCK_VK,
        gateway_config_keys=["apiKeyHelper", "env"],
    ),
    ToolDetectionRule(
        tool_type=ToolType.OPENCODE,
        config_paths=[],
        auth_mode=AuthMode.JWT,
        gateway_config_keys=["provider"],
    ),
    ToolDetectionRule(
        tool_type=ToolType.CLINE,
        config_paths=[],
        auth_mode=AuthMode.JWT,
        gateway_config_keys=["cline.apiProvider", "cline.openaiBaseUrl"],
    ),
]

_PATH_RESOLVERS: dict[ToolType, callable] = {
    ToolType.CLAUDE_CODE: _claude_code_paths,
    ToolType.OPENCODE: _opencode_paths,
    ToolType.CLINE: _cline_paths,
}

_DISPLAY_NAMES: dict[ToolType, str] = {
    ToolType.CLAUDE_CODE: "Claude Code",
    ToolType.OPENCODE: "OpenCode",
    ToolType.CLINE: "Cline",
}


# ---------------------------------------------------------------------------
# Detection logic
# ---------------------------------------------------------------------------

def _check_is_configured(config_data: dict, rule: ToolDetectionRule) -> bool:
    """Check if all gateway config keys exist in tool config (BR-DET-04)."""
    for key in rule.gateway_config_keys:
        parts = key.split(".")
        obj = config_data
        for part in parts:
            if not isinstance(obj, dict) or part not in obj:
                return False
            obj = obj[part]
    return True


def detect_tools() -> list[DetectedTool]:
    """Scan for installed AI tools and return detected list (US-03).

    Returns empty list (not an error) if no tools found (BR-DET-02).
    """
    detected: list[DetectedTool] = []

    for rule in DETECTION_RULES:
        resolver = _PATH_RESOLVERS.get(rule.tool_type)
        if not resolver:
            continue

        paths = resolver()
        for config_path in paths:
            if not os.path.isfile(config_path):
                continue

            try:
                config_data = read_json(config_path)
            except ValueError:
                config_data = {}

            is_configured = _check_is_configured(config_data, rule)

            tool = DetectedTool(
                tool_type=rule.tool_type,
                name=_DISPLAY_NAMES.get(rule.tool_type, rule.tool_type.value),
                config_path=config_path,
                auth_mode=rule.auth_mode,
                is_configured=is_configured,
            )
            detected.append(tool)
            log.info(
                "tool_detected",
                tool=tool.name,
                config_path=config_path,
                auth_mode=rule.auth_mode.value,
                is_configured=is_configured,
            )
            break  # one match per rule is enough

    if not detected:
        log.info("no_tools_detected")

    return detected
