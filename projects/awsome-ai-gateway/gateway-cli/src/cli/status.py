# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""gateway-cli status — Show current gateway configuration state."""

from __future__ import annotations

import click
import structlog

from cli.managed import _managed_file, is_gateway_enabled, read_gateway_settings

log = structlog.get_logger(component="cli")


@click.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show gateway configuration status."""
    _ = ctx.obj.get("_", lambda s: s)

    enabled = is_gateway_enabled()
    settings = read_gateway_settings() if enabled else None
    managed_path = _managed_file()

    click.echo("")
    click.echo("Gateway CLI Status")
    click.echo("=" * 50)
    click.echo(f"  Managed settings: {managed_path}")
    click.echo("")

    if enabled and settings:
        click.secho("  Gateway: [ON]", fg="green", bold=True)
        env = settings.get("env", {})
        click.echo(f"    Base URL:        {env.get('ANTHROPIC_BASE_URL', '-')}")
        click.echo(f"    Admin API URL:   {env.get('GATEWAY_CLI_GATEWAY_URL', '-')}")
        click.echo(f"    API Key Helper:  {settings.get('apiKeyHelper', '-')}")
        otel = env.get('OTEL_EXPORTER_OTLP_ENDPOINT')
        if otel:
            click.echo(f"    OTEL Endpoint:   {otel}")
    else:
        click.secho("  Gateway: [OFF]", fg="red", bold=True)
        click.echo("    Claude Code uses direct API access.")

    click.echo("")
    click.echo("=" * 50)
