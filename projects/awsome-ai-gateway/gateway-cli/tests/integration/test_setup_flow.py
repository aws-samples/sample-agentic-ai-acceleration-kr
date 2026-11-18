# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""`gateway-cli setup` 명령 배선 통합 테스트.

⚠️ 이 파일은 한 번 전면 재작성됐다. 원래는 이미 없어진 설계(도구 자동탐지 →
도구별 설정파일 병합 → "Setup Summary/Total/Success" 요약, `--only` 필터)를
검증하고 있었다. 현재 `setup` 은 그런 일을 하지 않는다 — Claude Code 의
managed-settings.d 에 파일 하나(`50-gateway.json`)를 쓴다(`cli/managed.py`).
그래서 9개 테스트가 `patch("cli.setup.detect_tools")` 에서
`AttributeError: module 'cli.setup' does not have the attribute 'detect_tools'`
로 전부 죽어 있었다 — 즉 setup 명령에 대한 통합 커버리지가 0 이었다.
(main 브랜치에도 같은 상태였으므로 이 브랜치가 만든 회귀는 아니다.)

여기서 검증하는 것은 **명령 배선**이다: 순수 함수인 `build_gateway_settings` 와
`_resolve_oidc` 는 tests/unit/test_managed_oidc.py 가 이미 덮고 있으므로
중복하지 않고, 그 함수들에 무엇이 전달되는지 / URL 파생 / 경고 / 실패 경로를 본다.

`write_gateway_settings` 는 항상 패치한다 — 실제 구현은 `sudo tee` 로
시스템 관리 설정 디렉터리(`/etc/claude-code`, macOS 는 `/Library/...`)에 쓰기 때문에
테스트가 호스트를 건드리면 안 된다.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from cli.main import cli
from cli.managed import GatewayWriteResult


def _invoke(args: list[str], **patches):
    """setup 을 호출하고, write_gateway_settings 가 받은 kwargs 를 함께 돌려준다."""
    written = GatewayWriteResult(Path("/etc/claude-code/managed-settings.d/50-gateway.json"))
    with patch("cli.setup.write_gateway_settings", return_value=written) as write, patch(
        "cli.setup.is_gateway_enabled", return_value=patches.pop("already_enabled", False)
    ), patch(
        # ⚠️ 패치 대상은 `cli.setup.resolve_helper_path` 다.
        # setup.py 가 `from cli.tools.bedrock_config import resolve_helper_path` 로
        # 이름을 자기 모듈에 바인딩하므로, 원본 모듈 쪽을 패치하면 조용한 no-op 이
        # 된다(실제 바이너리 탐색이 돌아 결과가 머신마다 달라진다).
        "cli.setup.resolve_helper_path",
        return_value=patches.pop("helper_path", "/usr/local/bin/api-key-helper"),
    ):
        result = CliRunner().invoke(cli, args, catch_exceptions=False)
    return result, write


class TestSetupWritesManagedSettings:
    def test_gateway_url_and_derived_urls(self) -> None:
        result, write = _invoke(["setup", "--gateway-url", "https://gw.example.com"])

        assert result.exit_code == 0, result.output
        kwargs = write.call_args.kwargs
        assert kwargs["gateway_url"] == "https://gw.example.com"
        # 관례: gateway-proxy :8000, admin-api :8080. 파생이 깨지면 api-key-helper 가
        # VK 발급 엔드포인트를 못 찾는다.
        assert kwargs["admin_api_url"] == "https://gw.example.com:8080"
        assert kwargs["api_key_helper_path"] == "/usr/local/bin/api-key-helper"
        assert "https://gw.example.com" in result.output
        assert "https://gw.example.com:8080" in result.output

    def test_otel_endpoint_derived_as_http_even_for_grpc(self) -> None:
        """Node.js OTEL SDK 는 gRPC 여도 http:// 스킴을 요구한다 (setup.py 주석)."""
        result, write = _invoke(["setup", "--gateway-url", "https://gw.example.com"])

        assert result.exit_code == 0, result.output
        assert write.call_args.kwargs["otel_endpoint"] == "http://gw.example.com:4317"

    def test_explicit_otel_endpoint_wins_over_derivation(self) -> None:
        result, write = _invoke(
            [
                "setup",
                "--gateway-url",
                "https://gw.example.com",
                "--otel-endpoint",
                "http://otel.example.com:4317",
            ]
        )

        assert result.exit_code == 0, result.output
        assert write.call_args.kwargs["otel_endpoint"] == "http://otel.example.com:4317"

    def test_explicit_admin_api_url_wins_over_derivation(self) -> None:
        result, write = _invoke(
            [
                "setup",
                "--gateway-url",
                "https://gw.example.com",
                "--admin-api-url",
                "https://admin.example.com",
            ]
        )

        assert result.exit_code == 0, result.output
        assert write.call_args.kwargs["admin_api_url"] == "https://admin.example.com"

    def test_explicit_helper_path_skips_resolution(self) -> None:
        result, write = _invoke(
            [
                "setup",
                "--gateway-url",
                "https://gw.example.com",
                "--api-key-helper",
                "/opt/gw/api-key-helper",
            ]
        )

        assert result.exit_code == 0, result.output
        assert write.call_args.kwargs["api_key_helper_path"] == "/opt/gw/api-key-helper"


