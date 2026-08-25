# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""gateway-cli entry point.

A step-by-step setup wizard for LLM Gateway end users, driven entirely by the
corporate values baked into the build (:mod:`cli.site_defaults`). Each step is
small and independently runnable so the flow stays transparent and resumable.

This installer targets **Cowork (Claude Desktop)**. The primary flow authenticates
the user (login, shared OIDC) and then points Claude Desktop at the gateway
via its OS-native managed config (Windows registry policy / macOS configLibrary):

  1. login     — OIDC PKCE browser login
  2. setup     — trust the corporate CA in the OS store (egress-proxy TLS) AND
                 write the Cowork managed config, then relaunch Claude Desktop
  3. verify    — health-check the Cowork gateway setup end to end (also prints
                 the current managed config)

`setup` folds in the corporate-CA install (a no-op when no PEM is present, e.g.
off the corporate network); `verify` folds in the config dump.
See docs/cowork-native-conversion-plan.csv.
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

from cli import (
    __version__,
    cowork_app,
    cowork_ca,
    cowork_config,
    cowork_uninstall,
    elevation,
)
from cli.login import (
    LoginStepError,
    clear_tokens,
    is_cognito_issuer,
    load_tokens,
    load_vk_cache,
    run_login,
    run_login_password,
)
from cli.models import (
    FALLBACK_MODELS,
    is_allowed_model,
    parse_available_models,
)
from cli.paths import (
    _resolve_api_key_helper,
    data_dir,
    oidc_tokens_path,
    prog_name as _prog,
    vk_cache_path,
)
from cli.platform import browser_available
from cli.site_defaults import (
    admin_api_url,
    apply_ca_bundle,
    oidc_client_id,
    oidc_issuer_url,
)
from cli.utils.rollback import Rollback
from cli.verify import CheckStatus, run_cowork_verify
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
    """gateway-cli — Cowork (Claude Desktop) gateway setup wizard."""
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
    click.echo(f"{_prog()} {__version__}")


# Cowork is Claude *Desktop* and reads its managed config ONLY at launch, so a
# config change (model list, base URL, credential kind) needs a full quit +
# relaunch to take effect. Credential rotation (the VK/token the api-key-helper
# returns) does NOT — the running app re-invokes the helper on its own schedule.
@cli.command("relaunch")
@click.option(
    "--no-relaunch",
    is_flag=True,
    help="Don't relaunch; just print the manual quit+relaunch steps.",
)
@click.pass_context
def relaunch_cmd(ctx: click.Context, no_relaunch: bool) -> None:
    """Force-quit and relaunch Claude Desktop (Cowork) so config changes apply.

    Windows launches the MSIX app by its AUMID via shell:AppsFolder; macOS quits
    then reopens Claude. Refuses to run under SYSTEM/root (can't drive a per-user
    GUI app) and honours COWORK_NO_RELAUNCH=1.
    """
    _relaunch_after_config(no_relaunch=no_relaunch)


# Cowork is an Electron/Chromium app that validates TLS against the OS trust
# store, not NODE_EXTRA_CA_CERTS. On the corporate network a TLS-intercepting
# egress proxy re-signs downloads + telemetry with a corporate CA, so that CA
# must be in the OS store for the app to connect. (The inference URL is publicly
# trusted — the corporate CA is irrelevant there.)
@cli.group("ca")
def ca_group() -> None:
    """Inspect / undo the corporate TLS CA trust (install is part of `setup`).

    `setup` installs the corporate CA into the OS trust store as its first step.
    These subcommands are the read-only check and the undo:
      • `ca check`   — is the corporate CA currently trusted? (read-only)
      • `ca restore` — remove ONLY the CA that `setup` installed.
    """


def _print_ca_result(result: cowork_ca.CaResult) -> None:
    color = "green" if result.ok else "red"
    click.secho(f"  {result.detail}", fg=color)
    for warning in result.warnings:
        click.secho(f"  ! {warning}", fg="yellow")


@ca_group.command("check")
def ca_check_cmd() -> None:
    """Report whether the corporate CA is trusted by the OS store (read-only)."""
    result = cowork_ca.check()
    _print_ca_result(result)
    if not result.ok:
        raise SystemExit(1)


