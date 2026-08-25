# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""gateway-cli env — Show and persist the 6 required environment variables.

Prints ready-to-paste shell snippets (bash/zsh + PowerShell) for:
  - OIDC_ISSUER_URL, OIDC_CLIENT_ID, ADMIN_API_URL, ANTHROPIC_BASE_URL
    (operator-provided — required)
  - REQUESTS_CA_BUNDLE, SSL_CERT_FILE
    (corporate SSL inspection bypass — optional, only shown when currently set)

With --persist:
  - POSIX: appends export lines to ~/.zshrc or ~/.bashrc (idempotent)
  - Windows: writes to HKEY_CURRENT_USER\\Environment via winreg (User scope)
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import click

from cli import manifest

# The operator-provided vars registered as permanent User-scope OS env vars.
# Single-sourced from the manifest (``os_persist=True``) so this list and
# ``cli.setup``'s persisted set cannot drift; see tests/test_os_persist_manifest_drift.py.
_OPERATOR_VARS = list(manifest.os_persisted_keys())

_SSL_VARS = [
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_FILE",
]


def _collect_vars() -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for var in _OPERATOR_VARS:
        result[var] = os.environ.get(var)
    for var in _SSL_VARS:
        val = os.environ.get(var)
        if val:
            result[var] = val
    return result


def _persist_posix(rc_path: Path, vars_to_persist: dict[str, str]) -> list[str]:
    existing_text = rc_path.read_text(encoding="utf-8") if rc_path.exists() else ""
    appended: list[str] = []
    lines_to_add: list[str] = []

    for var, val in vars_to_persist.items():
        if re.search(rf"(^|[^A-Za-z0-9_]){re.escape(var)}=", existing_text, re.M):
            continue
        lines_to_add.append(f'export {var}="{val}"')
        appended.append(var)

    if lines_to_add:
        block = "\n# LLM Gateway — added by gateway-cli env --persist\n"
        block += "\n".join(lines_to_add) + "\n"
        with rc_path.open("a", encoding="utf-8") as fh:
            fh.write(block)

    return appended


def _persist_windows(vars_to_persist: dict[str, str]) -> list[str]:
    import winreg  # type: ignore[import]

    from cli.utils.backup import backup_values

    written: list[str] = []
    # Open with QUERY too so we can read-before-write: SetValueEx replaces a value
    # in place with no undo, so we first snapshot any prior HKCU value we are about
    # to overwrite (T6.2). The snapshot lands in the shared backups dir under the
    # same timestamped policy as the settings-file backups, so a user value the
    # persist step clobbered stays recoverable. POSIX has no analogue — its rc
    # append is idempotent and never overwrites an existing line.
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        "Environment",
        0,
        winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE,
    ) as key:
        prior: dict[str, str] = {}
        for var in vars_to_persist:
            try:
                existing, _ = winreg.QueryValueEx(key, var)
            except FileNotFoundError:
                continue  # no prior value → nothing to preserve for this var
            prior[var] = existing
        backup_values("gateway-cli-hkcu-env", "Environment", prior)

        for var, val in vars_to_persist.items():
            winreg.SetValueEx(key, var, 0, winreg.REG_SZ, val)
            written.append(var)
    return written


def persist_env_vars(vars_to_persist: dict[str, str]) -> list[str]:
    """Persist ``vars_to_persist`` as permanent OS environment variables.

    Windows: HKEY_CURRENT_USER\\Environment (User scope) via winreg.
    POSIX:   appended (idempotently) to ~/.zshrc or ~/.bashrc.

    Returns the list of variable names that were newly written/updated. Shared by
    ``gateway-cli env --persist`` and ``gateway-cli setup`` (which persists by
    default). Callers handle their own error/telemetry reporting.
    """
    if not vars_to_persist:
        return []
    if sys.platform == "win32":
        return _persist_windows(vars_to_persist)
    shell = os.environ.get("SHELL", "")
    rc_path = Path.home() / (".zshrc" if "zsh" in shell else ".bashrc")
    return _persist_posix(rc_path, vars_to_persist)


@click.command("env")
@click.option(
    "--persist",
    is_flag=True,
    default=False,
    help=(
        "Permanently register vars: appends to ~/.zshrc or ~/.bashrc on POSIX; "
        "writes to HKCU\\\\Environment (User scope) on Windows."
    ),
)
@click.pass_context
def env_cmd(ctx: click.Context, persist: bool) -> None:
    """Show current gateway environment variables and print shell snippets.

    \b
    Required (operator-provided):
      OIDC_ISSUER_URL, OIDC_CLIENT_ID, ADMIN_API_URL, ANTHROPIC_BASE_URL

    \b
    Optional (corporate SSL bypass — shown only when already set):
      REQUESTS_CA_BUNDLE, SSL_CERT_FILE

    \b
    Examples:
      gateway-cli env                # show current values + snippets
      gateway-cli env --persist      # append to shell profile permanently
    """
    current = _collect_vars()

    click.echo("")
    click.echo("=== Current environment variables ===")
    for var, val in current.items():
        display = val if val else "(not set)"
        click.echo(f"  {var:<25} = {display}")
    click.echo("")

    set_vars = {k: v for k, v in current.items() if v}

    if not set_vars:
        click.secho(
            "No values set. Get them from your admin, then edit the snippets below.",
            fg="yellow",
        )
        click.echo("")

    click.echo("--- bash / zsh ---")
    for var in list(current.keys()):
        val = current[var] or "<value-from-admin>"
        click.echo(f'export {var}="{val}"')
    click.echo("")

    click.echo("--- PowerShell (User scope, permanent) ---")
    for var in list(current.keys()):
        val = current[var] or "<value-from-admin>"
        click.echo(f'[Environment]::SetEnvironmentVariable("{var}", "{val}", "User")')
    click.echo("")

    if persist:
        if not set_vars:
            click.secho(
                "Nothing to persist — no variables are currently set.",
                fg="red",
            )
            return

        if sys.platform == "win32":
            try:
                written = _persist_windows(set_vars)
                click.secho(
                    f"Saved to Windows registry (HKCU\\Environment): {', '.join(written)}",
                    fg="green",
                )
                click.echo("Restart your shell for the changes to take effect.")
            except Exception as exc:
                raise click.ClickException(f"Registry write failed: {exc}") from exc
        else:
            shell = os.environ.get("SHELL", "")
            rc_path = Path.home() / (".zshrc" if "zsh" in shell else ".bashrc")

            try:
                appended = _persist_posix(rc_path, set_vars)
            except OSError as exc:
                raise click.ClickException(f"Failed to write rc file: {exc}") from exc

            if appended:
                click.secho(
                    f"Appended to {rc_path}: {', '.join(appended)}",
                    fg="green",
                )
                click.echo(f"Run: source {rc_path}")
            else:
                click.echo(f"All variables already present in {rc_path} (nothing added).")


# ---------------------------------------------------------------------------
# NOTE — no OS-env *removal* helper lives here by design.
#
# An earlier revision had setup() sweep gateway-bypassing variables
# (CLAUDE_CODE_USE_BEDROCK, ANTHROPIC_API_KEY, …) out of the shell rc / Windows
# registry, including the machine-wide System scope. That direction was reversed:
# gateway-cli must not mutate the user's system environment variables. Such a
# variable is now DETECTED and REPORTED by ``gateway-cli verify`` (see
# ``cli.verify._check_conflicting_env``), which prints step-by-step removal
# guidance for both User and System (admin) scope so the user removes it
# themselves and can confirm the result.
# ---------------------------------------------------------------------------
