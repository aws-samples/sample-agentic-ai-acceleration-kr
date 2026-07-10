# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""statusline entry point — Usage polling + one-line stdout output (PP-02, LP-02, RP-01)."""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from datetime import datetime, timezone

import click
import structlog

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
    level = 0 if verbose else 50  # 50=CRITICAL — suppress all logs in non-verbose mode
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
# VK acquisition (BR-SL-05)
# ---------------------------------------------------------------------------

def _acquire_virtual_key() -> str | None:
    """Get VK from env (Claude Code passes it) or by running api-key-helper.

    Priority: ANTHROPIC_API_KEY env (instant) > api-key-helper binary (slow)
    """
    import subprocess

    # Claude Code passes the API key to statusLine subprocess
    vk = os.environ.get("ANTHROPIC_API_KEY")
    if vk:
        return vk

    # Fallback: run api-key-helper
    try:
        result = subprocess.run(
            ["api-key-helper"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    return None


# ---------------------------------------------------------------------------
# Polling loop (RP-01)
# ---------------------------------------------------------------------------

def _run_polling(config, virtual_key: str, log) -> int:
    """Main polling loop: fetch usage → format → stdout (RP-01)."""
    from statusline.formatter import (
        Severity,
        StatuslineState,
        determine_severity,
        format_status,
    )
    from statusline.usage_client import fetch_usage

    install_signal_handlers()

    state = StatuslineState()

    log.info("polling_started", interval=config.interval)

    while not is_shutdown_requested():
        try:
            usage = fetch_usage(config, virtual_key)
            state.current = usage
            state.is_online = True
            state.last_success_at = datetime.now(timezone.utc)
            state.error_count = 0
            state.severity = determine_severity(usage.percentage, True)
        except Exception as exc:
            state.error_count += 1
            state.is_online = False
            state.severity = Severity.OFFLINE
            log.debug("fetch_failed", error=str(exc), error_count=state.error_count)

        # Output one line to stdout (BR-SL-06)
        line = format_status(state)
        print(line, flush=True)

        time.sleep(config.interval)

    log.info("polling_shutdown")
    return 0


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

@click.command()
@click.option(
    "--interval",
    default=None,
    type=int,
    help="Polling interval in seconds (10~300, default 30) (BR-SL-01)",
)
@click.option("--gateway-url", default=None, help="Gateway base URL")
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging")
def main(interval: int | None, gateway_url: str | None, verbose: bool) -> None:
    """statusline — Display LLM Gateway usage in Claude Code status bar."""
    from statusline.config import load_config

    overrides: dict = {"verbose": verbose or None, "gateway_url": gateway_url}
    if interval is not None:
        # Clamp to valid range (BR-SL-01)
        interval = max(10, min(300, interval))
        overrides["interval"] = interval

    config = load_config(cli_overrides=overrides)
    configure_logging(config.verbose)
    log = structlog.get_logger(component="statusline")

    if not config.gateway_url:
        print("Gateway URL is required. Use --gateway-url or set in config.yaml.", file=sys.stderr)
        sys.exit(1)

    virtual_key = _acquire_virtual_key()
    if not virtual_key:
        print(
            "No Virtual Key found. Set ANTHROPIC_API_KEY or run gateway-cli setup.",
            file=sys.stderr,
        )
        sys.exit(1)

    # One-shot mode: fetch once, print, exit (Claude Code calls this periodically)
    from statusline.formatter import StatuslineState, determine_severity, format_status, Severity
    from statusline.usage_client import fetch_usage

    state = StatuslineState()
    try:
        usage = fetch_usage(config, virtual_key)
        state.current = usage
        state.is_online = True
        state.severity = determine_severity(usage.percentage, True)
    except Exception:
        state.is_online = False
        state.severity = Severity.OFFLINE

    print(format_status(state), flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
