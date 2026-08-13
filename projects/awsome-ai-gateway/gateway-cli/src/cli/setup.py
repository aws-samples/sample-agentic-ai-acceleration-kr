# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""gateway-cli setup — Write gateway config to managed-settings.d."""

from __future__ import annotations

import os
from typing import Optional

import click
import structlog

from cli.config import GatewayConfig
from cli.managed import is_gateway_enabled, write_gateway_settings
from cli.tools.bedrock_config import resolve_helper_path

log = structlog.get_logger(component="cli")


def _resolve_oidc(
    issuer_url: Optional[str], client_id: Optional[str]
) -> tuple[str, str, str]:
    """Resolve (issuer_url, client_id, source) for the managed env block.

    Priority: CLI option > env var > ``gateway-cli login`` token cache. The returned
    source label describes where the *issuer* came from (it is printed on that line).

    The token cache is included because it is the one source that is guaranteed
    correct: ``login`` records the issuer/client it actually authenticated against.
    A user who ran ``login`` then ``setup`` in a fresh shell (the documented order in
    scripts/onboard-macos-linux.sh, where the env vars live only in the first shell)
    would otherwise get managed settings with no OIDC keys at all — and api-key-helper
    silently falls back to STS. Returns ("", "", "") when nothing resolves.
    """
    src = "option" if issuer_url else "env"
    issuer_url = (issuer_url or os.environ.get("OIDC_ISSUER_URL", "")).rstrip("/")
    client_id = client_id or os.environ.get("OIDC_CLIENT_ID", "")

    if not (issuer_url and client_id):
        try:
            from gateway_cli_oidc.oidc_client import load_tokens

            tokens = load_tokens()
        except Exception:  # noqa: BLE001 — cache is best-effort, never fatal
            tokens = None
        cached_issuer = (getattr(tokens, "issuer_url", "") or "").rstrip("/")
        cached_client = getattr(tokens, "client_id", "") or ""
        # The cache is only valid as an (issuer, client_id) pair. Borrowing just one
        # half builds a combination that never existed, so api-key-helper enters OIDC
        # mode and then dies in _get_valid_tokens with "cached tokens belong to a
        # different IDP" (worse than the STS fallback — no VK at all). If an issuer is
        # already given, borrow client_id only when the cache is for the same IDP.
        if cached_issuer and cached_client and issuer_url in ("", cached_issuer):
            if not issuer_url:
                issuer_url, src = cached_issuer, "login cache"
            if not client_id:
                client_id, src = cached_client, "login cache"

    if issuer_url and client_id:
        return issuer_url, client_id, src
    return "", "", ""


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
@click.option(
    "--issuer-url",
    default=None,
    help="OIDC issuer URL. Defaults to $OIDC_ISSUER_URL, then the 'gateway-cli login' cache.",
)
@click.option(
    "--client-id",
    default=None,
    help="OIDC client_id. Defaults to $OIDC_CLIENT_ID, then the 'gateway-cli login' cache.",
)
@click.option(
    "--audience",
    default=None,
    help="OIDC audience. Only needed if admin-api has OIDC_AUDIENCE verification enabled.",
)
@click.pass_context
def setup(
    ctx: click.Context,
    gateway_url: Optional[str],
    admin_api_url: Optional[str],
    api_key_helper: Optional[str],
    otel_endpoint: Optional[str],
    issuer_url: Optional[str],
    client_id: Optional[str],
    audience: Optional[str],
) -> None:
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
    oidc_issuer, oidc_client, oidc_src = _resolve_oidc(issuer_url, client_id)

    if is_gateway_enabled():
        click.echo(_("Gateway is already enabled. Updating settings..."))

    click.echo(f"  Gateway URL:     {config.gateway_url}")
    click.echo(f"  Admin API URL:   {admin_api_url}")
    click.echo(f"  API Key Helper:  {helper_path}")
    if otel:
        click.echo(f"  OTEL Endpoint:   {otel}")
    if oidc_issuer:
        click.echo(f"  OIDC Issuer:     {oidc_issuer}  (from {oidc_src})")
        click.echo(f"  OIDC Client ID:  {oidc_client}")
    else:
        # Staying silent here drops api-key-helper into STS(IAM) mode, so the VK is
        # issued against the AWS ARN instead of the IDP identity (the wrong user), or
        # with no SSO session no VK is issued at all and Claude Code falls back to the
        # 1P login screen. Warn visibly.
        click.secho(
            _("  OIDC:            (not set) — api-key-helper will run in STS(IAM) mode."),
            fg="yellow",
        )
        click.secho(
            _(
                "                   To use IDP login: run 'gateway-cli login' and retry, or\n"
                "                   pass --issuer-url / --client-id."
            ),
            fg="yellow",
        )
    click.echo("")

    try:
        path = write_gateway_settings(
            gateway_url=config.gateway_url,
            admin_api_url=admin_api_url,
            api_key_helper_path=helper_path,
            otel_endpoint=otel,
            otel_auth_token=config.otel_auth_token or None,
            oidc_issuer_url=oidc_issuer or None,
            oidc_client_id=oidc_client or None,
            oidc_audience=audience or os.environ.get("OIDC_AUDIENCE") or None,
        )
        click.secho(f"  Gateway enabled: {path}", fg="green")
        click.echo("")
        click.echo(_("Restart Claude Code to apply changes."))
    except Exception as exc:
        raise click.ClickException(f"Failed to write managed settings: {exc}")