@ca_group.command("restore")
def ca_restore_cmd() -> None:
    """Remove ONLY the CA that `setup` installed (per its recorded backup)."""
    result = cowork_ca.restore()
    _print_ca_result(result)
    if not result.ok:
        raise SystemExit(1)


# Cowork is configured through an OS-native channel (Windows registry policy /
# macOS configLibrary), NOT Claude Code's settings.json. `setup` writes the
# gateway keys to that channel (installing the corporate CA first) and relaunches
# the app so the config takes effect (Desktop reads config only at launch).
# `verify` health-checks that setup and prints the live config.
def _print_config_result(result: cowork_config.ConfigResult) -> None:
    color = "green" if result.ok else "red"
    click.secho(f"  {result.detail}", fg=color)
    for warning in result.warnings:
        click.secho(f"  WARNING: {warning}", fg="yellow")


def _print_cowork_config() -> None:
    """Print the current Cowork managed config (secret-safe), if any.

    Shared by `setup` (post-write summary) and `verify` (the config dump that was
    formerly the separate `cowork status` command).
    """
    if cowork_config.hklm_conflict():
        click.secho(
            "  WARNING: HKLM\\SOFTWARE\\Policies\\Claude has values — the app "
            "ignores HKCU entirely; this user policy will not take effect.",
            fg="yellow",
        )
    # Show WHERE the config lives — the registry policy key on Windows, the
    # configLibrary JSON on macOS. The Cowork equivalent of the settings.json
    # path Claude Code prints.
    click.echo(f"  channel: {cowork_config.config_location()}")
    current = cowork_config.read_config()
    if not current:
        click.secho("  Cowork is not configured for the gateway.", fg="yellow")
        return
    for name in sorted(current):
        value = current[name]
        if name == "inferenceGatewayApiKey" and value:
            value = f"{str(value)[:12]}…(len {len(str(value))})"
        click.echo(f"    {name:<28} = {value}")
    if current.get("inferenceProvider") == "gateway":
        click.secho("  provider=gateway (3P mode configured)", fg="green")


def _install_corporate_ca(rb: Rollback, *, force: bool) -> None:
    """Install the corporate CA into the OS trust store as part of `setup`.

    A no-op (with a short note) when no CA PEM is configured/present — e.g. on a
    clean box off the corporate network, where the public inference URL needs no
    corporate CA. When a PEM *is* present, a failure (chiefly a fingerprint-pin
    mismatch) aborts setup before any config is written, so the user gets a clean
    retry rather than a half-configured app pointed through an untrusted proxy.
    Pass --force to override a pin mismatch for a legitimate CA rotation.

    Uses the invocation-scoped plan/apply/compensate primitive: the compensation
    is armed on ``rb`` BEFORE the install mutates the trust store, so a later
    config-write failure rolls the CA back — but only if THIS run installed it
    (a CA that was already trusted is left alone). See
    docs/cowork-setup-rollback-design.md §2/§3.
    """
    if cowork_ca.resolve_pem() is None:
        click.secho(
            "  No corporate CA PEM present — skipping CA install.",
            fg="yellow",
        )
        return
    plan = cowork_ca.plan_install(force=force)
    # Arm BEFORE apply mutates the store (§8 #2). compensate() is a no-op unless
    # this run actually added the cert (§8 #3), so arming unconditionally is safe.
    rb.arm("corporate CA", plan.compensate)
    result = plan.apply()
    _print_ca_result(result)
    if not result.ok:
        raise click.ClickException(
            "corporate CA install failed (see above) — aborting setup before "
            "writing config. Resolve the CA problem, or re-run with --force for a "
            "legitimate CA rotation."
        )


