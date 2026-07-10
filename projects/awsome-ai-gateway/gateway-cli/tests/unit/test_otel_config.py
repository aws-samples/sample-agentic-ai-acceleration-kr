# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Tests for cli.tools.otel_config — OTel endpoint configuration."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import responses

from cli.config import (
    AuthMode,
    ComponentStatus,
    DetectedTool,
    GatewayConfig,
    ToolType,
)
from cli.tools.otel_config import (
    _apply_claude_code_otel,
    _apply_cline_otel,
    _apply_opencode_otel,
    setup_otel,
)


class TestApplyClaudeCodeOtel:
    def test_sets_otel_env_vars(self) -> None:
        config = GatewayConfig(
            otel_endpoint="https://otel.example.com",
            otel_auth_token="my-token",
        )
        result = _apply_claude_code_otel({}, config)
        assert result["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == "https://otel.example.com"
        assert "Authorization=Bearer my-token" in result["env"]["OTEL_EXPORTER_OTLP_HEADERS"]

    def test_no_auth_token(self) -> None:
        config = GatewayConfig(otel_endpoint="https://otel.example.com")
        result = _apply_claude_code_otel({}, config)
        assert "OTEL_EXPORTER_OTLP_HEADERS" not in result.get("env", {})


class TestApplyOpenCodeOtel:
    def test_sets_telemetry(self) -> None:
        config = GatewayConfig(otel_endpoint="https://otel.example.com", otel_auth_token="tok")
        result = _apply_opencode_otel({}, config)
        assert result["telemetry"]["endpoint"] == "https://otel.example.com"
        assert result["telemetry"]["auth_token"] == "tok"


class TestApplyClineOtel:
    def test_sets_cline_telemetry(self) -> None:
        config = GatewayConfig(otel_endpoint="https://otel.example.com")
        result = _apply_cline_otel({}, config)
        assert result["cline.telemetryEndpoint"] == "https://otel.example.com"


class TestSetupOtel:
    def test_skip_no_endpoint(self, tmp_path: Path) -> None:
        tool = DetectedTool(
            tool_type=ToolType.CLAUDE_CODE,
            name="Claude Code",
            config_path=str(tmp_path / "settings.json"),
            auth_mode=AuthMode.BEDROCK_VK,
        )
        config = GatewayConfig(otel_endpoint="")
        result = setup_otel(tool, config)
        assert result.status == ComponentStatus.SKIPPED

    @responses.activate
    def test_success_with_connection(self, tmp_path: Path) -> None:
        config_path = tmp_path / "settings.json"
        config_path.write_text("{}", encoding="utf-8")

        responses.add(responses.GET, "https://otel.example.com", status=200)

        tool = DetectedTool(
            tool_type=ToolType.CLAUDE_CODE,
            name="Claude Code",
            config_path=str(config_path),
            auth_mode=AuthMode.BEDROCK_VK,
        )
        config = GatewayConfig(otel_endpoint="https://otel.example.com")
        result = setup_otel(tool, config)
        assert result.status == ComponentStatus.SUCCESS

        data = json.loads(config_path.read_text(encoding="utf-8"))
        assert data["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == "https://otel.example.com"

    @responses.activate
    def test_connection_failure(self, tmp_path: Path) -> None:
        config_path = tmp_path / "settings.json"
        config_path.write_text("{}", encoding="utf-8")

        responses.add(
            responses.GET,
            "https://otel.example.com",
            body=ConnectionError("refused"),
        )

        tool = DetectedTool(
            tool_type=ToolType.CLAUDE_CODE,
            name="Claude Code",
            config_path=str(config_path),
            auth_mode=AuthMode.BEDROCK_VK,
        )
        config = GatewayConfig(otel_endpoint="https://otel.example.com")
        result = setup_otel(tool, config)
        assert result.status == ComponentStatus.FAILED
        assert "unreachable" in result.message.lower()
