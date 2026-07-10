# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Tests for cli.config — domain entities and config loader."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from cli.config import (
    AuthMode,
    GatewayConfig,
    ToolType,
    load_config,
)


class TestLoadConfig:
    def test_load_from_yaml(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            "gateway_url: https://gw.example.com\n"
            "otel_endpoint: https://otel.example.com\n"
            "statusline:\n"
            "  interval_seconds: 60\n",
            encoding="utf-8",
        )
        config = load_config(config_path=str(cfg_file))
        assert config.gateway_url == "https://gw.example.com"
        assert config.otel_endpoint == "https://otel.example.com"
        assert config.statusline_interval == 60

    def test_load_missing_file_returns_defaults(self, tmp_path: Path) -> None:
        config = load_config(config_path=str(tmp_path / "missing.yaml"))
        assert config.gateway_url == ""
        assert config.statusline_interval == 30

    def test_env_overrides_yaml(self, tmp_path: Path, monkeypatch) -> None:
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("gateway_url: https://from-yaml.com\n", encoding="utf-8")
        monkeypatch.setenv("GATEWAY_CLI_GATEWAY_URL", "https://from-env.com")
        config = load_config(config_path=str(cfg_file))
        assert config.gateway_url == "https://from-env.com"

    def test_cli_overrides_env(self, tmp_path: Path, monkeypatch) -> None:
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("", encoding="utf-8")
        monkeypatch.setenv("GATEWAY_CLI_GATEWAY_URL", "https://from-env.com")
        config = load_config(
            config_path=str(cfg_file),
            cli_overrides={"gateway_url": "https://from-cli.com"},
        )
        assert config.gateway_url == "https://from-cli.com"

    def test_cli_none_values_skipped(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("gateway_url: https://gw.com\n", encoding="utf-8")
        config = load_config(
            config_path=str(cfg_file),
            cli_overrides={"gateway_url": None},
        )
        assert config.gateway_url == "https://gw.com"

    def test_verbose_env_bool(self, tmp_path: Path, monkeypatch) -> None:
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("", encoding="utf-8")
        monkeypatch.setenv("GATEWAY_CLI_VERBOSE", "true")
        config = load_config(config_path=str(cfg_file))
        assert config.verbose is True

    def test_jwt_auth_section(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            "jwt_auth:\n"
            "  sso_auth_url: https://sso.example.com/authorize\n"
            "  sso_token_url: https://sso.example.com/token\n"
            "  client_id: my-cli\n",
            encoding="utf-8",
        )
        config = load_config(config_path=str(cfg_file))
        assert config.jwt_auth["sso_auth_url"] == "https://sso.example.com/authorize"
        assert config.jwt_auth["client_id"] == "my-cli"

    def test_defaults(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("", encoding="utf-8")
        config = load_config(config_path=str(cfg_file))
        assert config.connect_timeout == 5
        assert config.read_timeout == 10
        assert config.lang == "en"
        assert config.verbose is False


class TestEnums:
    def test_tool_type_values(self) -> None:
        assert ToolType.CLAUDE_CODE.value == "claude-code"
        assert ToolType.OPENCODE.value == "opencode"
        assert ToolType.CLINE.value == "cline"

    def test_auth_mode_values(self) -> None:
        assert AuthMode.BEDROCK_VK.value == "bedrock_vk"
        assert AuthMode.JWT.value == "jwt"
