# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""api-key-helper entry point — VK issuance + daemon mode (PP-01, PP-02, LP-02).

Dual-mode (auto-detect):
  - OIDC mode: ``OIDC_ISSUER_URL`` + ``OIDC_CLIENT_ID`` env vars present.
               Uses cached OIDC token (from ``gateway-cli login``) → admin-api
               ``/v1/auth/exchange`` → VK.
  - STS mode (legacy): SSO presigned STS request → admin-api ``/cli/auth/virtual-key``.
"""

from __future__ import annotations

import os
import signal
import sys
import time
from datetime import datetime, timezone

import click
import structlog

from api_key_helper.sso import check_sso_session, create_presigned_sts_request, get_device_name, get_sso_session_expiry
from api_key_helper.vk_client import request_virtual_key

# ---------------------------------------------------------------------------
# Logging (PP-02 — independent structlog per binary)
# ---------------------------------------------------------------------------

SENSITIVE_KEYS = {"sts_request", "virtual_key", "jwt_token", "otel_auth_token"}


def _mask_sensitive(logger, method_name, event_dict):
    for key in SENSITIVE_KEYS:
        if key in event_dict:
            event_dict[key] = "***MASKED***"
    return event_dict


def configure_logging(verbose: bool = False) -> None:
    level = 0 if verbose else 20
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            _mask_sensitive,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    )


# ---------------------------------------------------------------------------
# Signal handling (LP-02)
# ---------------------------------------------------------------------------

_shutdown_requested = False


def _handle_shutdown(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True


def install_signal_handlers() -> None:
    signal.signal(signal.SIGINT, _handle_shutdown)
    if sys.platform != "win32":
        signal.signal(signal.SIGTERM, _handle_shutdown)


def is_shutdown_requested() -> bool:
    return _shutdown_requested


# ---------------------------------------------------------------------------
# Normal mode flow
# ---------------------------------------------------------------------------

def _run_normal(config, log) -> int:
    """Execute normal mode: SSO check → pre-sign → VK request → stdout."""

    # [1] SSO session check (BR-VK-02)
    log.info("checking_sso_session")
    if not check_sso_session():
        print(
            "SSO session expired or not authenticated. Run `aws sso login`.",
            file=sys.stderr,
        )
        return 1

    # [2] Pre-signed STS request (BR-VK-03)
    try:
        sts_request = create_presigned_sts_request()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    # [3] Device name (BR-VK-04)
    device_name = get_device_name()

    # [4] SSO session expiry (FR-2.2 — non-fatal if unavailable)
    sso_session_expires_at = get_sso_session_expiry()

    # [5] Request Virtual Key
    try:
        vk_response = request_virtual_key(config, sts_request, device_name, sso_session_expires_at)
    except Exception as exc:
        log.error("virtual_key_request_failed", error=str(exc))
        print(f"Admin API error: {exc}", file=sys.stderr)
        return 2

    # [6] Output VK to stdout only (BR-VK-01)
    print(vk_response.virtual_key, flush=True)
    log.info("virtual_key_issued", expires_at=vk_response.expires_at.isoformat())

    return 0


# ---------------------------------------------------------------------------
# Daemon mode flow (PP-01, RP-01)
# ---------------------------------------------------------------------------

def _run_daemon(config, check_interval: int, log) -> int:
    """Execute daemon mode: initial VK + polling loop for expiry renewal."""

    install_signal_handlers()

    # Initial VK issuance
    log.info("daemon_initial_vk")
    if not check_sso_session():
        print(
            "SSO session expired or not authenticated. Run `aws sso login`.",
            file=sys.stderr,
        )
        return 1

    try:
        sts_request = create_presigned_sts_request()
        device_name = get_device_name()
        sso_session_expires_at = get_sso_session_expiry()
        vk_response = request_virtual_key(config, sts_request, device_name, sso_session_expires_at)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Admin API error: {exc}", file=sys.stderr)
        return 2

    print(vk_response.virtual_key, flush=True)
    log.info("daemon_started", check_interval=check_interval)

    current_vk = vk_response

    # Polling loop (PP-01 — fixed interval, RP-01 — no retry)
    while not is_shutdown_requested():
        time.sleep(check_interval)
        if is_shutdown_requested():
            break

        # Check if VK is near expiry (30 min threshold) (BR-VK-05)
        now = datetime.now(timezone.utc)
        time_remaining = (current_vk.expires_at - now).total_seconds()

        if time_remaining > 1800:  # > 30 minutes
            log.debug("vk_not_expiring", remaining_seconds=int(time_remaining))
            continue

        log.info("vk_expiring_soon", remaining_seconds=int(time_remaining))

        # Attempt renewal
        if not check_sso_session():
            print(
                "SSO session expired. VK renewal paused. Run `aws sso login`.",
                file=sys.stderr,
            )
            continue

        try:
            sts_request = create_presigned_sts_request()
            sso_session_expires_at = get_sso_session_expiry()
            new_vk = request_virtual_key(config, sts_request, device_name, sso_session_expires_at)
            current_vk = new_vk
            print(new_vk.virtual_key, flush=True)
            log.info("vk_renewed", expires_at=new_vk.expires_at.isoformat())
        except Exception as exc:
            log.error("vk_renewal_failed", error=str(exc))

    log.info("daemon_shutdown")
    return 0


# ---------------------------------------------------------------------------
# OIDC mode flow
# ---------------------------------------------------------------------------

def _run_oidc_normal(config, log) -> int:
    """Cached VK 우선 → 만료 임박 시 OIDC token 으로 새 VK 발급."""
    from gateway_cli_oidc.oidc_client import (
        CachedVK,
        OIDCExchangeError,
        OIDCLoginError,
        exchange_jwt_for_vk,
        get_valid_id_token,
        load_oidc_config_from_env,
        load_vk_cache,
        save_vk_cache,
    )

    oidc_cfg = load_oidc_config_from_env()
    if oidc_cfg is None:
        print(
            "OIDC config missing — set OIDC_ISSUER_URL + OIDC_CLIENT_ID, "
            "or use STS mode (unset OIDC_ISSUER_URL).",
            file=sys.stderr,
        )
        return 3
    admin_api_url = oidc_cfg.admin_api_url or config.gateway_url
    if not admin_api_url:
        print("ADMIN_API_URL or --gateway-url required for OIDC mode.", file=sys.stderr)
        return 3

    # 1. VK cache hit — 같은 IDP + admin-api + 만료 5분 이상 남음
    cached = load_vk_cache()
    if (
        cached
        and cached.issuer_url == oidc_cfg.issuer_url
        and cached.admin_api_url == admin_api_url
        and not cached.is_expiring(threshold_seconds=300)
    ):
        print(cached.virtual_key, flush=True)
        log.info(
            "virtual_key_cache_hit",
            mode="oidc",
            remaining_seconds=int(cached.expires_at - time.time()),
        )
        return 0

    # 2. cache miss / expiring → OIDC token 으로 새 VK 발급
    try:
        # id_token 사용 — 사용자 신원 (email, name, groups) 이 여기에만 있음 (Cognito).
        id_token = get_valid_id_token(oidc_cfg)
        if not id_token:
            print(
                "OIDC id_token missing in cache. "
                "Re-login required: gateway-cli login",
                file=sys.stderr,
            )
            return 4
    except OIDCLoginError as e:
        print(f"OIDC token error: {e}", file=sys.stderr)
        return 4

    device_name = get_device_name()
    try:
        vk_resp = exchange_jwt_for_vk(admin_api_url, id_token, device_name)
    except OIDCExchangeError as e:
        log.error("oidc_exchange_failed", error=str(e))
        print(f"Admin API exchange error: {e}", file=sys.stderr)
        return 2

    save_vk_cache(CachedVK(
        virtual_key=vk_resp.virtual_key,
        expires_at=vk_resp.expires_at.timestamp(),
        issuer_url=oidc_cfg.issuer_url,
        admin_api_url=admin_api_url,
        user_id=vk_resp.user_id,
        team_id=vk_resp.team_id,
    ))

    print(vk_resp.virtual_key, flush=True)
    log.info("virtual_key_issued", mode="oidc", expires_at=vk_resp.expires_at.isoformat())
    return 0


def _run_oidc_daemon(config, check_interval: int, log) -> int:
    """OIDC daemon — initial VK + polling renewal (BR-VK-05 in OIDC mode)."""
    from gateway_cli_oidc.oidc_client import (
        CachedVK,
        OIDCExchangeError,
        OIDCLoginError,
        exchange_jwt_for_vk,
        get_valid_id_token,
        load_oidc_config_from_env,
        save_vk_cache,
    )

    install_signal_handlers()

    oidc_cfg = load_oidc_config_from_env()
    if oidc_cfg is None:
        print("OIDC config missing.", file=sys.stderr)
        return 3
    admin_api_url = oidc_cfg.admin_api_url or config.gateway_url
    if not admin_api_url:
        print("ADMIN_API_URL or --gateway-url required.", file=sys.stderr)
        return 3

    device_name = get_device_name()

    # Initial VK
    try:
        id_token = get_valid_id_token(oidc_cfg)
        vk_resp = exchange_jwt_for_vk(admin_api_url, id_token, device_name)
    except (OIDCLoginError, OIDCExchangeError) as e:
        print(f"Initial VK issue failed: {e}", file=sys.stderr)
        return 4

    save_vk_cache(CachedVK(
        virtual_key=vk_resp.virtual_key,
        expires_at=vk_resp.expires_at.timestamp(),
        issuer_url=oidc_cfg.issuer_url,
        admin_api_url=admin_api_url,
        user_id=vk_resp.user_id,
        team_id=vk_resp.team_id,
    ))
    print(vk_resp.virtual_key, flush=True)
    log.info("daemon_started", mode="oidc", check_interval=check_interval)
    current_vk = vk_resp

    while not is_shutdown_requested():
        time.sleep(check_interval)
        if is_shutdown_requested():
            break

        time_remaining = (current_vk.expires_at - datetime.now(timezone.utc)).total_seconds()
        if time_remaining > 1800:
            log.debug("vk_not_expiring", remaining_seconds=int(time_remaining))
            continue

        log.info("vk_expiring_soon", remaining_seconds=int(time_remaining))
        try:
            id_token = get_valid_id_token(oidc_cfg)  # auto-refresh OIDC token
            new_vk = exchange_jwt_for_vk(admin_api_url, id_token, device_name)
            current_vk = new_vk
            save_vk_cache(CachedVK(
                virtual_key=new_vk.virtual_key,
                expires_at=new_vk.expires_at.timestamp(),
                issuer_url=oidc_cfg.issuer_url,
                admin_api_url=admin_api_url,
                user_id=new_vk.user_id,
                team_id=new_vk.team_id,
            ))
            print(new_vk.virtual_key, flush=True)
            log.info("vk_renewed", mode="oidc", expires_at=new_vk.expires_at.isoformat())
        except (OIDCLoginError, OIDCExchangeError) as e:
            log.error("vk_renewal_failed", mode="oidc", error=str(e))

    log.info("daemon_shutdown", mode="oidc")
    return 0


# ---------------------------------------------------------------------------
# Mode auto-detection
# ---------------------------------------------------------------------------

def _detect_mode(explicit_mode: str | None) -> str:
    """Return 'oidc' or 'sts'.

    Explicit override > env auto-detect > legacy sts default.
    """
    if explicit_mode in ("oidc", "sts"):
        return explicit_mode
    if os.environ.get("OIDC_ISSUER_URL") and os.environ.get("OIDC_CLIENT_ID"):
        return "oidc"
    return "sts"


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

@click.command()
@click.option("--gateway-url", default=None, help="Admin API base URL")
@click.option("--daemon", is_flag=True, help="Run in daemon mode (BR-VK-05)")
@click.option("--check-interval", default=300, type=int, help="Daemon check interval in seconds")
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging")
@click.option(
    "--auth-mode", type=click.Choice(["auto", "oidc", "sts"]), default="auto",
    show_default=True,
    help="Auth mode override. 'auto' detects via OIDC env vars.",
)
def main(gateway_url: str | None, daemon: bool, check_interval: int, verbose: bool, auth_mode: str) -> None:
    """api-key-helper — Issue Virtual Keys via OIDC (default if configured) or AWS SSO."""
    from api_key_helper.config import load_config

    config = load_config(cli_overrides={"gateway_url": gateway_url, "verbose": verbose or None})
    configure_logging(config.verbose)
    log = structlog.get_logger(component="api-key-helper")

    mode = _detect_mode(auth_mode if auth_mode != "auto" else None)
    log.info("auth_mode_resolved", mode=mode)

    if mode == "oidc":
        if daemon:
            sys.exit(_run_oidc_daemon(config, check_interval, log))
        sys.exit(_run_oidc_normal(config, log))

    # STS mode (legacy)
    if not config.gateway_url:
        print("Gateway URL is required. Use --gateway-url or set in config.yaml.", file=sys.stderr)
        sys.exit(3)

    if daemon:
        sys.exit(_run_daemon(config, check_interval, log))
    sys.exit(_run_normal(config, log))


if __name__ == "__main__":
    main()