class TestSetupOIDC:
    def test_oidc_options_forwarded(self) -> None:
        result, write = _invoke(
            [
                "setup",
                "--gateway-url",
                "https://gw.example.com",
                "--issuer-url",
                "https://idp.example.com/",
                "--client-id",
                "cid-123",
                "--audience",
                "aud-1",
            ]
        )

        assert result.exit_code == 0, result.output
        kwargs = write.call_args.kwargs
        # 후행 슬래시는 제거돼야 한다 — 토큰의 iss 와 정확히 일치해야 하므로.
        assert kwargs["oidc_issuer_url"] == "https://idp.example.com"
        assert kwargs["oidc_client_id"] == "cid-123"
        assert kwargs["oidc_audience"] == "aud-1"
        assert "OIDC Issuer" in result.output

    def test_missing_oidc_warns_loudly(self, monkeypatch) -> None:
        """조용히 STS 로 떨어지면 잘못된 사용자로 VK 가 발급되거나 1P 로그인으로 되돌아간다.

        setup.py 가 이 경우 노란 경고 2줄을 내도록 되어 있다 — 그 계약을 고정한다.
        """
        for var in ("OIDC_ISSUER_URL", "OIDC_CLIENT_ID", "OIDC_AUDIENCE"):
            monkeypatch.delenv(var, raising=False)

        # login 토큰 캐시도 비어 있어야 한다(conftest 가 HOME 을 tmp 로 돌리므로 기본 빈 상태).
        result, write = _invoke(["setup", "--gateway-url", "https://gw.example.com"])

        assert result.exit_code == 0, result.output
        assert "STS" in result.output
        assert "gateway-cli login" in result.output
        kwargs = write.call_args.kwargs
        assert kwargs["oidc_issuer_url"] is None
        assert kwargs["oidc_client_id"] is None

    def test_partial_oidc_is_not_written(self, monkeypatch) -> None:
        """한쪽만 있으면 api-key-helper 는 OIDC 모드로 못 들어간다 — 반쪽 기록 금지."""
        monkeypatch.delenv("OIDC_CLIENT_ID", raising=False)

        result, write = _invoke(
            ["setup", "--gateway-url", "https://gw.example.com",
             "--issuer-url", "https://idp.example.com"]
        )

        assert result.exit_code == 0, result.output
        kwargs = write.call_args.kwargs
        assert kwargs["oidc_issuer_url"] is None
        assert kwargs["oidc_client_id"] is None
        assert "STS" in result.output

    def test_oidc_from_environment(self, monkeypatch) -> None:
        monkeypatch.setenv("OIDC_ISSUER_URL", "https://idp.env.example.com")
        monkeypatch.setenv("OIDC_CLIENT_ID", "cid-env")

        result, write = _invoke(["setup", "--gateway-url", "https://gw.example.com"])

        assert result.exit_code == 0, result.output
        kwargs = write.call_args.kwargs
        assert kwargs["oidc_issuer_url"] == "https://idp.env.example.com"
        assert kwargs["oidc_client_id"] == "cid-env"


class TestSetupIdempotencyAndErrors:
    def test_rerun_reports_update(self) -> None:
        result, write = _invoke(
            ["setup", "--gateway-url", "https://gw.example.com"], already_enabled=True
        )

        assert result.exit_code == 0, result.output
        assert "already enabled" in result.output.lower()
        # 이미 켜져 있어도 다시 써야 한다 (설정 갱신이 목적).
        assert write.called

    def test_write_failure_is_reported_not_swallowed(self) -> None:
        with patch(
            "cli.setup.write_gateway_settings",
            side_effect=PermissionError("sudo: a password is required"),
        ), patch("cli.setup.is_gateway_enabled", return_value=False), patch(
            "cli.setup.resolve_helper_path", return_value="/bin/helper"
        ):
            result = CliRunner().invoke(
                cli, ["setup", "--gateway-url", "https://gw.example.com"]
            )

        assert result.exit_code != 0
        assert "Failed to write managed settings" in result.output

    def test_missing_gateway_url_errors(self, tmp_path: Path) -> None:
        empty = tmp_path / "config.yaml"
        empty.write_text("", encoding="utf-8")

        with patch("cli.config.DEFAULT_CONFIG_PATH", str(empty)):
            result = CliRunner().invoke(cli, ["setup"])

        assert result.exit_code != 0
        assert "Gateway URL" in result.output


class TestSetupFromConfigFile:
    def test_config_yaml_supplies_gateway_and_otel(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "gateway_url: https://gw.from-file.com\n"
            "otel_endpoint: https://otel.from-file.com\n",
            encoding="utf-8",
        )

        written = GatewayWriteResult(Path("/etc/claude-code/managed-settings.d/50-gateway.json"))
        with patch("cli.setup.write_gateway_settings", return_value=written) as write, patch(
            "cli.setup.is_gateway_enabled", return_value=False
        ), patch(
            "cli.setup.resolve_helper_path", return_value="/bin/helper"
        ), patch("cli.config.DEFAULT_CONFIG_PATH", str(cfg)):
            result = CliRunner().invoke(cli, ["setup"], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        kwargs = write.call_args.kwargs
        assert kwargs["gateway_url"] == "https://gw.from-file.com"
        assert kwargs["otel_endpoint"] == "https://otel.from-file.com"
