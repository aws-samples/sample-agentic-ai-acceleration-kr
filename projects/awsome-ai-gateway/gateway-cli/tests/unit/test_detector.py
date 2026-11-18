# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Tests for cli.tools.detector — AI tool auto-detection."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from cli.config import AuthMode, ToolType
from cli.tools.detector import _check_is_configured, _claude_code_paths, detect_tools


class TestCheckIsConfigured:
    def test_all_keys_present(self) -> None:
        from cli.tools.detector import ToolDetectionRule

        rule = ToolDetectionRule(
            tool_type=ToolType.CLAUDE_CODE,
            config_paths=[],
            auth_mode=AuthMode.BEDROCK_VK,
            gateway_config_keys=["apiKeyHelper", "env"],
        )
        data = {"apiKeyHelper": "/path/to/helper", "env": {"KEY": "val"}}
        assert _check_is_configured(data, rule) is True

    def test_missing_key(self) -> None:
        from cli.tools.detector import ToolDetectionRule

        rule = ToolDetectionRule(
            tool_type=ToolType.CLAUDE_CODE,
            config_paths=[],
            auth_mode=AuthMode.BEDROCK_VK,
            gateway_config_keys=["apiKeyHelper", "env"],
        )
        data = {"apiKeyHelper": "/path"}  # missing "env"
        assert _check_is_configured(data, rule) is False

    def test_nested_dotted_key(self) -> None:
        from cli.tools.detector import ToolDetectionRule

        rule = ToolDetectionRule(
            tool_type=ToolType.CLINE,
            config_paths=[],
            auth_mode=AuthMode.JWT,
            gateway_config_keys=["cline.apiProvider"],
        )
        data = {"cline": {"apiProvider": "openai-compatible"}}
        assert _check_is_configured(data, rule) is True

    def test_nested_dotted_key_missing(self) -> None:
        from cli.tools.detector import ToolDetectionRule

        rule = ToolDetectionRule(
            tool_type=ToolType.CLINE,
            config_paths=[],
            auth_mode=AuthMode.JWT,
            gateway_config_keys=["cline.apiProvider"],
        )
        data = {"other": "value"}
        assert _check_is_configured(data, rule) is False

    def test_empty_data(self) -> None:
        from cli.tools.detector import ToolDetectionRule

        rule = ToolDetectionRule(
            tool_type=ToolType.OPENCODE,
            config_paths=[],
            auth_mode=AuthMode.JWT,
            gateway_config_keys=["provider"],
        )
        assert _check_is_configured({}, rule) is False


class TestDetectTools:
    def test_detect_claude_code(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({"someKey": "val"}), encoding="utf-8")

        with patch(
            "cli.tools.detector._claude_code_paths", return_value=[str(settings)]
        ), patch("cli.tools.detector._opencode_paths", return_value=[]), patch(
            "cli.tools.detector._cline_paths", return_value=[]
        ):
            tools = detect_tools()

        assert len(tools) == 1
        assert tools[0].tool_type == ToolType.CLAUDE_CODE
        assert tools[0].auth_mode == AuthMode.BEDROCK_VK
        assert tools[0].is_configured is False

    def test_detect_configured_tool(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text(
            json.dumps({"apiKeyHelper": "/path", "env": {"KEY": "val"}}),
            encoding="utf-8",
        )

        with patch(
            "cli.tools.detector._claude_code_paths", return_value=[str(settings)]
        ), patch("cli.tools.detector._opencode_paths", return_value=[]), patch(
            "cli.tools.detector._cline_paths", return_value=[]
        ):
            tools = detect_tools()

        assert tools[0].is_configured is True

    def test_detect_no_tools(self) -> None:
        with patch(
            "cli.tools.detector._claude_code_paths", return_value=[]
        ), patch("cli.tools.detector._opencode_paths", return_value=[]), patch(
            "cli.tools.detector._cline_paths", return_value=[]
        ):
            tools = detect_tools()
        assert tools == []

    def test_detect_multiple_tools(self, tmp_path: Path) -> None:
        claude_settings = tmp_path / "claude_settings.json"
        claude_settings.write_text("{}", encoding="utf-8")
        opencode_config = tmp_path / "opencode_config.json"
        opencode_config.write_text("{}", encoding="utf-8")

        with patch(
            "cli.tools.detector._claude_code_paths",
            return_value=[str(claude_settings)],
        ), patch(
            "cli.tools.detector._opencode_paths",
            return_value=[str(opencode_config)],
        ), patch("cli.tools.detector._cline_paths", return_value=[]):
            tools = detect_tools()

        assert len(tools) == 2
        types = {t.tool_type for t in tools}
        assert ToolType.CLAUDE_CODE in types
        assert ToolType.OPENCODE in types

    def test_nonexistent_path_skipped(self) -> None:
        with patch(
            "cli.tools.detector._claude_code_paths",
            return_value=["/no/such/path.json"],
        ), patch("cli.tools.detector._opencode_paths", return_value=[]), patch(
            "cli.tools.detector._cline_paths", return_value=[]
        ):
            tools = detect_tools()
        assert tools == []


class TestClaudeCodePaths:
    """Claude Code 사용자 설정 파일 위치 — https://code.claude.com/docs/en/settings.md 기준.

    문서: 사용자 설정은 `~/.claude/settings.json` 이고 "On Windows, `~/.claude` means
    `%USERPROFILE%\\.claude`". 예전 구현은 Windows 에서만 `%APPDATA%\\Claude\\settings.json`
    을 봤는데, 문서에 없는 위치다.
    """

    @pytest.mark.parametrize("platform", ["darwin", "linux"])
    def test_posix_reads_home_dot_claude(self, platform, monkeypatch, isolated_home) -> None:
        monkeypatch.setattr(sys, "platform", platform)
        expected = os.path.join(str(isolated_home), ".claude", "settings.json")
        assert _claude_code_paths() == [expected]

    def test_windows_reads_userprofile_dot_claude(self, monkeypatch, isolated_home) -> None:
        # 실제 Windows 처럼 APPDATA 를 USERPROFILE 아래 별도 경로로 둔다(conftest 는 같은 값).
        monkeypatch.setenv("APPDATA", str(isolated_home / "AppData" / "Roaming"))
        monkeypatch.setattr(sys, "platform", "win32")
        # Windows 의 os.path.expanduser("~") 는 USERPROFILE 을 쓴다. conftest 가 HOME 과
        # USERPROFILE 을 같은 tmp 홈으로 맞춰 두었으므로 이 POSIX 호스트에서도 같은 값이다.
        expected = os.path.join(os.environ["USERPROFILE"], ".claude", "settings.json")
        assert _claude_code_paths() == [expected]
