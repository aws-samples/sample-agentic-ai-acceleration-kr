# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Tests for api_key_helper.main — normal and daemon mode."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

from api_key_helper.config import HelperConfig
from api_key_helper.vk_client import VirtualKeyResponse


class TestNormalMode:
    def test_success_flow(self) -> None:
        from api_key_helper.main import _run_normal

        config = HelperConfig(gateway_url="https://gw.example.com")
        vk = VirtualKeyResponse(
            virtual_key="vk-123",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=12),
        )
        log = MagicMock()

        with patch("api_key_helper.main.check_sso_session", return_value=True), patch(
            "api_key_helper.main.create_presigned_sts_request",
            return_value={"url": "https://sts.example.com/", "headers": {}},
        ), patch("api_key_helper.main.get_device_name", return_value="laptop"), patch(
            "api_key_helper.main.request_virtual_key", return_value=vk
        ), patch("builtins.print") as mock_print:
            exit_code = _run_normal(config, log)

        assert exit_code == 0
        mock_print.assert_called_once_with("vk-123", flush=True)

    def test_sso_expired(self) -> None:
        from api_key_helper.main import _run_normal

        config = HelperConfig(gateway_url="https://gw.example.com")
        log = MagicMock()

        with patch("api_key_helper.main.check_sso_session", return_value=False):
            exit_code = _run_normal(config, log)

        assert exit_code == 1

    def test_api_error(self) -> None:
        from api_key_helper.main import _run_normal

        config = HelperConfig(gateway_url="https://gw.example.com")
        log = MagicMock()

        with patch("api_key_helper.main.check_sso_session", return_value=True), patch(
            "api_key_helper.main.create_presigned_sts_request",
            return_value={"url": "https://sts.example.com/", "headers": {}},
        ), patch("api_key_helper.main.get_device_name", return_value="laptop"), patch(
            "api_key_helper.main.request_virtual_key",
            side_effect=Exception("connection refused"),
        ):
            exit_code = _run_normal(config, log)

        assert exit_code == 2


class TestDaemonMode:
    def test_initial_failure_exits(self) -> None:
        from api_key_helper.main import _run_daemon

        config = HelperConfig(gateway_url="https://gw.example.com")
        log = MagicMock()

        with patch("api_key_helper.main.check_sso_session", return_value=False):
            exit_code = _run_daemon(config, 300, log)

        assert exit_code == 1

    def test_daemon_exits_on_shutdown(self) -> None:
        from api_key_helper.main import _run_daemon

        config = HelperConfig(gateway_url="https://gw.example.com")
        vk = VirtualKeyResponse(
            virtual_key="vk-123",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=12),
        )
        log = MagicMock()

        call_count = 0

        def fake_sleep(seconds):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                import api_key_helper.main as m
                m._shutdown_requested = True

        with patch("api_key_helper.main.check_sso_session", return_value=True), patch(
            "api_key_helper.main.create_presigned_sts_request",
            return_value={"url": "https://sts.example.com/", "headers": {}},
        ), patch("api_key_helper.main.get_device_name", return_value="laptop"), patch(
            "api_key_helper.main.request_virtual_key", return_value=vk
        ), patch("api_key_helper.main.time.sleep", side_effect=fake_sleep), patch(
            "api_key_helper.main.install_signal_handlers"
        ), patch("builtins.print"):
            import api_key_helper.main as m
            m._shutdown_requested = False
            exit_code = _run_daemon(config, 5, log)

        assert exit_code == 0
