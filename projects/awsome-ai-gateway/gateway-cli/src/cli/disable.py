# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""gateway-cli disable — Remove gateway managed settings."""

from __future__ import annotations

import click
import structlog

from cli.managed import (
    _managed_file,
    find_legacy_gateway_settings,
    is_gateway_enabled,
    remove_gateway_settings,
)

log = structlog.get_logger(component="cli")


@click.command()
@click.pass_context
def disable(ctx: click.Context) -> None:
    """Disable LLM Gateway for Claude Code.

    Removes the managed settings file (50-gateway.json) from Claude Code's
    managed-settings.d:

    \b
      macOS:      /Library/Application Support/ClaudeCode/managed-settings.d/
      Linux/WSL:  /etc/claude-code/managed-settings.d/
      Windows:    C:\\Program Files\\ClaudeCode\\managed-settings.d\\

    On macOS it also removes the copy that earlier versions left under /etc,
    which Claude Code on macOS does not read. Requires sudo on
    macOS/Linux/WSL, an administrator shell on Windows.
    """
    _ = ctx.obj.get("_", lambda s: s)

    enabled = is_gateway_enabled()
    # macOS 에서 예전 위치에만 파일이 남은 상태도 지울 대상이다. 남겨 두면 disable 뒤에도
    # status 가 "setup 을 다시 실행하라" 고 안내해, 끄려던 사용자에게 정반대 지시가 된다.
    legacy = find_legacy_gateway_settings()
    if not enabled and legacy is None:
        click.echo(_("Gateway is not currently enabled."))
        return

    try:
        removed = remove_gateway_settings()
        if removed:
            click.secho("  Gateway disabled.", fg="yellow")
            if enabled:
                click.echo(f"    Removed: {_managed_file()}")
            if legacy is not None:
                click.echo(f"    Removed: {legacy}  (이전 버전이 쓰던 위치)")
            click.echo("")
            click.echo(_("Restart Claude Code to apply changes."))
            click.echo(_("Run 'gateway-cli setup' to re-enable."))
    except Exception as exc:
        raise click.ClickException(f"Failed to remove managed settings: {exc}")