@cli.command("setup")
@click.option(
    "--base-url",
    default=None,
    envvar="GATEWAY_CLI_COWORK_GATEWAY_URL",
    help="Cowork inference base URL (inferenceGatewayBaseUrl). Must be HTTPS "
    "(CloudFront). Omit to use the build-time-baked COWORK_GATEWAY_URL.",
)
@click.option(
    "--model",
    "model",
    default=None,
    envvar="GATEWAY_CLI_MODEL",
    help="Model alias to set as the Cowork default (first inferenceModels entry). "
    f"Omit to use the baked default: {', '.join(FALLBACK_MODELS)}.",
)
@click.option(
    "--available-models",
    "available_models",
    default=None,
    envvar="GATEWAY_CLI_AVAILABLE_MODELS",
    help="Comma-separated inferenceModels roster (e.g. cowork-opus). Each alias "
    "must be registered on the gateway. --model must be one of these. Omit to "
    "use the baked default roster.",
)
@click.option(
    "--credential-kind",
    type=click.Choice(["helper-script", "static"]),
    default="helper-script",
    help="helper-script (production; app auto-refreshes the VK via the helper) or "
    "static (a concrete VK written into the config; expires ~1h).",
)
@click.option(
    "--api-key-helper",
    "api_key_helper",
    default=None,
    help="Path to the api-key-helper binary (helper-script kind; auto-resolved "
    "if omitted).",
)
@click.option(
    "--api-key",
    "api_key",
    default=None,
    envvar="GATEWAY_CLI_VK",
    help="Virtual Key to write (static kind).",
)
@click.option(
    "--force",
    is_flag=True,
    help="Install the corporate CA even if its fingerprint does not match the "
    "build-time pin (use only for a legitimate CA rotation).",
)
@click.option(
    "--skip-ca",
    is_flag=True,
    help="Skip the corporate-CA install step (config-only setup). Use when the CA "
    "is already trusted or you are off the corporate egress proxy.",
)
@click.option(
    "--scope",
    "scope",
    type=click.Choice(["user", "machine"]),
    default=None,
    help="Windows registry scope: 'user' writes HKCU (per-user, default), 'machine' "
    "writes HKLM (all users, needs admin; overrides per-user policies). Omit to use "
    "the choice recorded by the installer, else 'user'. Ignored on macOS.",
)
@click.option(
    "--no-relaunch",
    is_flag=True,
    help="Write the config but don't relaunch Claude Desktop (print manual steps).",
)
@click.option(
    "--elevate/--no-elevate",
    "elevate",
    default=True,
    help="Windows only: when run non-elevated by a local administrator, request "
    "admin rights via a UAC consent prompt and re-run setup elevated (the config "
    "write needs elevation). --no-elevate keeps the current behaviour (fail with a "
    "'run as administrator' hint) — use it for CI/scripts. Ignored on macOS, when "
    "already elevated, or for a genuine standard user (whose UAC prompt would run "
    "as a different admin and write the wrong per-user hive).",
)
def setup_cmd(
    base_url: str | None,
    model: str | None,
    available_models: str | None,
    credential_kind: str,
    api_key_helper: str | None,
    api_key: str | None,
    force: bool,
    skip_ca: bool,
    scope: str | None,
    no_relaunch: bool,
    elevate: bool,
) -> None:
    """Point Claude Desktop (Cowork) at the gateway, then relaunch it.

    Runs in two steps: (1) install the corporate TLS CA into the OS trust store
    (skipped when no PEM is present, or with --skip-ca), then (2) write the managed
    config to the OS-native channel (Windows registry policy / macOS configLibrary),
    preserving any pre-existing org values, and force-quit + relaunch the app so the
    change takes effect. Credential rotation alone does NOT need a relaunch — only a
    config change does.
    """
    # UAC auto-elevation (Windows only). The config write needs an elevated token.
    # When a local admin runs setup non-elevated we relaunch THIS exe with the same
    # arguments behind a UAC *consent* prompt (the SID — and thus the HKCU hive — is
    # preserved), wait for the elevated child, and exit with its code. This is gated
    # to consent-elevatable admins on the packaged exe (see cli.elevation): a genuine
    # standard user, an already-elevated process, a dev checkout, or macOS all fall
    # through to run in-process, so a standard user still sees the actionable
    # "run as administrator" message rather than a wrong-hive credential prompt.
    if elevate and elevation.should_auto_elevate():
        click.secho(
            "Administrator rights are required to write the managed config — "
            "requesting elevation (UAC)…",
            fg="cyan",
        )
        file, params = elevation.build_relaunch_params(
            sys.argv, sys.executable, getattr(sys, "frozen", False)
        )
        try:
            code = elevation.relaunch_elevated_and_wait(file, params)
        except elevation.ElevationCancelled:
            raise click.ClickException(
                "Elevation was cancelled at the UAC prompt. Re-run setup from an "
                "elevated command prompt (right-click → \"Run as administrator\"), "
                "or pass --no-elevate."
            ) from None
        except elevation.ElevationError as exc:
            raise click.ClickException(
                f"Could not elevate automatically: {exc}. Re-run setup from an "
                "elevated command prompt (right-click → \"Run as administrator\")."
            ) from None
        click.secho(
            f"Elevated setup finished (exit code {code}). See the elevated window "
            "above for its output.",
            fg="cyan",
        )
        sys.exit(code)

    # setup mutates two independent surfaces (CA trust store, then the managed
    # config). A failure after the CA is installed but before the config write
    # succeeds would leave a partial state, so both mutations run inside an
    # invocation-scoped LIFO rollback saga: each step arms its compensation BEFORE
    # its first live write, and any exception unwinds the armed steps in reverse
    # (best-effort, reverting only THIS run's delta). On success rb.commit()
    # discards the stack. See docs/cowork-setup-rollback-design.md.
    rb = Rollback()
    try:
        with rb:
            # Step 1 — corporate CA into the OS trust store (Cowork is Chromium; it
            # validates TLS against the OS store, not env vars). Done first so a pin
            # mismatch aborts before we write any config.
            if skip_ca:
                click.secho("  Skipping corporate-CA install (--skip-ca).", fg="yellow")
            else:
                _install_corporate_ca(rb, force=force)
            click.echo("")

            # Step 2 — resolve the roster (identical --model gating to the old flow).
            roster = parse_available_models(available_models)
            if model and roster and not is_allowed_model(model, roster):
                raise click.ClickException(
                    f"--model '{model}' is not in the available models. Choose one of: "
                    + ", ".join(roster)
                    + "."
                )
            models = roster or None
            if model:
                # Put the chosen model first (the default) without dropping the roster.
                models = [model] + [m for m in (models or []) if m != model]

            # For helper-script, resolve the helper path next to this exe.
            helper_path = api_key_helper
            if credential_kind == "helper-script" and not helper_path:
                helper_path = _resolve_api_key_helper()

            # Resolve the Windows registry scope: --scope flag > installer choice >
            # per-user default. Inert on macOS (the configLibrary writer ignores it).
            resolved_scope = cowork_config.resolve_registry_scope(scope)

            try:
                config = cowork_config.build_config(
                    base_url=base_url,
                    models=models,
                    credential_kind=credential_kind,
                    helper_path=helper_path,
                    api_key=api_key,
                    registry_scope=resolved_scope,
                )
            except ValueError as e:
                raise click.ClickException(str(e))

            click.echo(f"  Base URL:       {config.base_url}")
            click.echo(f"  Models:         {', '.join(config.models)}")
            click.echo(f"  Credential:     {config.credential_kind}")
            if config.credential_kind == "helper-script":
                click.echo(f"  Helper:         {config.helper_path}")
            if sys.platform == "win32":
                _scope_label = (
                    "HKLM (machine-wide, all users)"
                    if resolved_scope == "machine"
                    else "HKCU (per-user)"
                )
                click.echo(f"  Registry scope: {_scope_label}")
            click.echo("")

            # Snapshot this run's starting state and arm the config restore BEFORE
            # the write mutates anything (write_config sets registry values in a
            # non-atomic loop). The compensation reverts to THIS run's pre-write
            # snapshot — not the first-setup baseline remove_config would use.
            cfg_plan = cowork_config.plan_write(config)
            rb.arm("managed config", cfg_plan.compensate)
            result = cfg_plan.apply()
            _print_config_result(result)
            if not result.ok:
                # The armed compensation already covers any partial write.
                raise click.ClickException(result.detail)

            # setup succeeded — keep the mutations; the stack will not unwind.
            rb.commit()

            # A config change needs a relaunch to take effect (Desktop reads config
            # only at launch). Relaunch is non-fatal and NOT rolled back (a valid
            # config that hasn't been picked up yet must not be reverted), so it
            # runs AFTER commit(). Skip it if nothing actually changed.
            if result.changed:
                click.echo("")
                _relaunch_after_config(no_relaunch=no_relaunch)
    except Exception:
        # The saga already unwound this run's mutations; report what was rolled
        # back as secondary context, then re-raise so the original failure remains
        # the primary error + exit status.
        _print_rollback_summary(rb)
        raise

    click.echo("")
    click.echo("Next: open the Cowork tab, pick the model, and start a session.")
    click.echo(f"Then run `{_prog()} verify` to confirm everything is working.")


