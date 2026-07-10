# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Integration tests for gateway-cli setup edge cases."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from cli.config import AuthMode, DetectedTool, ToolType
from cli.main import cli


def _make_tool(tmp_path: Path, tool_type: ToolType, auth_mode: AuthMode) -> DetectedTool:
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


class TestNoToolsDetected:
    """BR-DET-02: No tools detected scenario."""

    def test_no_tools_message(self, tmp_path: Path) -> None:
        with patch("cli.setup.detect_tools", return_value=[]):
            runner = CliRunner()
            result = runner.invoke(
                cli,
                ["setup", "--gateway-url", "https://gw.example.com"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        assert "No AI tools detected" in result.output


class TestOnlyFilter:
    """BR-SETUP-02: --only filter."""

    def test_only_api_key_helper(self, tmp_path: Path) -> None:
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
                ["setup", "--gateway-url", "https://gw.example.com",
                 "--only", "api-key-helper"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        data = json.loads(Path(tool.config_path).read_text(encoding="utf-8"))
        assert "apiKeyHelper" in data
        # OTel and statusline should NOT be set (only api-key-helper was requested)
        assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in data.get("env", {})

    def test_only_otel(self, tmp_path: Path) -> None:
        tool = _make_tool(tmp_path, ToolType.CLAUDE_CODE, AuthMode.BEDROCK_VK)

        with patch("cli.setup.detect_tools", return_value=[tool]), patch(
            "cli.setup.backup_config", return_value=None
        ), patch(
            "cli.tools.otel_config._validate_otel_connection", return_value=True
        ):
            runner = CliRunner()
            result = runner.invoke(
                cli,
                ["setup", "--gateway-url", "https://gw.example.com",
                 "--only", "otel"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        # OTel should be skipped because otel_endpoint is empty
        assert "Setup Summary" in result.output


class TestIdempotency:
    """BR-SETUP-03: Re-run updates existing config."""

    def test_rerun_updates_config(self, tmp_path: Path) -> None:
        tool = _make_tool(tmp_path, ToolType.CLAUDE_CODE, AuthMode.BEDROCK_VK)
        # Pre-existing config with old values
        Path(tool.config_path).write_text(
            json.dumps({
                "apiKeyHelper": "/old/helper",
                "env": {"ANTHROPIC_BASE_URL": "https://old.com"},
                "userSetting": "keep-me",
            }),
            encoding="utf-8",
        )

        with patch("cli.setup.detect_tools", return_value=[tool]), patch(
            "cli.setup.backup_config", return_value=None
        ), patch(
            "cli.tools.bedrock_config._resolve_helper_path",
            return_value="/new/helper",
        ):
            runner = CliRunner()
            result = runner.invoke(
                cli,
                ["setup", "--gateway-url", "https://new-gw.com"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        data = json.loads(Path(tool.config_path).read_text(encoding="utf-8"))
        assert data["apiKeyHelper"] == "/new/helper"
        assert data["env"]["ANTHROPIC_BASE_URL"] == "https://new-gw.com"
        assert data["userSetting"] == "keep-me"  # non-gateway key preserved


class TestPartialFailure:
    """BR-SETUP-04: Partial failure — other components continue."""

    def test_otel_failure_doesnt_block_bedrock(self, tmp_path: Path) -> None:
        tool = _make_tool(tmp_path, ToolType.CLAUDE_CODE, AuthMode.BEDROCK_VK)

        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            "gateway_url: https://gw.example.com\n"
            "otel_endpoint: https://otel.unreachable.com\n",
            encoding="utf-8",
        )

        with patch("cli.setup.detect_tools", return_value=[tool]), patch(
            "cli.setup.backup_config", return_value=None
        ), patch(
            "cli.tools.bedrock_config._resolve_helper_path",
            return_value="/bin/helper",
        ), patch(
            "cli.tools.otel_config._validate_otel_connection", return_value=False
        ), patch("cli.config.DEFAULT_CONFIG_PATH", str(cfg_file)):
            runner = CliRunner()
            result = runner.invoke(
                cli,
                ["setup"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        # Bedrock should still succeed
        data = json.loads(Path(tool.config_path).read_text(encoding="utf-8"))
        assert data["apiKeyHelper"] == "/bin/helper"
        # OTel marked as failed in summary
        assert "Failed:" in result.output


class TestMissingGatewayUrl:
    """BR-SETUP-05: Missing --gateway-url with no config."""

    def test_error_without_gateway_url(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "empty_config.yaml"
        cfg_file.write_text("", encoding="utf-8")

        with patch("cli.config.DEFAULT_CONFIG_PATH", str(cfg_file)):
            runner = CliRunner()
            result = runner.invoke(
                cli,
                ["setup"],
                catch_exceptions=False,
            )

        assert result.exit_code != 0
        assert "Gateway URL" in result.output or "gateway_url" in result.output.lower()


class TestVersionCommand:
    def test_version_output(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["version"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "0.1.0" in result.output
