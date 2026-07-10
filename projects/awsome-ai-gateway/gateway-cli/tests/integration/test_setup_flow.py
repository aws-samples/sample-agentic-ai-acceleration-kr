# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Integration tests for gateway-cli setup full flow (US-04b)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from cli.config import AuthMode, DetectedTool, ToolType
from cli.main import cli


def _make_tool(tmp_path: Path, tool_type: ToolType, auth_mode: AuthMode) -> DetectedTool:
    """Helper to create a DetectedTool with a real config file."""
    config_path = tmp_path / f"{tool_type.value}-settings.json"
    config_path.write_text("{}", encoding="utf-8")
    names = {
        ToolType.CLAUDE_CODE: "Claude Code",
        ToolType.OPENCODE: "OpenCode",
        ToolType.CLINE: "Cline",
    }
    return DetectedTool(
        tool_type=tool_type,
        name=names[tool_type],
        config_path=str(config_path),
        auth_mode=auth_mode,
    )


class TestSetupFlowClaudeCode:
    """Test setup flow for Claude Code (Bedrock VK path)."""

    def test_setup_claude_code_bedrock(self, tmp_path: Path) -> None:
        tool = _make_tool(tmp_path, ToolType.CLAUDE_CODE, AuthMode.BEDROCK_VK)

        with patch("cli.setup.detect_tools", return_value=[tool]), patch(
            "cli.setup.backup_config", return_value=None
        ), patch(
            "cli.tools.bedrock_config._resolve_helper_path",
            return_value="/usr/local/bin/api-key-helper",
        ), patch(
            "cli.tools.otel_config._validate_otel_connection", return_value=True
        ):
            runner = CliRunner()
            result = runner.invoke(
                cli,
                ["setup", "--gateway-url", "https://gw.example.com"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        assert "Claude Code" in result.output
        assert "Setup Summary" in result.output

        # Verify config file was written
        data = json.loads(Path(tool.config_path).read_text(encoding="utf-8"))
        assert data["apiKeyHelper"] == "/usr/local/bin/api-key-helper"
        assert data["env"]["ANTHROPIC_BASE_URL"] == "https://gw.example.com"

    def test_setup_with_otel(self, tmp_path: Path) -> None:
        tool = _make_tool(tmp_path, ToolType.CLAUDE_CODE, AuthMode.BEDROCK_VK)

        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            "gateway_url: https://gw.example.com\n"
            "otel_endpoint: https://otel.example.com\n",
            encoding="utf-8",
        )

        with patch("cli.setup.detect_tools", return_value=[tool]), patch(
            "cli.setup.backup_config", return_value=None
        ), patch(
            "cli.tools.bedrock_config._resolve_helper_path",
            return_value="/bin/api-key-helper",
        ), patch(
            "cli.tools.otel_config._validate_otel_connection", return_value=True
        ), patch("cli.config.DEFAULT_CONFIG_PATH", str(cfg_file)):
            runner = CliRunner()
            result = runner.invoke(
                cli,
                ["setup"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        data = json.loads(Path(tool.config_path).read_text(encoding="utf-8"))
        assert data["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == "https://otel.example.com"


class TestSetupFlowMultipleTools:
    """Test setup flow with multiple detected tools."""

    def test_setup_claude_and_opencode(self, tmp_path: Path) -> None:
        claude = _make_tool(tmp_path, ToolType.CLAUDE_CODE, AuthMode.BEDROCK_VK)
        opencode = _make_tool(tmp_path, ToolType.OPENCODE, AuthMode.JWT)

        with patch("cli.setup.detect_tools", return_value=[claude, opencode]), patch(
            "cli.setup.backup_config", return_value=None
        ), patch(
            "cli.tools.bedrock_config._resolve_helper_path",
            return_value="/bin/api-key-helper",
        ):
            runner = CliRunner()
            result = runner.invoke(
                cli,
                ["setup", "--gateway-url", "https://gw.example.com"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        assert "Claude Code" in result.output
        assert "OpenCode" in result.output
        # Claude Code should have bedrock config
        claude_data = json.loads(Path(claude.config_path).read_text(encoding="utf-8"))
        assert "apiKeyHelper" in claude_data


class TestSetupSummaryOutput:
    """Test summary output formatting."""

    def test_summary_shows_counts(self, tmp_path: Path) -> None:
        tool = _make_tool(tmp_path, ToolType.CLAUDE_CODE, AuthMode.BEDROCK_VK)

        with patch("cli.setup.detect_tools", return_value=[tool]), patch(
            "cli.setup.backup_config", return_value=None
        ), patch(
            "cli.tools.bedrock_config._resolve_helper_path",
            return_value="/bin/helper",
        ):
            runner = CliRunner()
            result = runner.invoke(
                cli,
                ["setup", "--gateway-url", "https://gw.example.com"],
                catch_exceptions=False,
            )

        assert "Total:" in result.output
        assert "Success:" in result.output