@cli.command("disable")
@click.option(
    "--no-relaunch",
    is_flag=True,
    help="Revert the config but don't relaunch Claude Desktop.",
)
def disable_cmd(no_relaunch: bool) -> None:
    """Revert the Cowork config to its exact state before setup wrote it.

    Removes only the values this tool added and restores any it overwrote; a
    pre-existing org policy is left untouched. Does NOT remove the corporate CA —
    use `gateway-cli ca restore` for that.
    """
    result = cowork_config.remove_config()
    _print_config_result(result)
    if not result.ok:
        raise SystemExit(1)
    if result.changed:
        click.echo("")
        _relaunch_after_config(no_relaunch=no_relaunch)


# Two-command teardown split (docs/cowork-uninstall-implementation-plan.md):
#   • `clear`     — revert everything software-level this tool touched (config,
#                   CA, tokens/VK, its own markers/backups). Per-user, no
#                   elevation, in-process — every surface is a file or registry
#                   value the process does NOT hold open, so there is no
#                   self-lock. Superset of `disable` + `ca restore` + `logout`.
#   • `uninstall` — remove the binaries by delegating to the Inno uninstaller
#                   (`unins000.exe`). A running exe cannot delete its own image
#                   or the shared `_internal\` runtime, so this NEVER self-deletes
#                   — it hands off and exits (see cowork_uninstall).
# Because `uninstall` deletes the exe `clear` runs from, run `clear` first
# (`uninstall --clear-first` does this in one step).
def _do_clear(
    *,
    keep_ca: bool,
    keep_tokens: bool,
    dry_run: bool,
    no_relaunch: bool,
) -> None:
    """Revert all software-level state this tool created, in the safe order.

    Order (config revert BEFORE the marker sweep, so a rollback snapshot is never
    destroyed while still needed):
      1. config revert  — cowork_config.remove_config()   (consumes the marker)
      2. CA restore      — cowork_ca.restore()             (unless --keep-ca)
      3. tokens + VK      — clear_tokens()                  (unless --keep-tokens)
      4. marker + backup sweep — cowork_config.sweep_backups()  (AFTER step 1)

    dry_run prints the plan and touches nothing. Shared by the `clear` command
    and `uninstall --clear-first`.
    """
    if dry_run:
        click.secho("Dry run — nothing will be changed.", fg="cyan", bold=True)
        click.echo("  1. revert managed config to its pre-setup state")
        if keep_ca:
            click.echo("  2. (skipped — --keep-ca) leave the corporate CA trusted")
        else:
            click.echo("  2. remove ONLY the corporate CA that setup installed")
        if keep_tokens:
            click.echo("  3. (skipped — --keep-tokens) leave OIDC tokens + VK cache")
        else:
            click.echo("  3. clear OIDC tokens + VK cache")
        marker = "present" if cowork_config.marker_exists() else "absent"
        click.echo(f"  4. sweep this tool's markers + backups (marker: {marker})")
        return

    # 1 — config revert (consumes the marker). Track whether it changed anything so
    # we only relaunch Claude Desktop when the managed config actually moved.
    config_result = cowork_config.remove_config()
    _print_config_result(config_result)
    if not config_result.ok:
        raise SystemExit(1)

    # 2 — corporate CA (unless kept). A shared corporate CA that other tools rely
    # on can be preserved with --keep-ca.
    if keep_ca:
        click.secho("  Keeping the corporate CA (--keep-ca).", fg="yellow")
    else:
        ca_result = cowork_ca.restore()
        _print_ca_result(ca_result)

    # 3 — tokens + VK cache (unless kept).
    if keep_tokens:
        click.secho("  Keeping OIDC tokens + VK cache (--keep-tokens).", fg="yellow")
    else:
        had_tokens = load_tokens() is not None or load_vk_cache() is not None
        clear_tokens()
        if had_tokens:
            click.secho("  Cleared OIDC tokens + VK cache.", fg="green")
        else:
            click.secho("  No OIDC tokens or VK cache to clear.", fg="yellow")

    # 4 — sweep this tool's own markers + backups (AFTER step 1 consumed the
    # marker). sweep_backups() is allowlisted to the CLI's own data dir.
    swept = cowork_config.sweep_backups()
    if swept:
        click.secho(f"  Removed {len(swept)} marker/backup file(s).", fg="green")
    else:
        click.secho("  No leftover markers or backups to remove.", fg="yellow")

    # A config change needs a relaunch to take effect (Desktop reads config only at
    # launch). Skip when nothing actually changed.
    if config_result.changed:
        click.echo("")
        _relaunch_after_config(no_relaunch=no_relaunch)


