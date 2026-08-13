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

        # api-key-helper only runs in OIDC mode when BOTH OIDC keys are present
        # (api_key_helper.main._detect_mode). With either one missing it silently
        # falls back to STS(IAM), so the VK is issued for the wrong user or Claude
        # Code drops back to first-party login — surface the mode here in status.
        issuer, client = env.get("OIDC_ISSUER_URL"), env.get("OIDC_CLIENT_ID")
        if issuer and client:
            click.echo(f"    OIDC Issuer:     {issuer}")
            click.echo(f"    OIDC Client ID:  {client}")
            click.echo(_("    Auth Mode:       OIDC (IDP identity)"))
        else:
            click.secho(
                _("    Auth Mode:       STS (IAM ARN identity) — OIDC not configured"),
                fg="yellow",
            )
            click.secho(
                _(
                    "                     To use IDP login, re-run 'gateway-cli setup'"
                    " (auto-detected when run after login)"
                ),
                fg="yellow",
            )
    else:
        click.secho("  Gateway: [OFF]", fg="red", bold=True)
        click.echo("    Claude Code uses direct API access.")

    click.echo("")
    click.echo("=" * 50)
