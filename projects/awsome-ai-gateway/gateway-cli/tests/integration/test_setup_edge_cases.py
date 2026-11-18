# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""setup / disable / status 라이프사이클 엣지케이스.

⚠️ 이 파일도 전면 재작성됐다 — 사라진 설계(`--only` 필터, 도구 자동탐지,
"No AI tools detected" 메시지, 도구별 설정파일 병합)를 검증하던 5개 테스트가
`patch("cli.setup.detect_tools")` 에서 AttributeError 로 죽어 있었다.
지금의 3개 명령(`setup`/`disable`/`status`)은 모두 managed-settings.d 의
`50-gateway.json` 한 파일을 대상으로 동작한다(`cli/managed.py`).

`disable`/`status` 는 이전까지 통합 커버리지가 아예 없었다.

managed-settings 경로를 tmp 로 돌려서 실제 시스템 디렉터리를 건드리지 않고
read/exists 경로를 진짜로 태운다. 쓰기(`write_gateway_settings`)와
삭제(`remove_gateway_settings`)만 sudo 를 쓰므로 그 둘은 패치한다.

⚠️ 패치 지점은 `_managed_file` 이 아니라 `cli.managed._managed_dir` 이다.
`cli/status.py` 가 `from cli.managed import _managed_file` 로 이름을 자기 모듈에
바인딩하므로, `cli.managed._managed_file` 만 패치하면 status 가 화면에 찍는
경로는 여전히 실제 시스템 디렉터리다(실측: 이 파일 첫 실행에서 그 단정이
깨졌다). 한 단계 더 아래인 `_managed_dir` 은 오직 `cli.managed` 의 전역
조회로만 불리므로, 여기를 돌리면 어느 모듈이 어떻게 import 했든 전부 따라온다.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from cli.main import cli
from cli.managed import GATEWAY_SETTINGS_FILENAME, build_gateway_settings


@pytest.fixture()
def managed_file(tmp_path: Path):
    """managed-settings.d 를 tmp 로 돌린다 — 실제 시스템 경로 대신."""
    managed_dir = tmp_path / "managed-settings.d"
    managed_dir.mkdir(parents=True, exist_ok=True)
    with patch("cli.managed._managed_dir", return_value=managed_dir), patch(
        # macOS 에서 돌리면 status/disable 이 이전 버전의 /etc/claude-code 파일도 찾아본다.
        # 개발자 Mac 에 그 파일이 실제로 있으면 결과가 머신마다 달라지므로 여기서는 끈다
        # (legacy 처리 자체는 test_managed_settings_location.py 가 검증한다).
        "cli.managed._legacy_managed_file", return_value=None
    ):
        yield managed_dir / GATEWAY_SETTINGS_FILENAME


def _enable(managed_file: Path, **overrides) -> dict:
    """실제 `build_gateway_settings` 산출물을 파일로 심어 'ON' 상태를 만든다.

    손으로 dict 를 지어내지 않는 이유: 지어낸 모양은 프로덕션이 쓰는 모양과
    어긋나도 테스트가 통과해버린다. 실제 빌더를 통과시켜야 status 가 읽는 키와
    setup 이 쓰는 키가 같다는 것이 보장된다.
    """
    kwargs = {
        "gateway_url": "https://gw.example.com",
        "admin_api_url": "https://gw.example.com:8080",
        "api_key_helper_path": "/usr/local/bin/api-key-helper",
    }
    kwargs.update(overrides)
    settings = build_gateway_settings(**kwargs)
    managed_file.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return settings