@cli.command("clear")
@click.option("--keep-ca", is_flag=True, help="Don't remove the corporate CA (step 2).")
@click.option(
    "--keep-tokens", is_flag=True, help="Don't clear OIDC tokens + VK cache (step 3)."
)
@click.option(
    "--dry-run", is_flag=True, help="Print what would be reverted; change nothing."
)
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt.")
@click.option(
    "--no-relaunch",
    is_flag=True,
    help="Revert the config but don't relaunch Claude Desktop.",
)
def clear_cmd(
    keep_ca: bool, keep_tokens: bool, dry_run: bool, yes: bool, no_relaunch: bool
) -> None:
    """Revert everything software-level setup/login did — but keep the binaries.

    Undoes, in the safe order: the managed config (like `disable`), the corporate
    CA (like `ca restore`), the OIDC tokens + VK cache (like `logout`), and this
    tool's own markers/backups. Runs as the target user with no elevation — every
    surface is a file or registry value, so there is no self-lock. To also remove
    the installed binaries, run `uninstall` afterwards (or `uninstall --clear-first`).
    """
    if not dry_run and not yes:
        click.confirm(
            "This reverts the managed config, corporate CA, tokens/VK, and backups. "
            "Continue?",
            abort=True,
        )
    _do_clear(
        keep_ca=keep_ca,
        keep_tokens=keep_tokens,
        dry_run=dry_run,
        no_relaunch=no_relaunch,
    )


