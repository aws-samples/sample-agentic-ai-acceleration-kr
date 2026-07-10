# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""gateway-cli disable — Remove gateway managed settings."""

from __future__ import annotations

import click
import structlog

from cli.managed import is_gateway_enabled, remove_gateway_settings

log = structlog.get_logger(component="cli")


@click.command()
@click.pass_context
def disable(ctx: click.Context) -> None:
    """Disable LLM Gateway for Claude Code.

    Removes the managed settings file from /etc/claude-code/managed-settings.d/.
    Requires sudo on Linux/WSL.
    """
    _ = ctx.obj.get("_", lambda s: s)

    if not is_gateway_enabled():
        click.echo(_("Gateway is not currently enabled."))
        return

    try:
        removed = remove_gateway_settings()
        if removed:
            click.secho("  Gateway disabled.", fg="yellow")
            click.echo("")
            click.echo(_("Restart Claude Code to apply changes."))
            click.echo(_("Run 'gateway-cli setup' to re-enable."))
    except Exception as exc:
        raise click.ClickException(f"Failed to remove managed settings: {exc}")
