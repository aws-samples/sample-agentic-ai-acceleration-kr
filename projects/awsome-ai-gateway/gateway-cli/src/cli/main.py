# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Gateway CLI entry point — Click group with structlog (PP-02)."""

from __future__ import annotations

import gettext
import os
import sys

import click
import structlog

# ---------------------------------------------------------------------------
# Logging setup (PP-02 — independent structlog per binary)
# ---------------------------------------------------------------------------

SENSITIVE_KEYS = {"sts_request", "virtual_key", "jwt_token", "otel_auth_token"}


def _mask_sensitive(logger, method_name, event_dict):
    """Mask sensitive fields in structured log output (SP-02)."""
    for key in SENSITIVE_KEYS:
        if key in event_dict:
            event_dict[key] = "***MASKED***"
    return event_dict


def configure_logging(verbose: bool = False) -> None:
    """Configure structlog to stderr with JSON output (PP-02)."""
    level = 0 if verbose else 20  # DEBUG=0, INFO=20
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
# i18n setup
# ---------------------------------------------------------------------------

def _setup_i18n(lang: str = "en") -> gettext.GNUTranslations:
    """Initialize gettext translations."""
    localedir = os.path.join(os.path.dirname(__file__), "..", "..", "locales")
    if getattr(sys, "frozen", False):
        localedir = os.path.join(sys._MEIPASS, "locales")
    return gettext.translation(
        "gateway-cli", localedir=localedir, languages=[lang], fallback=True
    )


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------

@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging")
@click.option("--lang", default=None, help="Language (en/ko)")
@click.pass_context
def cli(ctx: click.Context, verbose: bool, lang: str | None) -> None:
    """Gateway CLI — Integrated onboarding tool for AI development tools."""
    from cli.config import load_config

    config = load_config(cli_overrides={"verbose": verbose or None, "lang": lang})
    configure_logging(config.verbose)
    ctx.ensure_object(dict)
    ctx.obj["config"] = config

    t = _setup_i18n(config.lang)
    ctx.obj["_"] = t.gettext


@cli.command()
def version() -> None:
    """Show Gateway CLI version."""
    click.echo("gateway-cli 0.1.0")


# Register commands
from cli.setup import setup  # noqa: E402
from cli.disable import disable  # noqa: E402
from cli.status import status  # noqa: E402
from cli.login import login, logout  # noqa: E402

cli.add_command(setup)
cli.add_command(disable)
cli.add_command(status)
cli.add_command(login)
cli.add_command(logout)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