@cli.command("uninstall")
@click.option(
    "--clear-first",
    is_flag=True,
    help="Run `clear` in-process first (revert config/CA/tokens/backups), then "
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
    detached (it self-copies to %TEMP%, then removes both exes, the shared
    `_internal\\` runtime, the PATH entry, and the ARP registration). This command
    NEVER deletes its own directory — a running exe holds a lock on its image.

    It does NOT revert config/CA/tokens; that is `clear`'s job. Because uninstall
    deletes the exe `clear` runs from, use --clear-first to do both in the right
    order (clear in-process, then delegate the binary removal).
    """
    if not dry_run and not yes:
        prompt = "Remove the installed Cowork CLI binaries?"
        if clear_first:
            prompt = (
                "Revert all software state AND remove the installed Cowork CLI "
                "binaries?"
            )
        click.confirm(prompt, abort=True)

    if clear_first:
        # Revert software state in-process while the exe is still present, THEN
        # delegate the binary removal. Non-dry-run only — a dry-run uninstall just
        # reports the resolved path without touching anything.
        if dry_run:
            click.secho("Dry run — would clear software state first, then:", fg="cyan")
        else:
            _do_clear(
                keep_ca=False, keep_tokens=False, dry_run=False, no_relaunch=True
            )
            click.echo("")

    outcome = cowork_uninstall.uninstall(dry_run=dry_run)
    color = "green" if (outcome.delegated or dry_run) else "yellow"
    click.secho(f"  {outcome.detail}", fg=color)
    for warning in outcome.warnings:
        click.secho(f"  ! {warning}", fg="yellow")
    if outcome.hint:
        click.echo("")
        click.echo(outcome.hint)


@cli.command("verify")
def verify_cmd() -> None:
    """Health-check the Cowork (Claude Desktop) gateway setup end to end.

    First prints the current managed config (the former `status` view), then runs
    the checks: the managed-config channel, the HKLM precedence trap, inference +
    egress-proxy reachability, the corporate-CA trust/fingerprint pin, and (for the
    helper-script credential kind) the shared api-key-helper token health.

    The OIDC token-health checks read the corporate OIDC/gateway values baked into
    the build (:mod:`cli.site_defaults`).
    """
    click.echo("Current Cowork managed config")
    click.echo("=" * 50)
    _print_cowork_config()
    click.echo("=" * 50)
    click.echo("")

    outcome = run_cowork_verify()

    for check in outcome.checks:
        icon = _STATUS_ICON[check.status]
        color = _STATUS_COLOR[check.status]
        click.secho(f"  [{icon}] {check.name}", fg=color, nl=False)
        click.echo(f" — {check.detail}")

    click.echo("")
    if outcome.overall == CheckStatus.OK:
        click.secho("Cowork gateway setup looks healthy.", fg="green", bold=True)
    else:
        click.secho("One or more Cowork checks failed (see above).", fg="red", bold=True)
        raise SystemExit(1)


def _print_rollback_summary(rb: Rollback) -> None:
    """Print which setup compensations ran and which failed (secondary context).

    The ORIGINAL setup failure is the primary error Click prints from the re-raised
    exception; this summary is the follow-on "here's what was rolled back" note.
    """
    if not rb.rolled_back and not rb.rollback_errors:
        return
    click.echo("")
    click.secho("Rolled back the steps that had already succeeded:", fg="yellow", bold=True)
    for label in rb.rolled_back:
        click.secho(f"  ✓ {label} — reverted", fg="yellow")
    if rb.rollback_errors:
        click.secho("Rollback issues (resolve manually):", fg="red", bold=True)
        for label, detail in rb.rollback_errors:
            click.secho(f"  ✗ {label} — {detail}", fg="red")


def _relaunch_after_config(*, no_relaunch: bool) -> None:
    """Relaunch Claude Desktop so a config change takes effect (shared helper)."""
    outcome = cowork_app.relaunch_app(no_relaunch=no_relaunch)
    if outcome.relaunched:
        click.secho(f"Claude Desktop relaunched — {outcome.detail}", fg="green")
        return
    click.secho(f"Auto-relaunch skipped: {outcome.detail}", fg="yellow")
    if outcome.hint:
        click.echo("")
        click.echo(outcome.hint)


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
@click.pass_context
def login_cmd(
    ctx: click.Context,
    redirect_port: int,
    timeout: int,
    no_browser: bool,
) -> None:
    """Step 3 — OIDC PKCE browser login.

    Opens your browser to the Cognito Hosted UI, waits for the callback, exchanges
    the auth code for OIDC tokens, and immediately exchanges the id_token for a
    Virtual Key. Both are cached in the OS-native data directory (mode 0600).

    Uses the corporate OIDC/gateway values baked into the build.
    """
    # Baked corporate OIDC/gateway values (:mod:`cli.site_defaults`).
    issuer_url = oidc_issuer_url()
    for key, value in (("oidcIssuerUrl", issuer_url), ("oidcClientId", oidc_client_id())):
        if not value.strip():
            raise click.ClickException(
                f"missing {key} — normally baked into the build. "
                "Use a build that has the corporate OIDC values baked in."
            )

    # Decide browser vs. headless email+password. Explicit --no-browser (or
    # GATEWAY_CLI_NO_BROWSER=1) forces the fallback; otherwise auto-detect a
    # missing browser. The fallback needs a Cognito issuer.
    forced_no_browser = no_browser or os.environ.get("GATEWAY_CLI_NO_BROWSER") == "1"
    use_password = forced_no_browser or not browser_available()

    if use_password and not is_cognito_issuer(issuer_url):
        if forced_no_browser:
            raise click.ClickException(
                "--no-browser requires an AWS Cognito issuer; this IDP is not Cognito."
            )
        use_password = False  # non-Cognito + no browser: nothing we can do but try browser

    click.echo(f"  IDP:         {issuer_url}")
    click.echo(f"  Admin API:   {admin_api_url() or '—'}")
    click.echo(f"  Data dir:    {data_dir()}")
    click.echo(f"  Mode:        {'email+password (no browser)' if use_password else 'browser'}")
    click.echo("")

    try:
        if use_password:
            tokens = run_login_password()
        else:
            tokens = run_login(redirect_port=redirect_port, timeout_seconds=timeout)
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
    click.echo(f"Next: run `{_prog()} setup`.")


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


_BAR = "─" * 52

_STATUS_COLOR = {
    CheckStatus.OK: "green",
    CheckStatus.FAIL: "red",
}
_STATUS_ICON = {
    CheckStatus.OK: "✓",
    CheckStatus.FAIL: "✗",
}


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
