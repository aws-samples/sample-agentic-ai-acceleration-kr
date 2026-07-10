# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""gateway-cli login / logout — OIDC PKCE Authorization Code flow.

OIDC 환경변수 (또는 옵션) 로 IDP 정보 받아 브라우저 PKCE flow.
토큰은 ``~/.gateway-cli/oidc-tokens.json`` (mode 0600) 에 저장.
"""
from __future__ import annotations

import sys
from typing import Optional

import click
import structlog

from gateway_cli_oidc.oidc_client import (
    OIDCConfig,
    OIDCLoginError,
    clear_tokens,
    clear_vk_cache,
    load_oidc_config_from_env,
    load_tokens,
    login_pkce,
)

log = structlog.get_logger(component="cli")


def _resolve_config(
    issuer_url: Optional[str],
    client_id: Optional[str],
    audience: Optional[str],
    redirect_port: int,
) -> OIDCConfig:
    """CLI 옵션 > 환경변수 우선순위로 OIDCConfig 결정."""
    cfg = load_oidc_config_from_env()
    if cfg is None:
        cfg = OIDCConfig(issuer_url="", client_id="", audience="", redirect_port=redirect_port)
    if issuer_url:
        cfg.issuer_url = issuer_url.rstrip("/")
    if client_id:
        cfg.client_id = client_id
    if audience is not None:
        cfg.audience = audience
    if redirect_port:
        cfg.redirect_port = redirect_port

    missing = []
    if not cfg.issuer_url:
        missing.append("OIDC_ISSUER_URL (or --issuer-url)")
    if not cfg.client_id:
        missing.append("OIDC_CLIENT_ID (or --client-id)")
    if missing:
        raise click.ClickException(
            "Missing required OIDC config: " + ", ".join(missing)
        )
    return cfg


@click.command()
@click.option("--issuer-url", default=None, help="OIDC issuer URL (override env OIDC_ISSUER_URL)")
@click.option("--client-id", default=None, help="OIDC client_id (override env OIDC_CLIENT_ID)")
@click.option("--audience", default=None, help="OIDC audience (override env OIDC_AUDIENCE)")
@click.option(
    "--redirect-port", default=8090, type=int, show_default=True,
    help="Local browser callback port",
)
@click.option(
    "--timeout", default=300, type=int, show_default=True,
    help="Browser flow timeout in seconds",
)
def login(issuer_url, client_id, audience, redirect_port, timeout) -> None:
    """OIDC 로그인 (PKCE Authorization Code flow + browser).

    토큰을 ~/.gateway-cli/oidc-tokens.json (mode 0600) 에 캐시.
    """
    cfg = _resolve_config(issuer_url, client_id, audience, redirect_port)

    try:
        tokens = login_pkce(cfg, timeout_seconds=timeout)
    except OIDCLoginError as e:
        click.echo(f"Login failed: {e}", err=True)
        sys.exit(1)

    click.echo("Login successful.", err=True)
    click.echo(f"  IDP:       {cfg.issuer_url}", err=True)
    click.echo(f"  Client ID: {cfg.client_id}", err=True)
    click.echo(f"  Token TTL: {int(tokens.expires_at - __import__('time').time())}s", err=True)
    if tokens.refresh_token:
        click.echo("  Refresh:   yes (auto-refresh enabled)", err=True)
    else:
        click.echo("  Refresh:   no (re-login required after expiry)", err=True)


@click.command()
def logout() -> None:
    """OIDC 토큰 + VK 캐시 모두 삭제."""
    existed = load_tokens() is not None
    clear_tokens()
    clear_vk_cache()
    if existed:
        click.echo("Logged out — token + VK cache cleared.", err=True)
    else:
        click.echo("Already logged out.", err=True)
