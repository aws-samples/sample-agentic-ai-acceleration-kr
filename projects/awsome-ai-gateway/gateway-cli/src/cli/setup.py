# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""gateway-cli setup — Write gateway config to managed-settings.d."""

from __future__ import annotations

from typing import Optional

import click
import structlog

from cli.config import GatewayConfig
from cli.managed import is_gateway_enabled, write_gateway_settings
from cli.tools.bedrock_config import resolve_helper_path

log = structlog.get_logger(component="cli")


@click.command()
@click.option(
    "--gateway-url",
    default=None,
    help="Gateway proxy URL for API calls (e.g. http://gateway:8000)",
)
@click.option(
    "--admin-api-url",
    default=None,
    help="Admin API URL for VK issuance (e.g. http://admin-api:8080). Defaults to gateway-url with port 8080.",
)
@click.option(
    "--api-key-helper",
    default=None,
    help="Path to api-key-helper binary (auto-resolved if omitted)",
)
@click.option(
    "--otel-endpoint",
    default=None,
    help="OpenTelemetry collector endpoint (e.g. http://otel-collector:4317)",
)
@click.pass_context
def setup(ctx: click.Context, gateway_url: Optional[str], admin_api_url: Optional[str], api_key_helper: Optional[str], otel_endpoint: Optional[str]) -> None:
    """Enable LLM Gateway for Claude Code.

    Writes managed settings to /etc/claude-code/managed-settings.d/
    which takes highest priority in Claude Code's config hierarchy.
    Requires sudo on Linux/WSL.
    """
    _ = ctx.obj.get("_", lambda s: s)

    config: GatewayConfig = ctx.obj["config"]
    if gateway_url:
        config.gateway_url = gateway_url

    if not config.gateway_url:
        raise click.ClickException(
            _("Gateway URL is required. Use --gateway-url or set in config.yaml.")
        )

    # Derive admin-api URL from gateway URL if not specified
    # Convention: gateway-proxy on :8000, admin-api on :8080
    if not admin_api_url:
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(config.gateway_url)
        admin_api_url = urlunparse(parsed._replace(netloc=f"{parsed.hostname}:8080"))

    # Derive OTEL endpoint — Node.js OTEL SDK requires http:// even for gRPC
    if not otel_endpoint and not config.otel_endpoint:
        from urllib.parse import urlparse
        p = urlparse(config.gateway_url)
        otel_endpoint = f"http://{p.hostname}:4317"
    otel = otel_endpoint or config.otel_endpoint or None
    helper_path = api_key_helper or resolve_helper_path()

    if is_gateway_enabled():
        click.echo(_("Gateway is already enabled. Updating settings..."))

    click.echo(f"  Gateway URL:     {config.gateway_url}")
    click.echo(f"  Admin API URL:   {admin_api_url}")
    click.echo(f"  API Key Helper:  {helper_path}")
    if otel:
        click.echo(f"  OTEL Endpoint:   {otel}")
    click.echo("")

    try:
        path = write_gateway_settings(
            gateway_url=config.gateway_url,
            admin_api_url=admin_api_url,
            api_key_helper_path=helper_path,
            otel_endpoint=otel,
            otel_auth_token=config.otel_auth_token or None,
        )
        click.secho(f"  Gateway enabled: {path}", fg="green")
        click.echo("")
        click.echo(_("Restart Claude Code to apply changes."))
    except Exception as exc:
        raise click.ClickException(f"Failed to write managed settings: {exc}")
