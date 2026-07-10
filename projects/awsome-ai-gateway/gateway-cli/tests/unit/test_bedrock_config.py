# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Tests for cli.tools.bedrock_config — Bedrock + apiKeyHelper config."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from cli.config import (
    AuthMode,
    ComponentStatus,
    DetectedTool,
    GatewayConfig,
    ToolType,
)
from cli.tools.bedrock_config import apply_bedrock_config, setup_bedrock


class TestApplyBedrockConfig:
    def test_sets_helper_and_base_url(self) -> None:
        original = {"someExisting": "value"}
        result = apply_bedrock_config(original, "https://gw.example.com", "/usr/bin/helper")
        assert result["apiKeyHelper"] == "/usr/bin/helper"
        assert result["env"]["ANTHROPIC_BASE_URL"] == "https://gw.example.com"

    def test_preserves_existing_keys(self) -> None:
        original = {"theme": "dark", "env": {"OTHER_VAR": "keep"}}
        result = apply_bedrock_config(original, "https://gw.com", "/helper")
        assert result["theme"] == "dark"
        assert result["env"]["OTHER_VAR"] == "keep"
        assert result["env"]["ANTHROPIC_BASE_URL"] == "https://gw.com"

    def test_overwrites_existing_gateway_keys(self) -> None:
        original = {"apiKeyHelper": "/old/path", "env": {"ANTHROPIC_BASE_URL": "old"}}
        result = apply_bedrock_config(original, "https://new.com", "/new/helper")
        assert result["apiKeyHelper"] == "/new/helper"
        assert result["env"]["ANTHROPIC_BASE_URL"] == "https://new.com"

    def test_does_not_mutate_original(self) -> None:
        original = {"key": "val"}
        apply_bedrock_config(original, "https://gw.com", "/helper")
        assert "apiKeyHelper" not in original


class TestSetupBedrock:
    def test_success(self, tmp_path: Path) -> None:
        config_path = tmp_path / "settings.json"
        config_path.write_text("{}", encoding="utf-8")

        tool = DetectedTool(
            tool_type=ToolType.CLAUDE_CODE,
            name="Claude Code",
            config_path=str(config_path),
            auth_mode=AuthMode.BEDROCK_VK,
        )
        config = GatewayConfig(gateway_url="https://gw.example.com")

        with patch("cli.tools.bedrock_config._resolve_helper_path", return_value="/path/helper"):
            result = setup_bedrock(tool, config)

        assert result.status == ComponentStatus.SUCCESS
        data = json.loads(config_path.read_text(encoding="utf-8"))
        assert data["apiKeyHelper"] == "/path/helper"
        assert data["env"]["ANTHROPIC_BASE_URL"] == "https://gw.example.com"

    def test_invalid_json_fails(self, tmp_path: Path) -> None:
        config_path = tmp_path / "settings.json"
        config_path.write_text("{bad json", encoding="utf-8")

        tool = DetectedTool(
            tool_type=ToolType.CLAUDE_CODE,
            name="Claude Code",
            config_path=str(config_path),
            auth_mode=AuthMode.BEDROCK_VK,
        )
        config = GatewayConfig(gateway_url="https://gw.example.com")
        result = setup_bedrock(tool, config)
        assert result.status == ComponentStatus.FAILED
        assert "parse error" in result.message.lower()
