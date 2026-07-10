# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Tests for api_key_helper.sso — SSO session check and SigV4 pre-sign."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

from api_key_helper.sso import check_sso_session, get_device_name


class TestCheckSsoSession:
    def test_valid_session(self) -> None:
        with patch("api_key_helper.sso.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            assert check_sso_session() is True

    def test_expired_session(self) -> None:
        with patch("api_key_helper.sso.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            assert check_sso_session() is False

    def test_aws_cli_not_found(self) -> None:
        with patch("api_key_helper.sso.subprocess.run", side_effect=FileNotFoundError):
            assert check_sso_session() is False

    def test_timeout(self) -> None:
        with patch(
            "api_key_helper.sso.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="aws", timeout=15),
        ):
            assert check_sso_session() is False


class TestGetDeviceName:
    def test_returns_hostname(self) -> None:
        name = get_device_name()
        assert isinstance(name, str)
        assert len(name) > 0

    def test_fallback_on_error(self) -> None:
        with patch("api_key_helper.sso.socket.gethostname", side_effect=Exception), patch(
            "api_key_helper.sso.platform.node", side_effect=Exception
        ):
            assert get_device_name() == "unknown-device"