class TestStatus:
    def test_status_off_when_no_managed_file(self, managed_file: Path) -> None:
        assert not managed_file.exists()
        result = CliRunner().invoke(cli, ["status"], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        assert "[OFF]" in result.output
        assert str(managed_file) in result.output

    def test_status_on_shows_urls_from_the_file(self, managed_file: Path) -> None:
        _enable(managed_file)
        result = CliRunner().invoke(cli, ["status"], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        assert "[ON]" in result.output
        assert "https://gw.example.com" in result.output
        assert "https://gw.example.com:8080" in result.output
        assert "/usr/local/bin/api-key-helper" in result.output

    def test_status_reports_sts_mode_when_oidc_absent(self, managed_file: Path) -> None:
        """OIDC 미설정은 조용히 넘어가면 안 된다 — 잘못된 신원으로 VK 가 나간다."""
        _enable(managed_file)
        result = CliRunner().invoke(cli, ["status"], catch_exceptions=False)

        assert "STS" in result.output
        assert "OIDC" in result.output

    def test_status_reports_oidc_mode_when_both_keys_present(
        self, managed_file: Path
    ) -> None:
        _enable(
            managed_file,
            oidc_issuer_url="https://idp.example.com",
            oidc_client_id="cid-123",
        )
        result = CliRunner().invoke(cli, ["status"], catch_exceptions=False)

        assert "https://idp.example.com" in result.output
        assert "cid-123" in result.output
        assert "OIDC" in result.output

    def test_status_survives_corrupt_managed_file(self, managed_file: Path) -> None:
        """손상된 JSON 에 크래시하면 사용자가 복구 방법을 알 수 없다 — OFF 로 보고."""
        managed_file.write_text("{not json", encoding="utf-8")
        result = CliRunner().invoke(cli, ["status"], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        assert "[OFF]" in result.output

    def test_status_shows_otel_endpoint_when_configured(self, managed_file: Path) -> None:
        _enable(managed_file, otel_endpoint="http://otel.example.com:4317")
        result = CliRunner().invoke(cli, ["status"], catch_exceptions=False)

        assert "http://otel.example.com:4317" in result.output


class TestDisable:
    def test_disable_when_not_enabled_is_a_noop(self, managed_file: Path) -> None:
        with patch("cli.disable.remove_gateway_settings") as remove:
            result = CliRunner().invoke(cli, ["disable"], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        assert "not currently enabled" in result.output
        # 없는 파일을 지우려고 sudo 를 띄우면 안 된다.
        assert not remove.called

    def test_disable_removes_and_tells_user_to_restart(self, managed_file: Path) -> None:
        _enable(managed_file)
        with patch("cli.disable.remove_gateway_settings", return_value=True) as remove:
            result = CliRunner().invoke(cli, ["disable"], catch_exceptions=False)

        assert result.exit_code == 0, result.output
        assert remove.called
        assert "disabled" in result.output.lower()
        assert "Restart Claude Code" in result.output

    def test_disable_failure_is_reported(self, managed_file: Path) -> None:
        _enable(managed_file)
        with patch(
            "cli.disable.remove_gateway_settings",
            side_effect=PermissionError("sudo: a password is required"),
        ):
            result = CliRunner().invoke(cli, ["disable"])

        assert result.exit_code != 0
        assert "Failed to remove managed settings" in result.output


class TestSetupStatusRoundTrip:
    def test_document_setup_writes_is_what_status_reads(self, managed_file: Path) -> None:
        """setup 이 넘기는 인자로 만든 문서를 status 가 그대로 해석하는지 확인.

        두 명령이 같은 키 이름에 합의하고 있는지가 핵심이다 — 한쪽만 키를
        바꾸면 사용자에게는 '설정했는데 status 는 OFF/빈칸' 으로 보인다.
        """
        with patch("cli.setup.write_gateway_settings") as write, patch(
            "cli.setup.is_gateway_enabled", return_value=False
        ), patch("cli.setup.resolve_helper_path", return_value="/bin/helper"):
            result = CliRunner().invoke(
                cli,
                [
                    "setup",
                    "--gateway-url",
                    "https://gw.example.com",
                    "--issuer-url",
                    "https://idp.example.com",
                    "--client-id",
                    "cid-123",
                ],
                catch_exceptions=False,
            )
        assert result.exit_code == 0, result.output

        # setup 이 실제로 넘긴 인자로 문서를 만들어 파일에 심는다.
        settings = build_gateway_settings(**write.call_args.kwargs)
        managed_file.write_text(json.dumps(settings, indent=2), encoding="utf-8")

        status = CliRunner().invoke(cli, ["status"], catch_exceptions=False)
        assert status.exit_code == 0, status.output
        assert "[ON]" in status.output
        assert "https://gw.example.com" in status.output
        assert "https://gw.example.com:8080" in status.output
        assert "/bin/helper" in status.output
        assert "cid-123" in status.output
        # OIDC 를 설정했으므로 STS 경고가 남아 있으면 안 된다.
        assert "STS (IAM" not in status.output

    def test_statusline_command_is_registered(self, managed_file: Path) -> None:
        """managed settings 의 statusLine 이 빠지면 사용량 표시가 조용히 사라진다."""
        settings = _enable(managed_file)
        assert settings["statusLine"] == {"type": "command", "command": "statusline"}


class TestMissingGatewayUrl:
    """BR-SETUP-05: Missing --gateway-url with no config."""

    def test_error_without_gateway_url(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "empty_config.yaml"
        cfg_file.write_text("", encoding="utf-8")

        with patch("cli.config.DEFAULT_CONFIG_PATH", str(cfg_file)):
            result = CliRunner().invoke(cli, ["setup"], catch_exceptions=False)

        assert result.exit_code != 0
        assert "Gateway URL" in result.output or "gateway_url" in result.output.lower()


class TestVersionCommand:
    def test_version_output(self) -> None:
        result = CliRunner().invoke(cli, ["version"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "0.1.0" in result.output
