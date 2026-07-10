# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Tests for cli.tools.detector — AI tool auto-detection."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from cli.config import AuthMode, ToolType
from cli.tools.detector import _check_is_configured, detect_tools


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
