# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""gateway-cli entry point.

A step-by-step setup wizard for LLM Gateway end users. Endpoints and OIDC
identifiers are baked into the build (see ``cli/site_defaults.py``) and may be
overridden by ``GATEWAY_CLI_*`` env vars or flags — there is no onboarding-card
file to locate. Each step is small and independently runnable so the flow stays
transparent and resumable.

Steps:
  1. login    — OIDC login (browser PKCE, or headless email+password)   [implemented]
  2. setup    — write Claude Code user settings                         [implemented]
  3. verify   — health-check the gateway end to end                     [implemented]
"""

from __future__ import annotations

import os
import sys
import time

# Force UTF-8 I/O on Windows (cp1252 default breaks em-dashes, arrows, checkmarks).
# Must be set before any output occurs; PYTHONUTF8=1 env var has the same effect.
if sys.platform == "win32":
    os.environ.setdefault("PYTHONUTF8", "1")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import click
import structlog

from cli import __version__, teardown
from cli import uninstall as uninstall_module
from cli.env import env_cmd
from cli.login import (
    LoginStepError,
    clear_tokens,
    is_cognito_issuer,
    load_tokens,
    load_vk_cache,
    run_login,
    run_login_password,
)
from cli.managed import (
    ManagedSettingsPermissionError,
    _managed_file,
    is_gateway_enabled,
    read_gateway_settings,
    remove_gateway_settings,
)
from cli.models import (
    DEFAULT_MODEL,
    FALLBACK_MODELS,
    is_allowed_model,
    parse_available_models,
    resolve_model_roster,
)
from cli.paths import data_dir, oidc_tokens_path, vk_cache_path
from cli.setup import SetupStepError, ensure_admin_for_setup, run_setup
from cli.site_defaults import apply_ca_bundle, resolve_config
from cli.verify import CheckStatus, run_verify
from gateway_cli_oidc.tls import enable_os_trust_store

log = structlog.get_logger(component="cli")


def configure_logging(verbose: bool = False) -> None:
    """Configure structlog to stderr with JSON output (mirrors gateway-cli)."""
    level = 0 if verbose else 20  # DEBUG=0, INFO=20
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    )


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging")
@click.pass_context
def cli(ctx: click.Context, verbose: bool) -> None:
    """gateway-cli — LLM Gateway setup wizard."""
    configure_logging(verbose)
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    # Delegate TLS verification to the OS trust store (Windows SChannel) so the
    # corporate proxy CA — whose Basic Constraints are not marked critical —
    # validates under OpenSSL 3.x as it did on the old interpreter. Must run
    # before apply_ca_bundle() so the exported PEM is offered to truststore.
    enable_os_trust_store()
    # Point our own TLS stack (login/verify HTTPS, boto3) at the corporate CA
    # when the baked/overridden PEM is present on this machine. No-op otherwise.
    applied_ca = apply_ca_bundle()
    if applied_ca and verbose:
        log.info("ca_bundle_applied", path=applied_ca)


@cli.command()
def version() -> None:
    """Show gateway-cli version."""
    click.echo(f"gateway-cli {__version__}")


@cli.command("status")
@click.pass_context
def status_cmd(ctx: click.Context) -> None:
    """Show current gateway managed-settings status."""
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
        status_line = settings.get("statusLine", {})
        if status_line:
            click.echo(f"    Status line:     {status_line.get('command', '-')}")
        otel = env.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        if otel:
            click.echo(f"    OTEL Endpoint:   {otel}")
    else:
        click.secho("  Gateway: [OFF]", fg="red", bold=True)
        click.echo("    Claude Code is using direct API access.")

    click.echo("")
    click.echo("=" * 50)


@cli.command("disable")
@click.pass_context
def disable_cmd(ctx: click.Context) -> None:
    """Remove gateway managed settings (disable the gateway)."""
    if not is_gateway_enabled():
        click.echo("Gateway is not currently enabled.")
        return

    try:
        remove_gateway_settings()
    except Exception as exc:
        raise click.ClickException(f"Failed to remove managed settings: {exc}") from exc

    click.secho("Gateway disabled.", fg="yellow")
    click.echo("Restart Claude Code to apply changes.")
    click.echo("Run 'gateway-cli setup' to re-enable.")


@cli.command("config")
@click.option(
    "--explain",
    "explain_flag",
    is_flag=True,
    help="Show each config field's resolved value and winning source (secrets masked).",
)
def config_cmd(explain_flag: bool) -> None:
    """Inspect resolved gateway configuration."""
    if not explain_flag:
        click.echo("Run 'gateway-cli config --explain' to show resolved configuration.")
        return

    from urllib.parse import urlparse

    from cli.config_explain import explain, render
    from cli.resolve import Sources
    from cli.resolvers import ResolveContext, resolve_identity
    from cli.site_extra import managed_extra, user_extra

    # Flatten site-extra (managed + user, the env block and top-level keys) into a
    # single field-keyed map, matching how resolve() reads Sources.site_extra.
    site_extra: dict = {}
    for section in (managed_extra(), user_extra()):
        for key, value in section.items():
            if key == "env" and isinstance(value, dict):
                for env_key, env_value in value.items():
                    site_extra.setdefault(env_key, env_value)
            else:
                site_extra.setdefault(key, value)

    # Best-effort collector base + identity, matching setup — never fail the view.
    otel_endpoint: str | None = None
    try:
        host = urlparse(resolve_config().gateway_url.rstrip("/")).hostname
        if host:
            otel_endpoint = f"http://{host}:80"
    except Exception:  # noqa: BLE001 — a diagnostic view must not abort on config gaps
        pass
    try:
        user_id = resolve_identity()
    except Exception:  # noqa: BLE001 — identity lookup is best-effort here
        user_id = None

    sources = Sources(
        env=os.environ,
        site_extra=site_extra,
        derive_ctx=ResolveContext(otel_endpoint=otel_endpoint, user_id=user_id),
    )
    click.echo(render(explain(sources)))


cli.add_command(env_cmd)


@cli.command("login")
@click.option(
    "--redirect-port",
    default=8090,
    type=int,
    show_default=True,
    help="Local callback port for the browser redirect.",
)
@click.option(
    "--timeout",
    default=300,
    type=int,
    show_default=True,
    help="Seconds to wait for the browser callback.",
)
@click.option(
    "--no-browser",
    is_flag=True,
    default=False,
    help="Skip the browser and log in with email + password (Cognito only). "
         "Auto-enabled on headless hosts.",
)
@click.option(
    "--email",
    default=None,
    envvar="GATEWAY_CLI_EMAIL",
    help="Email for headless (--no-browser) login. Falls back to the baked/"
         "GATEWAY_CLI_EMAIL value, then an interactive prompt.",
)
@click.option(
    "--password-stdin",
    is_flag=True,
    default=False,
    help="Read the headless-login password from the first line of stdin instead of "
         "prompting. Enables unattended/CI login (e.g. `echo $PW | gateway-cli login "
         "--no-browser --email you@corp --password-stdin`).",
)
@click.pass_context
def login_cmd(
    ctx: click.Context,
    redirect_port: int,
    timeout: int,
    no_browser: bool,
    email: str | None,
    password_stdin: bool,
) -> None:
    """Step 1 — OIDC PKCE browser login.

    Opens your browser to the Cognito Hosted UI, waits for the callback, exchanges
    the auth code for OIDC tokens, and immediately exchanges the id_token for a
    Virtual Key. Both are cached in the OS-native data directory (mode 0600).

    OIDC/gateway values are baked into the build (see cli/site_defaults.py); set
    the matching GATEWAY_CLI_* env vars to override them.
    """
    # Baked corporate defaults, plus any GATEWAY_CLI_* env overrides.
    cfg = resolve_config()

    values = cfg.as_values()
    for key in ("oidcIssuerUrl", "oidcClientId"):
        if not values.get(key, "").strip():
            raise click.ClickException(
                f"missing {key} — normally baked into the build. "
                "Provide it via the matching GATEWAY_CLI_* env var, or a build that has it."
            )

    # Decide browser vs. headless email+password. Explicit --no-browser (or
    # GATEWAY_CLI_NO_BROWSER=1) forces the fallback; otherwise auto-detect a
    # missing browser. The fallback needs a Cognito issuer.
    from cli.platform import browser_available

    forced_no_browser = no_browser or os.environ.get("GATEWAY_CLI_NO_BROWSER") == "1"
    use_password = forced_no_browser or not browser_available()

    if use_password and not is_cognito_issuer(cfg.oidc_issuer_url):
        if forced_no_browser:
            raise click.ClickException(
                "--no-browser requires an AWS Cognito issuer; this IDP is not Cognito."
            )
        use_password = False  # non-Cognito + no browser: nothing we can do but try browser

    click.echo(f"  User:        {cfg.email or '—'}")
    click.echo(f"  IDP:         {cfg.oidc_issuer_url}")
    click.echo(f"  Admin API:   {cfg.admin_api_url or '—'}")
    click.echo(f"  Data dir:    {data_dir()}")
    click.echo(f"  Mode:        {'email+password (no browser)' if use_password else 'browser'}")
    click.echo("")

    # Non-interactive credential intake for headless login. --password-stdin reads a
    # single line from stdin (bypassing getpass, which cannot read piped input on a
    # headless Windows console); --email prefills the account. Both only apply on the
    # email+password path — a browser login ignores them.
    stdin_password: str | None = None
    if password_stdin:
        if not use_password:
            raise click.ClickException(
                "--password-stdin only applies to headless login; pass --no-browser "
                "(or run on a host with no browser)."
            )
        stdin_password = sys.stdin.readline().rstrip("\r\n")
        if not stdin_password:
            raise click.ClickException("--password-stdin was set but stdin gave no password.")

    try:
        if use_password:
            tokens = run_login_password(
                cfg, email=email or cfg.email or None, password=stdin_password
            )
        else:
            tokens = run_login(cfg, redirect_port=redirect_port, timeout_seconds=timeout)
    except LoginStepError as e:
        raise click.ClickException(str(e))

    ttl = int(tokens.expires_at - time.time())
    click.secho("Login successful.", fg="green", bold=True)
    click.echo(f"  Token TTL:   {ttl}s")
    click.echo(f"  Tokens:      {oidc_tokens_path()}")
    vk = load_vk_cache()
    if vk:
        vk_ttl = int(vk.expires_at - time.time())
        click.echo(f"  VK TTL:      {vk_ttl}s")
        click.echo(f"  VK cache:    {vk_cache_path()}")
    else:
        click.echo("  VK cache:    (will be created by api-key-helper)")
    click.echo("")
    click.echo("Next: run `gateway-cli setup`.")


@cli.command("logout")
@click.pass_context
def logout_cmd(ctx: click.Context) -> None:
    """Clear cached OIDC tokens and Virtual Key."""
    had_tokens = load_tokens() is not None
    had_vk = load_vk_cache() is not None
    clear_tokens()  # removes both oidc-tokens.json and vk-cache.json
    if had_tokens or had_vk:
        click.secho("Logged out — token and VK cache cleared.", fg="yellow")
    else:
        click.echo("Already logged out.")


# ── Teardown — clear (software) / uninstall (binaries) ─────────────────────
# Two-command split (docs/TEARDOWN_CLEAR_UNINSTALL_PLAN.md, mirroring the shipped
# Cowork teardown):
#   • `clear`     — revert everything software-level this tool wrote (managed
#                   settings, user settings.json keys, persisted OS env vars,
#                   tokens/VK, its own backups). In-process: every surface is a
#                   file or registry value the process does NOT hold open, so
#                   there is no self-lock. Superset of `disable` + `logout` in
#                   the one safe order, plus the backup sweep.
#   • `uninstall` — remove the binaries by delegating to the Inno uninstaller
#                   (`unins000.exe`). A running exe cannot delete its own image
#                   or the shared `_internal\` runtime, so this NEVER
#                   self-deletes — it hands off and exits (see cli.uninstall).
# Because `uninstall` deletes the exe `clear` runs from, run `clear` first
# (`uninstall --clear-first` does this in one step).
def _do_clear(*, keep_tokens: bool, keep_os_env: bool, dry_run: bool) -> bool:
    """Revert all software-level state, in the safe order. Returns overall ok.

    Order (restores BEFORE the backup sweep — the restores consume snapshots):
      1. managed-settings revert — remove_gateway_settings() (marker-scoped;
         may need admin on Windows — see the Q1(b) note below)
      2. user settings.json      — teardown.revert_user_settings()
      3. persisted OS env vars   — teardown.restore_os_env()  (unless --keep-os-env)
      4. tokens + VK             — clear_tokens()             (unless --keep-tokens)
      5. backup sweep            — teardown.sweep_backups()   (strictly LAST)

    Q1(b): the managed file lives under an admin-only path on Windows, while
    steps 2-5 are per-user state that MUST run as the real user (an elevated-
    only run under a different profile would miss them). So a permission
    failure on step 1 does not abort: the user-scope steps still run, the
    elevated one-liner to finish step 1 is printed, and False is returned so
    callers (and `uninstall --clear-first`) know the teardown is incomplete.
    """
    if dry_run:
        click.secho("Dry run — nothing will be changed.", fg="cyan", bold=True)
        click.echo("  1. remove gateway keys from managed-settings.json")
        click.echo("  2. remove/restore gateway-cli's keys in ~/.claude/settings.json")
        if keep_os_env:
            click.echo("  3. (skipped — --keep-os-env) leave persisted OS env vars")
        else:
            click.echo("  3. restore/delete the persisted OS env vars "
                       "(HKCU\\Environment or shell rc)")
        if keep_tokens:
            click.echo("  4. (skipped — --keep-tokens) leave OIDC tokens + VK cache")
        else:
            click.echo("  4. clear OIDC tokens + VK cache")
        click.echo("  5. sweep this tool's backup snapshots")
        return True

    managed_ok = True
    # 1 — managed settings (marker-scoped removal; org keys preserved).
    try:
        removed = remove_gateway_settings()
        if removed:
            click.secho("  Managed settings reverted.", fg="green")
        else:
            click.secho("  No gateway keys in managed settings.", fg="yellow")
    except (ManagedSettingsPermissionError, PermissionError, OSError) as exc:
        managed_ok = False
        click.secho(f"  Managed settings NOT reverted: {exc}", fg="red")

    # 2 — user settings.json owned keys (restores pre-setup values when the
    # earliest snapshot has them). Runs before the sweep that deletes snapshots.
    settings_result = teardown.revert_user_settings()
    if settings_result.changed:
        click.secho(
            f"  settings.json: removed {len(settings_result.removed)}, "
            f"restored {len(settings_result.restored)} key(s).",
            fg="green",
        )
    elif settings_result.skipped_reason:
        click.secho(f"  settings.json: {settings_result.skipped_reason}", fg="yellow")
    else:
        click.secho("  settings.json: no gateway-cli keys present.", fg="yellow")

    # 3 — persisted OS env vars (unless kept).
    if keep_os_env:
        click.secho("  Keeping persisted OS env vars (--keep-os-env).", fg="yellow")
    else:
        env_result = teardown.restore_os_env()
        if env_result.changed:
            detail = []
            if env_result.restored:
                detail.append(f"restored {', '.join(env_result.restored)}")
            if env_result.deleted:
                detail.append(f"deleted {', '.join(env_result.deleted)}")
            if env_result.rc_files:
                detail.append(f"cleaned {', '.join(env_result.rc_files)}")
            click.secho(f"  OS env: {'; '.join(detail)}.", fg="green")
        else:
            click.secho("  OS env: nothing persisted to revert.", fg="yellow")

    # 4 — tokens + VK cache (unless kept).
    if keep_tokens:
        click.secho("  Keeping OIDC tokens + VK cache (--keep-tokens).", fg="yellow")
    else:
        had_tokens = load_tokens() is not None or load_vk_cache() is not None
        clear_tokens()
        if had_tokens:
            click.secho("  Cleared OIDC tokens + VK cache.", fg="green")
        else:
            click.secho("  No OIDC tokens or VK cache to clear.", fg="yellow")

    # 5 — sweep our own snapshots, strictly AFTER the restores that read them.
    swept = teardown.sweep_backups()
    if swept:
        click.secho(f"  Removed {len(swept)} backup snapshot(s).", fg="green")
    else:
        click.secho("  No backup snapshots to remove.", fg="yellow")

    click.echo("")
    if managed_ok:
        click.echo("Restart Claude Code to apply the reverted configuration.")
    else:
        click.secho(
            "Teardown incomplete: the managed settings file needs elevation.\n"
            "Finish it from an ADMINISTRATOR shell with:\n"
            "    gateway-cli disable",
            fg="red",
            bold=True,
        )
    return managed_ok


@cli.command("clear")
@click.option(
    "--keep-tokens", is_flag=True, help="Don't clear OIDC tokens + VK cache (step 4)."
)
@click.option(
    "--keep-os-env",
    is_flag=True,
    help="Don't revert the persisted OS env vars (step 3).",
)
@click.option(
    "--dry-run", is_flag=True, help="Print what would be reverted; change nothing."
)
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt.")
def clear_cmd(keep_tokens: bool, keep_os_env: bool, dry_run: bool, yes: bool) -> None:
    """Revert everything software-level setup/login did — but keep the binaries.

    Undoes, in the safe order: the managed settings (like `disable`), gateway-cli's
    keys in ~/.claude/settings.json, the persisted OS env vars, the OIDC tokens +
    VK cache (like `logout`), and this tool's own backup snapshots. Runs as the
    target user; only the managed-settings step may need an elevated shell (a
    clear message tells you when). To also remove the installed binaries, run
    `uninstall` afterwards (or `uninstall --clear-first`).
    """
    if not dry_run and not yes:
        click.confirm(
            "This reverts the managed settings, settings.json keys, OS env vars, "
            "tokens/VK, and backups. Continue?",
            abort=True,
        )
    ok = _do_clear(keep_tokens=keep_tokens, keep_os_env=keep_os_env, dry_run=dry_run)
    if not ok:
        raise SystemExit(1)


@cli.command("uninstall")
@click.option(
    "--clear-first",
    is_flag=True,
    help="Run `clear` in-process first (revert settings/env/tokens/backups), then "
    "delegate the binary removal. Recommended — avoids stranding state.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Resolve and print the uninstaller path; launch nothing.",
)
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt.")
def uninstall_cmd(clear_first: bool, dry_run: bool, yes: bool) -> None:
    """Remove the installed binaries by delegating to the Inno uninstaller.

    Resolves `unins000.exe` from Add/Remove Programs and launches it elevated +
    detached (it self-copies to %TEMP%, then removes the three exes, the shared
    `_internal\\` runtime, the PATH entry, and the ARP registration). This command
    NEVER deletes its own directory — a running exe holds a lock on its image.

    It does NOT revert settings/env/tokens; that is `clear`'s job. Because
    uninstall deletes the exe `clear` runs from, use --clear-first to do both in
    the right order (clear in-process, then delegate the binary removal).
    """
    if not dry_run and not yes:
        prompt = "Remove the installed gateway-cli binaries?"
        if clear_first:
            prompt = (
                "Revert all software state AND remove the installed gateway-cli "
                "binaries?"
            )
        click.confirm(prompt, abort=True)

    if clear_first:
        # Revert software state in-process while the exe is still present, THEN
        # delegate the binary removal. A dry-run just prints both plans.
        if dry_run:
            click.secho("Dry run — would clear software state first, then:", fg="cyan")
        else:
            ok = _do_clear(keep_tokens=False, keep_os_env=False, dry_run=False)
            if not ok:
                # Removing the binaries now would strand the managed settings
                # (Claude Code still routed at a gateway whose helper is gone).
                click.secho(
                    "Aborting uninstall: finish the managed-settings revert first "
                    "(see above), then re-run `gateway-cli uninstall`.",
                    fg="red",
                    bold=True,
                )
                raise SystemExit(1)
            click.echo("")

    outcome = uninstall_module.uninstall(dry_run=dry_run)
    color = "green" if (outcome.delegated or dry_run) else "yellow"
    click.secho(f"  {outcome.detail}", fg=color)
    for warning in outcome.warnings:
        click.secho(f"  ! {warning}", fg="yellow")
    if outcome.hint:
        click.echo("")
        click.echo(outcome.hint)


def _validate_model_flag(flag: str, key: str, value: str | None) -> None:
    """F2: reject a Bedrock inference-profile id passed to a model flag.

    Routes the value through the catalogued field validator (``validate="model_alias"``)
    so ``--model`` / ``--default-*-model`` fail loudly at the command boundary. This
    is load-bearing because the emit path (``cli.emit.emit``) *encodes* a value but
    does not validate it, and setup's roster-membership check only runs when
    ``--available-models`` is supplied — so without this a ``us.anthropic.*`` id would
    land verbatim in settings and point Claude Code at a roster the Anthropic-native
    gateway path cannot route. Blank/None (an unset optional flag) is a no-op.

    Deliberately *not* a membership check: a gateway alias not in the baked fallback
    roster (e.g. a future ``claude-sonnet-5``) is still accepted, matching setup's
    documented "any alias when no --available-models" behaviour.
    """
    if value is None or not value.strip():
        return
    from cli import validators
    from cli.manifest import by_key

    try:
        validators.validate(by_key(key), value.strip())
    except validators.ValidationError as e:
        raise click.ClickException(f"{flag}: {e}")


@cli.command("setup")
@click.option(
    "--model",
    "model",
    default=DEFAULT_MODEL,
    envvar="GATEWAY_CLI_MODEL",
    show_default=True,
    help="Model alias to set as the Claude Code default. Must be a member of "
    "--available-models when that is given; otherwise one of the baked "
    f"defaults: {', '.join(FALLBACK_MODELS)}.",
)
@click.option(
    "--available-models",
    "available_models",
    default=None,
    envvar="GATEWAY_CLI_AVAILABLE_MODELS",
    help="Comma-separated list of model aliases to expose in the Claude Code "
    "picker (e.g. claude-sonnet-5,claude-haiku-4-5,claude-opus-4-8). Set at "
    "setup time so the roster changes with NO CLI rebuild. --model must be one "
    "of these. Optional — omit to leave the picker roster unset.",
)
@click.option(
    "--default-sonnet-model",
    "default_sonnet_model",
    default=None,
    envvar="GATEWAY_CLI_DEFAULT_SONNET_MODEL",
    help="Value for ANTHROPIC_DEFAULT_SONNET_MODEL in settings.json (e.g. "
    "claudecode-sonnet-5). Optional.",
)
@click.option(
    "--default-opus-model",
    "default_opus_model",
    default=None,
    envvar="GATEWAY_CLI_DEFAULT_OPUS_MODEL",
    help="Value for ANTHROPIC_DEFAULT_OPUS_MODEL in settings.json (e.g. "
    "claudecode-opus-4.8). Optional.",
)
@click.option(
    "--default-haiku-model",
    "default_haiku_model",
    default=None,
    envvar="GATEWAY_CLI_DEFAULT_HAIKU_MODEL",
    help="Value for ANTHROPIC_DEFAULT_HAIKU_MODEL in settings.json (e.g. "
    "claudecode-haiku-4.5). Optional.",
)
@click.option(
    "--gateway-url",
    default=None,
    envvar="GATEWAY_CLI_GATEWAY_PROXY_URL",
    help="Gateway proxy URL (ANTHROPIC_BASE_URL). Overrides the baked value.",
)
@click.option(
    "--admin-api-url",
    default=None,
    envvar="GATEWAY_CLI_ADMIN_API_URL",
    help="Admin API URL. Overrides the baked value.",
)
@click.option(
    "--oidc-issuer-url",
    default=None,
    envvar="GATEWAY_CLI_OIDC_ISSUER_URL",
    help="OIDC issuer URL. Overrides the baked value.",
)
@click.option(
    "--oidc-client-id",
    default=None,
    envvar="GATEWAY_CLI_OIDC_CLIENT_ID",
    help="OIDC client id. Overrides the baked value.",
)
@click.option(
    "--email",
    default=None,
    envvar="GATEWAY_CLI_EMAIL",
    help="User email (optional; recorded for later steps). Overrides the baked value.",
)
@click.option(
    "--api-key-helper",
    default=None,
    help="Path to api-key-helper binary (auto-resolved if omitted).",
)
@click.option(
    "--statusline",
    default=None,
    help="Path to statusline binary (auto-resolved if omitted).",
)
@click.option(
    "--otel-endpoint",
    default=None,
    envvar="GATEWAY_CLI_OTEL_ENDPOINT",
    help="Base OTLP collector endpoint. Highest-precedence override; when "
    "omitted, falls back to the site-extra managed.env value, then an endpoint "
    "auto-derived from the gateway host (see docs/OTEL_PRECEDENCE.md).",
)
@click.option(
    "--otel-auth-token",
    default=None,
    envvar="GATEWAY_CLI_OTEL_AUTH_TOKEN",
    help="Bearer token for the OTLP collector. Written as the "
    "OTEL_EXPORTER_OTLP_HEADERS Authorization header when supplied.",
)
@click.option(
    "--persist-env/--no-persist-env",
    "persist_env",
    default=True,
    help="Also register OIDC_ISSUER_URL, OIDC_CLIENT_ID, ADMIN_API_URL and "
    "ANTHROPIC_BASE_URL as permanent OS environment variables (Windows "
    "User-scope registry / shell rc on POSIX). On by default; pass "
    "--no-persist-env to write only Claude Code's settings files.",
)
@click.pass_context
def setup_cmd(
    ctx: click.Context,
    model: str,
    available_models: str | None,
    default_sonnet_model: str | None,
    default_opus_model: str | None,
    default_haiku_model: str | None,
    gateway_url: str | None,
    admin_api_url: str | None,
    oidc_issuer_url: str | None,
    oidc_client_id: str | None,
    email: str | None,
    api_key_helper: str | None,
    statusline: str | None,
    otel_endpoint: str | None,
    otel_auth_token: str | None,
    persist_env: bool,
) -> None:
    """Step 2 — write Claude Code settings.

    The fixed gateway values (gateway URL, admin API URL, OIDC issuer/client) are
    baked into the build; only --model is meant to be edited by the user. The
    matching --gateway-url/--admin-api-url/--oidc-* flags (or GATEWAY_CLI_* env
    vars) override individual baked values.

    Merges gateway configuration into managed settings (highest priority, admin)
    and ~/.claude/settings.json.
    """
    # Elevation preflight FIRST — before any file/registry write happens below —
    # so a non-elevated Windows shell is told to re-run as administrator up front.
    try:
        ensure_admin_for_setup()
    except SetupStepError as e:
        raise click.ClickException(str(e))

    # F2: every model flag must be a gateway alias, never a Bedrock
    # inference-profile id — the emit path encodes but does not validate, and the
    # roster gate below only fires when --available-models is supplied, so validate
    # each model flag here at the command boundary.
    _validate_model_flag("--model", "model", model)
    _validate_model_flag("--default-sonnet-model", "ANTHROPIC_DEFAULT_SONNET_MODEL", default_sonnet_model)
    _validate_model_flag("--default-opus-model", "ANTHROPIC_DEFAULT_OPUS_MODEL", default_opus_model)
    _validate_model_flag("--default-haiku-model", "ANTHROPIC_DEFAULT_HAIKU_MODEL", default_haiku_model)

    roster = parse_available_models(available_models)
    # --available-models is optional. Only when the operator supplies it do we
    # gate --model on membership; without a roster any alias is accepted as-is.
    if roster and not is_allowed_model(model, roster):
        raise click.ClickException(
            f"--model '{model}' is not in the available models. Choose one of: "
            + ", ".join(roster)
            + "."
        )

    flag_values = {
        "gatewayUrl": gateway_url,
        "adminApiUrl": admin_api_url,
        "oidcIssuerUrl": oidc_issuer_url,
        "oidcClientId": oidc_client_id,
        "email": email,
    }

    # Effective config = baked corporate defaults (+ GATEWAY_CLI_* env) < CLI flags.
    flag_overrides = {k: v for k, v in flag_values.items() if v and v.strip()}
    effective = resolve_config(flag_overrides)

    missing = effective.missing_for_setup()
    if missing:
        raise click.ClickException(
            "missing values needed for setup: "
            + ", ".join(missing)
            + ".\nThese are normally baked into the build; supply them via flags "
            "(--gateway-url, --admin-api-url, --oidc-issuer-url, --oidc-client-id) "
            "or the matching GATEWAY_CLI_* env vars."
        )

    click.echo(f"  Gateway URL:   {effective.gateway_url}")
    click.echo(f"  Admin API URL: {effective.admin_api_url}")
    click.echo(f"  OIDC issuer:   {effective.oidc_issuer_url}")
    click.echo(f"  OIDC client:   {effective.oidc_client_id}")
    click.echo(f"  Model:         {model}")
    # Only advertise a picker roster when the operator supplied one; otherwise
    # leave availableModels unset (optional) and let Claude Code use its own.
    picker = roster or None
    click.echo(f"  Models:        {', '.join(picker) if picker else '(default)'}")
    click.echo("")

    try:
        result = run_setup(
            effective,
            api_key_helper=api_key_helper,
            statusline=statusline,
            otel_endpoint=otel_endpoint,
            otel_auth_token=otel_auth_token,
            model=model,
            available_models=picker,
            default_sonnet_model=default_sonnet_model,
            default_opus_model=default_opus_model,
            default_haiku_model=default_haiku_model,
            persist_env=persist_env,
        )
    except SetupStepError as e:
        raise click.ClickException(str(e))

    # No config file is persisted for later steps — endpoints are single-sourced
    # from the build-time-baked defaults (see cli/site_defaults.py). Production
    # builds bake their endpoints, so `login`/`verify` resolve the same values
    # setup did with no saved state. Staging/dev testing overrides via
    # GATEWAY_CLI_* env vars, which persist across commands in the shell.

    click.secho("Claude Code settings updated.", fg="green", bold=True)
    click.echo(f"  Settings file: {result.settings_path}")
    click.echo(f"  Managed (highest priority): {result.managed_path}")
    # Report conflicting config we removed so the user knows what changed (and
    # that a timestamped backup was taken first). These are the keys that would
    # otherwise silently bypass the gateway even after a "successful" setup.
    if result.cleaned_settings:
        click.secho(
            f"  Removed conflicting settings.json keys: {', '.join(result.cleaned_settings)}",
            fg="yellow",
        )
    if result.persisted_env:
        scope = "HKCU\\Environment" if sys.platform == "win32" else "shell profile"
        click.echo(f"  OS env vars ({scope}): {', '.join(result.persisted_env)}")
        click.echo("  (open a new terminal for the OS env vars to take effect)")
    elif persist_env:
        click.echo("  OS env vars: already up to date")
    click.echo("")
    click.echo("Restart Claude Code to apply changes.")
    click.echo("Next: run `gateway-cli verify` to confirm everything is working.")


@cli.command("verify")
@click.option(
    "--post-teardown",
    "post_teardown",
    is_flag=True,
    help="Instead of the health check, assert every surface `clear` owns is "
    "gone/reverted (managed settings, settings.json keys, OS env vars, tokens, "
    "backups). Exits 1 on any residue.",
)
@click.pass_context
def verify_cmd(ctx: click.Context, post_teardown: bool) -> None:
    """Step 3 — end-to-end health check.

    Verifies gateway reachability, admin API reachability, OIDC token validity,
    and Claude Code settings. Reports a pass/warn/fail for each check.

    The corporate values baked into the build are used; set the matching
    GATEWAY_CLI_* env vars to override them.

    With --post-teardown, runs the inverse gate instead: after `clear` (or
    `uninstall --clear-first`) every owned surface must be clean.
    """
    if post_teardown:
        checks = teardown.post_teardown_checks()
        any_residue = False
        for name, status in checks.items():
            ok = status == "ok"
            any_residue = any_residue or not ok
            icon = "✓" if ok else "✗"
            color = "green" if ok else "red"
            click.secho(f"  [{icon}] {name} — {status}", fg=color)
        click.echo("")
        if any_residue:
            click.secho(
                "Teardown residue found (see above) — run `gateway-cli clear` "
                "(elevated shell for managed settings if flagged).",
                fg="red",
                bold=True,
            )
            raise SystemExit(1)
        click.secho("Teardown clean — no gateway-cli state remains.", fg="green", bold=True)
        return

    # Baked corporate defaults, plus any GATEWAY_CLI_* env overrides.
    cfg = resolve_config()

    outcome = run_verify(cfg)

    for check in outcome.checks:
        icon = _STATUS_ICON[check.status]
        color = _STATUS_COLOR[check.status]
        click.secho(f"  [{icon}] {check.name}", fg=color, nl=False)
        click.echo(f" — {check.detail}")

    click.echo("")
    overall = outcome.overall
    if overall == CheckStatus.OK:
        click.secho("All checks passed. Setup is complete.", fg="green", bold=True)
        click.echo("You can now run `claude` to start Claude Code with the LLM Gateway.")
    elif overall == CheckStatus.WARN:
        click.secho("Setup complete with warnings (see above).", fg="yellow", bold=True)
    else:
        click.secho("One or more checks failed (see above).", fg="red", bold=True)
        raise SystemExit(1)


_BAR = "─" * 52

_STATUS_COLOR = {
    CheckStatus.OK: "green",
    CheckStatus.WARN: "yellow",
    CheckStatus.FAIL: "red",
}
_STATUS_ICON = {
    CheckStatus.OK: "✓",
    CheckStatus.WARN: "!",
    CheckStatus.FAIL: "✗",
}


@cli.command("onboard")
@click.option(
    "--redirect-port",
    default=8090,
    type=int,
    show_default=True,
    help="Local callback port for the OIDC browser redirect.",
)
@click.option(
    "--timeout",
    default=300,
    type=int,
    show_default=True,
    help="Seconds to wait for the browser callback.",
)
@click.option(
    "--api-key-helper",
    default=None,
    help="Path to api-key-helper binary (auto-resolved if omitted).",
)
@click.option(
    "--statusline",
    default=None,
    help="Path to statusline binary (auto-resolved if omitted).",
)
@click.option(
    "--model",
    "model",
    default=DEFAULT_MODEL,
    envvar="GATEWAY_CLI_MODEL",
    show_default=True,
    help="Model alias to set as the Claude Code default. Must be a member of "
    "--available-models when that is given; otherwise one of the baked "
    f"defaults: {', '.join(FALLBACK_MODELS)}.",
)
@click.option(
    "--available-models",
    "available_models",
    default=None,
    envvar="GATEWAY_CLI_AVAILABLE_MODELS",
    help="Comma-separated model aliases to expose in the picker (set at setup "
    "time; no CLI rebuild needed). --model must be one of these.",
)
@click.pass_context
def onboard_cmd(
    ctx: click.Context,
    redirect_port: int,
    timeout: int,
    api_key_helper: str | None,
    statusline: str | None,
    model: str,
    available_models: str | None,
) -> None:
    """Run all 3 onboarding steps in one go (login > setup > verify)."""
    # Elevation preflight BEFORE step 1 (login) so a non-elevated Windows run
    # exits up front instead of writing token caches only to fail at the setup
    # write later.
    try:
        ensure_admin_for_setup()
    except SetupStepError as e:
        raise click.ClickException(str(e))

    roster = parse_available_models(available_models)
    if not is_allowed_model(model, roster or None):
        raise click.ClickException(
            f"--model '{model}' is not in the available models. Choose one of: "
            + ", ".join(resolve_model_roster(roster or None))
            + "."
        )

    def step_header(n: int, title: str) -> None:
        click.echo("")
        click.echo(_BAR)
        click.secho(f"  Step {n}/3  {title}", bold=True)
        click.echo(_BAR)

    # Baked corporate defaults, plus any GATEWAY_CLI_* env overrides.
    cfg = resolve_config()

    missing = cfg.missing_for_setup()
    if missing:
        raise click.ClickException(
            "missing values needed to onboard: "
            + ", ".join(missing)
            + ". These are normally baked into the build; provide a build that "
            "has them or set the matching GATEWAY_CLI_* env vars."
        )

    # ── Step 1: OIDC browser login ──────────────────────────────────
    step_header(1, "Browser login (OIDC)")

    click.echo(f"  IDP:        {cfg.oidc_issuer_url}")
    click.echo(f"  Admin API:  {cfg.admin_api_url or '—'}")
    click.echo(f"  Data dir:   {data_dir()}")
    click.echo("")

    try:
        tokens = run_login(cfg, redirect_port=redirect_port, timeout_seconds=timeout)
    except LoginStepError as e:
        raise click.ClickException(str(e))

    ttl = int(tokens.expires_at - time.time())
    click.secho("  ✓ Logged in.", fg="green")
    click.echo(f"    Token TTL:  {ttl}s")
    click.echo(f"    Tokens:     {oidc_tokens_path()}")
    vk = load_vk_cache()
    if vk:
        vk_ttl = int(vk.expires_at - time.time())
        click.echo(f"    VK TTL:     {vk_ttl}s")
        click.echo(f"    VK cache:   {vk_cache_path()}")

    # ── Step 2: Write Claude Code settings ─────────────────────────
    step_header(2, "Configure Claude Code")

    click.echo(f"  Gateway URL:   {cfg.gateway_url}")
    click.echo(f"  Admin API URL: {cfg.admin_api_url}")
    click.echo("")

    try:
        result = run_setup(
            cfg,
            api_key_helper=api_key_helper,
            statusline=statusline,
            model=model,
            available_models=resolve_model_roster(roster or None),
        )
    except SetupStepError as e:
        raise click.ClickException(str(e))

    click.secho("  ✓ Settings written.", fg="green")
    click.echo(f"    {result.settings_path}")
    click.echo(f"    {result.managed_path}  (managed, highest priority)")
    if result.cleaned_settings:
        click.secho(
            f"    Removed conflicting settings.json keys: {', '.join(result.cleaned_settings)}",
            fg="yellow",
        )
    if result.persisted_env:
        scope = "HKCU\\Environment" if sys.platform == "win32" else "shell profile"
        click.echo(f"    OS env vars ({scope}): {', '.join(result.persisted_env)}")

    # ── Step 3: Verify ──────────────────────────────────────────────
    step_header(3, "Verify")

    verify_outcome = run_verify(cfg)
    for check in verify_outcome.checks:
        icon = _STATUS_ICON[check.status]
        color = _STATUS_COLOR[check.status]
        click.secho(f"  [{icon}] {check.name}", fg=color, nl=False)
        click.echo(f" — {check.detail}")

    # ── Summary ─────────────────────────────────────────────────────
    click.echo("")
    click.echo(_BAR)
    overall = verify_outcome.overall
    if overall == CheckStatus.FAIL:
        click.secho("  Onboarding finished with errors (see verify above).", fg="red", bold=True)
        click.echo("  Fix the issues and re-run `gateway-cli onboard` or `gateway-cli verify`.")
        raise SystemExit(1)
    elif overall == CheckStatus.WARN:
        click.secho("  Onboarding complete (with warnings — see verify above).", fg="yellow", bold=True)
    else:
        click.secho("  Onboarding complete.", fg="green", bold=True)
    click.echo("  Restart Claude Code, then run `claude` to get started.")
    click.echo(_BAR)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
