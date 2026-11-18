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
    # Claude Code 의 사용자 설정은 모든 OS 에서 `~/.claude/settings.json` 이고, Windows 에서
    # `~` 는 %USERPROFILE% 이다(https://code.claude.com/docs/en/settings.md). Windows 의
    # os.path.expanduser("~") 가 바로 USERPROFILE 이라 OS 분기가 필요 없다 — 예전 Windows
    # 분기가 보던 %APPDATA%\Claude\settings.json 은 문서에 없는 위치였다.
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

# ⚠️ 값이 함수 객체가 아니라 람다인 것은 의도적이다. 함수 객체를 그대로 담으면
# 이 dict 가 import 시점의 객체를 붙잡아버려서, 테스트가
# `patch("cli.tools.detector._claude_code_paths", ...)` 로 모듈 속성을 바꿔도
# detect_tools 는 여전히 원본을 호출한다 — 패치가 조용한 no-op 이 되고 테스트는
# 개발자 머신의 실제 `~/.claude/settings.json` 을 읽는다(실제로 4개 테스트가
# 그렇게 깨져 있었고, 1개는 그 파일이 마침 존재해서 '우연히' 통과했다).
# 람다로 감싸면 호출 시점에 모듈 전역을 다시 찾으므로 패치가 정상 동작한다.
# 런타임 동작은 완전히 동일하다.
_PATH_RESOLVERS: dict[ToolType, callable] = {
    ToolType.CLAUDE_CODE: lambda: _claude_code_paths(),
    ToolType.OPENCODE: lambda: _opencode_paths(),
    ToolType.CLINE: lambda: _cline_paths(),
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
