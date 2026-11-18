# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""gateway-cli status — Show current gateway configuration state."""

from __future__ import annotations

import click
import structlog

from cli.managed import (
    _managed_file,
    find_legacy_gateway_settings,
    is_gateway_enabled,
    read_gateway_settings,
)

log = structlog.get_logger(component="cli")


@click.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show gateway configuration status."""
    _ = ctx.obj.get("_", lambda s: s)

    enabled = is_gateway_enabled()
    settings = read_gateway_settings() if enabled else None
    managed_path = _managed_file()
    # macOS 에서 이전 버전이 /etc/claude-code 에 남긴 파일. Claude Code 는 그 위치를 읽지
    # 않으므로 ON/OFF 판정에는 쓰지 않고, 남아 있다는 사실과 할 일만 알린다.
    legacy = find_legacy_gateway_settings()

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

        # OIDC 두 키가 모두 있어야 api-key-helper 가 OIDC 모드로 동작한다
        # (api_key_helper.main._detect_mode). 하나라도 없으면 STS(IAM) 로 조용히
        # 떨어져 잘못된 사용자로 VK 가 발급되거나 1P 로그인으로 되돌아가므로
        # 상태 화면에서 바로 보이게 한다.
        issuer, client = env.get("OIDC_ISSUER_URL"), env.get("OIDC_CLIENT_ID")
        if issuer and client:
            click.echo(f"    OIDC Issuer:     {issuer}")
            click.echo(f"    OIDC Client ID:  {client}")
            click.echo("    Auth Mode:       OIDC (IDP 신원 기준)")
        else:
            click.secho(
                "    Auth Mode:       STS (IAM ARN 기준) — OIDC 미설정", fg="yellow"
            )
            click.secho(
                "                     IDP 로그인을 쓰려면 'gateway-cli setup' 재실행"
                " (login 후 실행 시 자동 감지)",
                fg="yellow",
            )
        if legacy is not None:
            click.secho(f"    Legacy file:     {legacy}", fg="yellow")
            click.secho(
                "                     macOS 의 Claude Code 는 읽지 않는 위치라 무시됩니다"
                " ('gateway-cli setup' 재실행 시 삭제)",
                fg="yellow",
            )
    else:
        click.secho("  Gateway: [OFF]", fg="red", bold=True)
        click.echo("    Claude Code uses direct API access.")
        if legacy is not None:
            click.secho(f"    이전 버전이 기록한 파일이 남아 있습니다: {legacy}", fg="yellow")
            click.secho(
                "    macOS 의 Claude Code 는 이 위치를 읽지 않습니다."
                " 'gateway-cli setup' 을 다시 실행하세요.",
                fg="yellow",
            )

    click.echo("")
    click.echo("=" * 50)
