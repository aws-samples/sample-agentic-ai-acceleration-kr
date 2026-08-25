# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Step 4 — write Claude Code managed settings.

Writes gateway configuration to two locations:

1. ``managed-settings.json`` (the enterprise managed settings file — the single
   highest-priority tier, above CLI args, project/local and user settings).
   Deep-merged via :func:`cli.managed.write_gateway_settings`: our keys override,
   any org-provided keys are preserved. Requires admin/sudo.
   Contains: ANTHROPIC_BASE_URL, GATEWAY_CLI_GATEWAY_URL, OTEL env vars,
   apiKeyHelper, statusLine.

   Linux:     /etc/claude-code/managed-settings.json
   Windows:   C:\\Program Files\\ClaudeCode\\managed-settings.json
   WSL:       /etc/claude-code/managed-settings.json when Claude Code is the
              native-Linux build; C:\\Program Files\\ClaudeCode\\... (via the
              /mnt/c mount) only when the Windows Claude Code binary is in use.
              See :func:`cli.managed._wsl_targets_windows_claude`.

   We target the primary managed-settings.json (not the managed-settings.d/
   drop-in) so our OTEL config is guaranteed to win over an org's pre-existing
   managed-settings.json — the ordering between the primary file and .d/
   fragments is undocumented. Caveat: an actual shell env var of the same name
   still beats any settings file; that is outside this tool's control.

2. ``~/.claude/settings.json`` (user settings, lower priority)
   Merged non-destructively. Contains: apiKeyHelper, model, OIDC env vars.

Keys written / updated (others are preserved):

  apiKeyHelper      — path to the api-key-helper binary (found next to the CLI or in PATH)
  model             — default model alias (claude-sonnet-4-6)
  env.ANTHROPIC_BASE_URL          — gateway-proxy URL (API calls)
  env.ADMIN_API_URL               — admin-api URL (primary key read by api-key-helper)
  env.GATEWAY_CLI_GATEWAY_URL     — admin-api URL (legacy alias — kept for backwards compat)
  env.OIDC_ISSUER_URL             — Cognito OIDC issuer (for api-key-helper OIDC mode)
  env.OIDC_CLIENT_ID              — Cognito app client id
  env.OTEL_EXPORTER_OTLP_ENDPOINT — OTel collector endpoint (auto-derived as :80)
  env.OTEL_EXPORTER_OTLP_PROTOCOL — http/protobuf
  env.CLAUDE_CODE_ENABLE_TELEMETRY        — 1
  env.OTEL_METRICS_EXPORTER              — otlp
  env.OTEL_TRACES_EXPORTER               — otlp
  env.CLAUDE_CODE_ENHANCED_TELEMETRY_BETA — 1

Both files are snapshotted with a timestamped, non-overwriting backup before
they are modified (see ``cli.utils.backup``), so no pre-existing user or org
settings are ever lost — even across repeated ``setup`` runs.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import sysconfig
from pathlib import Path
from urllib.parse import urlparse

import boto3
import structlog

from cli import manifest
from cli.env import persist_env_vars
from cli.login import resolve_login_user_id
from cli.managed import (
    ManagedSettingsPermissionError,
    _managed_file,
    _targets_windows_filesystem,
    write_gateway_settings,
)
from cli.manifest import ResolvedConfig
from cli.models import DEFAULT_MODEL
from cli.reconcile import reconcile_settings
from cli.site_defaults import configured_ca_bundle
from cli.site_extra import managed_extra, merge_into, user_extra
from cli.utils.backup import backup_config
from gateway_cli_oidc.tls import without_os_trust_store

log = structlog.get_logger(component="cli-v2")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class SetupStepError(Exception):
    """Raised when the setup step cannot be completed."""


class SetupResult:
    """Outcome of :func:`run_setup`.

    A successful return always means the managed-settings.json write at the
    highest tier succeeded — setup aborts with a hard error otherwise, rather
    than silently degrading to the lower-priority user settings file.

    settings_path:   the user settings.json that was written.
    managed_path:    the managed-settings.json that was written (highest tier).
    persisted_env:   OS environment variable names registered (User scope on
                     Windows / shell rc on POSIX); empty if persistence was
                     disabled or nothing needed writing.
    cleaned_settings: "<layer>: <key>" labels removed from settings.json because
                     they conflicted with gateway routing (see cli.reconcile).

    NOTE: setup does NOT touch OS/system environment variables — that direction
    was intentionally reversed. A gateway-bypassing OS env var
    (e.g. CLAUDE_CODE_USE_BEDROCK) is instead detected and reported by
    ``gateway-cli verify``, which prints step-by-step removal guidance so the
    user removes it themselves at the correct (User or System) scope.
    """

    def __init__(
        self,
        settings_path: Path,
        managed_path: Path,
        persisted_env: list[str] | None = None,
        cleaned_settings: list[str] | None = None,
    ) -> None:
        self.settings_path = settings_path
        self.managed_path = managed_path
        self.persisted_env = persisted_env or []
        self.cleaned_settings = cleaned_settings or []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _user_settings_path() -> Path:
    override = os.environ.get("GATEWAY_CLI_SETTINGS_PATH")
    if override:
        return Path(override).expanduser()
    # Claude Code CLI uses %USERPROFILE%\.claude\settings.json on Windows,
    # identical to ~/.claude/settings.json on macOS/Linux.
    # (The %APPDATA%\Claude\ path is the Claude desktop app — different product.)
    return Path.home() / ".claude" / "settings.json"


def _is_wsl() -> bool:
    """Return True when running inside Windows Subsystem for Linux."""
    try:
        return b"microsoft" in Path("/proc/version").read_bytes().lower()
    except OSError:
        return False


def _is_admin() -> bool | None:
    """Return whether this process holds the privilege to write managed settings.

    True  — definitely elevated (Windows Administrators token held).
    False — definitely NOT elevated (Windows, standard-user token).
    None  — undeterminable, so don't fail fast: on POSIX the managed write goes
            through ``sudo`` (which resolves elevation at write time), and on
            Windows any ctypes failure means we can't be sure.

    Used only for a fast, up-front check; the actual write is still guarded and
    surfaces a hard error if it turns out we lack privilege.
    """
    if sys.platform == "win32":
        try:
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return None
    return None


def _needs_admin_message(managed_path: Path) -> str:
    """The actionable error shown when setup lacks the rights to write managed settings."""
    # Only the Windows-filesystem write (native Windows, or the Windows Claude
    # Code target under /mnt/c in WSL) needs an elevated shell. A native-Linux
    # Claude Code in WSL writes to /etc/claude-code via sudo — same as plain
    # Linux — so it gets the sudo guidance below.
    if _targets_windows_filesystem():
        return (
            "PowerShell을 관리자 권한으로 다시 실행해 주세요.\n"
            "(Please re-run PowerShell as administrator.)\n\n"
            "administrator privileges are required to write the system-wide "
            f"managed settings file:\n    {managed_path}\n"
            "Setup was stopped so your configuration is NOT silently written to a "
            "lower-priority user settings file. Re-run from an elevated shell: "
            "right-click Windows Terminal or PowerShell and choose "
            "'Run as administrator', then run `gateway-cli setup` again."
        )
    return (
        "root privileges are required to write the system-wide managed settings "
        f"file:\n    {managed_path}\n"
        "Setup was stopped rather than degrading to a lower-priority user "
        "settings file. Re-run with sudo, e.g. `sudo gateway-cli setup`."
    )


def ensure_admin_for_setup() -> None:
    """Preflight: abort NOW if this Windows shell is not elevated.

    Every workflow that ends in :func:`run_setup` (``setup`` and ``onboard``)
    must call this at its very start — *before* any password reset, login, or
    file write — so a non-elevated Windows user is told to re-run as
    administrator instead of being left partially onboarded when the managed
    settings write later fails for lack of privilege.

    On Windows a standard (non-elevated) token is positively detectable, so we
    raise :class:`SetupStepError` with the Korean-first guidance. Where elevation
    can't be determined up front (POSIX, which defers to sudo at write time; or
    an undeterminable Windows token) this is a no-op and the write is still
    guarded downstream.
    """
    if _is_admin() is False:
        raise SetupStepError(_needs_admin_message(_managed_file()))


def _search_binary(name: str, quote: bool) -> str | None:
    """Locate a companion binary named ``name``; return its path or None.

    Resolution order:
      1. Next to the running executable (PyInstaller bundle / same-dir install).
      2. sysconfig scripts dir — canonical pip install location.
      3. Anywhere on PATH via shutil.which.

    When ``quote`` is True, a resolved path containing spaces is wrapped in
    double-quotes (Claude Code invokes the helper via ``cmd.exe /c`` on
    Windows, which splits an unquoted ``C:\\Program Files\\...`` on the space).
    """
    def _normalise(p: str) -> str:
        if quote and " " in p:
            return f'"{p}"'
        return p

    # 1. Same directory as the running executable.
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).parent
    else:
        exe_dir = Path(sys.argv[0]).resolve().parent if sys.argv else Path(__file__).parent
    candidate = exe_dir / name
    if candidate.is_file():
        return _normalise(str(candidate))

    # 2. pip scripts directory for the active Python installation.
    scripts_dir = sysconfig.get_path("scripts")
    if scripts_dir:
        candidate = Path(scripts_dir) / name
        if candidate.is_file():
            return _normalise(str(candidate))

    # 3. PATH lookup.
    found = shutil.which(name)
    if found:
        return _normalise(found)

    return None


def _resolve_binary(name_posix: str, name_win: str) -> str:
    """Return the absolute path (or bare name fallback) for a companion binary.

    Windows: resolve the ``.exe`` (quoted for ``cmd.exe``).

    Linux / macOS: resolve the native POSIX binary.

    WSL: a WSL environment can host EITHER a native-Linux Claude Code (which
    reads Linux-side settings and invokes the helper via ``sh -c``) OR the
    Windows Claude Code binary (which invokes the ``.exe`` via ``cmd.exe``).
    Prefer the native Linux helper whenever it is actually installed inside
    WSL: handing a Windows ``.exe`` to a native-Linux Claude Code yields an
    empty API key — the ``.exe`` cannot reach the Linux token store over WSL
    interop — and every gateway call then fails with HTTP 401.  Only when no
    native helper is present do we fall back to the ``.exe`` (the
    Windows-Claude-Code-from-WSL case).
    """
    is_win = sys.platform == "win32"
    is_wsl = (not is_win) and _is_wsl()

    if is_win:
        return _search_binary(name_win, quote=True) or name_win

    if is_wsl:
        return (
            _search_binary(name_posix, quote=False)
            or _search_binary(name_win, quote=True)
            or name_posix
        )

    return _search_binary(name_posix, quote=False) or name_posix


def _resolve_api_key_helper() -> str:
    return _resolve_binary("api-key-helper", "api-key-helper.exe")


def _resolve_statusline() -> str:
    return _resolve_binary("statusline", "statusline.exe")


def _read_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            return {}
        return json.loads(text)
    except (OSError, json.JSONDecodeError) as e:
        raise SetupStepError(f"cannot read settings file '{path}': {e}") from e


def _write_settings(path: Path, settings: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
    try:
        path.write_text(content, encoding="utf-8")
    except OSError as e:
        raise SetupStepError(f"cannot write settings file '{path}': {e}") from e


def _os_env_to_persist(
    gateway_url: str,
    admin_api_url: str,
    issuer_url: str,
    client_id: str,
) -> dict[str, str]:
    """The operator vars ``setup`` registers as permanent User-scope OS env vars.

    WHICH keys are persisted is single-sourced from the manifest
    (``manifest.os_persisted_keys()``) so this set and ``cli.env._OPERATOR_VARS``
    cannot drift (guarded by tests/test_os_persist_manifest_drift.py); the values
    come from the resolved config. Blank values are dropped, and a manifest key
    with no value mapping here is skipped rather than raising — the drift-guard
    test asserts the mapping stays complete.
    """
    values = {
        "ANTHROPIC_BASE_URL": gateway_url,
        "ADMIN_API_URL": admin_api_url,
        "OIDC_ISSUER_URL": issuer_url,
        "OIDC_CLIENT_ID": client_id,
    }
    return {
        key: values[key].strip()
        for key in manifest.os_persisted_keys()
        if values.get(key, "").strip()
    }


# ---------------------------------------------------------------------------
# Core write
# ---------------------------------------------------------------------------

def _resolve_aws_user_id() -> str | None:
    """Return the short user id (part after ':' in STS UserId) or None on failure.

    ``sts:GetCallerIdentity`` must be called with the caller's real (signed)
    credentials — it identifies *who is calling*, so an UNSIGNED/anonymous
    request is rejected with MissingAuthenticationToken and we'd never learn the
    user id. Use the default (signed) client so OTEL_RESOURCE_ATTRIBUTES gets a
    real per-user ``user.id`` (guideline 1-4). Any failure (no creds, offline)
    degrades to None and setup simply omits the attribute.

    The call is wrapped in :func:`without_os_trust_store` because truststore's
    global SSL patch makes botocore's SSLContext creation recurse infinitely
    (``RecursionError``); boto3 verifies via certifi/AWS_CA_BUNDLE, not the OS
    store, so temporarily lifting the patch here is safe and keeps login/verify
    (requests) on the OS store untouched.
    """
    try:
        with without_os_trust_store():
            sts = boto3.client("sts")
            identity = sts.get_caller_identity()
        user_id_full = identity.get("UserId", "")
        return user_id_full.split(":", 1)[-1] if ":" in user_id_full else user_id_full or None
    except Exception:
        return None


def _resolve_user_id() -> str | None:
    """Resolve the telemetry ``user.id`` for OTEL_RESOURCE_ATTRIBUTES.

    Prefers the logged-in OIDC identity (the id_token ``email`` claim — the
    ``<user>@example.com`` value the operator expects and the same principal the
    gateway authorizes), because ``setup`` always runs after ``login``. Falls
    back to the AWS STS caller id only when no OIDC identity is available (e.g.
    setup run standalone), so a value is still stamped where possible. Returns
    None when neither source yields an id, and setup omits the attribute.
    """
    oidc_id = resolve_login_user_id()
    if oidc_id:
        return oidc_id
    return _resolve_aws_user_id()


def run_setup(
    cfg: ResolvedConfig,
    api_key_helper: str | None = None,
    statusline: str | None = None,
    otel_endpoint: str | None = None,
    otel_auth_token: str | None = None,
    model: str = DEFAULT_MODEL,
    available_models: list[str] | None = None,
    default_sonnet_model: str | None = None,
    default_opus_model: str | None = None,
    default_haiku_model: str | None = None,
    persist_env: bool = True,
) -> SetupResult:
    """Write gateway settings to managed-settings.json and ~/.claude/settings.json.

    The managed-settings.json write **requires admin/sudo** because the file
    lives under an admin-only location. Without those privileges setup aborts
    immediately with a :class:`SetupStepError` — it does NOT silently fall back
    to folding the config into the lower-priority ``~/.claude/settings.json``,
    which would look like success while leaving the gateway un-enforced. The
    check is made up front (before any network call or backup) so a non-admin
    run fails fast and cleanly.

    User settings are still written (apiKeyHelper, model, base URLs) once the
    managed write succeeds. Existing non-gateway keys are preserved and the
    original settings.json is backed up first.

    When ``persist_env`` is True (the default) the four operator-provided
    variables (OIDC_ISSUER_URL, OIDC_CLIENT_ID, ADMIN_API_URL,
    ANTHROPIC_BASE_URL) are ALSO registered as permanent OS environment
    variables (Windows User-scope registry / shell rc on POSIX) so tools that
    read the process environment rather than Claude Code's settings.json see
    them too. A persistence failure is logged but does NOT fail setup — the
    settings-file write is the source of truth.
    """
    settings_path = _user_settings_path()
    managed_path = _managed_file()
    helper_path = api_key_helper or _resolve_api_key_helper()
    statusline_path = statusline or _resolve_statusline()

    # Validate the resolved config has the required values.
    missing = cfg.missing_for_setup()
    if missing:
        raise SetupStepError(
            "missing values needed for setup: " + ", ".join(missing)
        )

    # Fail fast: on Windows we can positively detect a standard (non-elevated)
    # token before doing any network work. Abort with a clear, actionable error
    # rather than proceeding and degrading to a lower-priority user settings
    # file. (POSIX defers to sudo at write time, so _is_admin() returns None.)
    # The command layer also runs this preflight BEFORE login/password so an
    # onboard flow never mutates credentials first; this is the backstop.
    ensure_admin_for_setup()

    gateway_url = cfg.gateway_url.rstrip("/")
    admin_api_url = cfg.admin_api_url.rstrip("/")
    issuer_url = cfg.oidc_issuer_url.rstrip("/")
    client_id = cfg.oidc_client_id

    # Resolve the OTel collector endpoint. Precedence, highest first:
    #   1. explicit --otel-endpoint CLI arg (the `otel_endpoint` param)
    #   2. site-extra managed.env.OTEL_EXPORTER_OTLP_ENDPOINT — corporate sites
    #      must be able to pin their own collector (see OTEL_PRECEDENCE.md)
    #   3. auto-derive from the gateway hostname on port 80
    # Feeding the site-extra value in HERE (rather than letting it lose the later
    # env merge) ensures every derived per-signal endpoint (/v1/logs, /v1/metrics,
    # /v1/traces) points at the same collector — no base/derived split.
    if not otel_endpoint:
        site_env = managed_extra().get("env")
        if isinstance(site_env, dict):
            site_otel = site_env.get("OTEL_EXPORTER_OTLP_ENDPOINT")
            if isinstance(site_otel, str) and site_otel.strip():
                otel_endpoint = site_otel.strip()
                log.info("otel_endpoint_from_site_extra", endpoint=otel_endpoint)
    if not otel_endpoint:
        p = urlparse(gateway_url)
        otel_endpoint = f"http://{p.hostname}:80"

    user_id = _resolve_user_id()
    ca_bundle = configured_ca_bundle()

    # ── 1. Write managed-settings.json (system-wide, highest priority) ──────────
    #     Requires admin/sudo. If the write is denied for lack of privilege we
    #     abort with a clear error instead of degrading to user settings — the
    #     up-front _is_admin() check catches the common Windows case, and this
    #     guards the rest (WSL, POSIX sudo denial, elevation lost mid-run).
    try:
        write_gateway_settings(
            gateway_url=gateway_url,
            admin_api_url=admin_api_url,
            api_key_helper_path=helper_path,
            statusline_path=statusline_path,
            otel_endpoint=otel_endpoint,
            otel_auth_token=otel_auth_token,
            user_id=user_id,
            ca_bundle=ca_bundle,
        )
    except ManagedSettingsPermissionError as e:
        # No admin/sudo — stop hard. Do NOT write a lower-priority fallback.
        raise SetupStepError(_needs_admin_message(managed_path)) from e
    except Exception as e:
        # A genuine, unexpected failure (bad path, corrupt existing file, …).
        raise SetupStepError(f"cannot write managed settings: {e}") from e

    # ── 2. Merge into ~/.claude/settings.json (user settings) ──────────────────
    settings = _read_settings(settings_path)

    backup = backup_config("claude-code", settings_path)
    if backup is not None:
        log.info("settings_backed_up", backup=backup.backup_path)

    # De-conflict: setup is otherwise purely additive, so a prior Bedrock/direct
    # install can leave keys that silently defeat the gateway (CLAUDE_CODE_USE_BEDROCK,
    # a stale ANTHROPIC_API_KEY that shadows apiKeyHelper, a legacy statusLine /
    # modelOverrides). Remove them now — the backup above makes this reversible.
    # Single source of truth for WHAT is a conflict lives in cli.reconcile.
    cleaned_settings = reconcile_settings(settings)
    if cleaned_settings:
        log.info("settings_conflicts_removed", removed=cleaned_settings)

    # Build-time custom keys (guideline 1-4) first, so our gateway-owned keys
    # below always win over anything site-extra sets for the same key.
    extra_keys = merge_into(settings, user_extra())
    if extra_keys:
        log.info("site_extra_user_merged", keys=extra_keys)

    # gateway-cli's owned user-tier keys are resolved+emitted through the single
    # catalog path (T5.3, closes C1/R2): the codec encodes each key for its
    # user-tier target — notably the structured `availableModels` (STR_LIST → a
    # JSON array), which a string-only writer could not. The explicit call order
    # below preserves the byte-stable settings.json the characterization golden
    # pins (test_char_user_settings); the manifest groups fields by category, so
    # emission order lives here rather than in FIELDS iteration.
    from typing import Any

    from cli.emit import emit, for_settings_tier

    to_user = for_settings_tier(manifest.Tier.USER)

    def put_user(key: str, typed: Any, *, default_only: bool = False) -> None:
        """Encode ``typed`` for ``key``'s user-tier target and write it into settings.

        SETTINGS_TOP fragments land at the top level; SETTINGS_ENV fragments in the
        ``env`` block. ``typed is None`` writes nothing (keeps optional keys gated).
        ``default_only`` uses ``setdefault`` so an existing (site-extra) value survives.
        """
        for frag in emit(manifest.by_key(key), typed, to_user):
            top = frag.output.placement is manifest.Placement.SETTINGS_TOP
            bucket = settings if top else settings["env"]
            if default_only:
                bucket.setdefault(frag.key, frag.value)
            else:
                bucket[frag.key] = frag.value

    put_user("apiKeyHelper", helper_path)
    put_user("model", model)
    # Expose the picker roster to Claude Code via its documented top-level
    # `availableModels` key (array of aliases). Set at setup time so the roster
    # changes with NO CLI rebuild (guideline 1-2).
    if available_models:
        put_user("availableModels", list(available_models))
    settings.setdefault("env", {})
    put_user("ANTHROPIC_BASE_URL", gateway_url)
    put_user("ADMIN_API_URL", admin_api_url)
    put_user("GATEWAY_CLI_GATEWAY_URL", admin_api_url)
    put_user("OIDC_ISSUER_URL", issuer_url)
    put_user("OIDC_CLIENT_ID", client_id)

    # Optional per-tier default model overrides (ANTHROPIC_DEFAULT_*_MODEL). Only
    # written when the operator supplied the corresponding flag, so a site-extra
    # value is preserved when the flag is omitted.
    for env_key, value in (
        ("ANTHROPIC_DEFAULT_SONNET_MODEL", default_sonnet_model),
        ("ANTHROPIC_DEFAULT_OPUS_MODEL", default_opus_model),
        ("ANTHROPIC_DEFAULT_HAIKU_MODEL", default_haiku_model),
    ):
        if value and value.strip():
            put_user(env_key, value.strip())

    # The managed tier (written above) owns the OTEL/gateway env at the highest
    # priority, so we don't duplicate that block into user settings. We only
    # mirror the CA bundle here: Claude Code reads NODE_EXTRA_CA_CERTS from the
    # *process* env, and a shell env var beats a settings file — so recording it
    # at the user tier too adds resilience. setdefault keeps any existing value.
    if ca_bundle:
        put_user("NODE_EXTRA_CA_CERTS", ca_bundle, default_only=True)

    # site-extra's `user.env` may carry a static OTEL_RESOURCE_ATTRIBUTES (e.g.
    # "service.name=claude-code,user.id=user@example.com"). That's how the same
    # baked user.id ended up on every PC. Overwrite ONLY the user.id segment with
    # the resolved per-user id, preserving the site's other attributes. Only act
    # when the key is actually present, so we don't inject telemetry attrs that
    # the site did not opt into at the user tier (AR-F2).
    #
    # This is the MERGE_ATTRS composition (authoritative_keys=("user.id",)) routed
    # through the single resolver: ``create_if_absent=False`` encodes the user-tier
    # rule (reconcile only a site-seeded map; never inject one). We guard on a
    # resolved ``user_id`` so the deriver returns it verbatim — no second live
    # OIDC/STS lookup — and an un-resolvable identity leaves the seed untouched,
    # exactly as before.
    if user_id and "OTEL_RESOURCE_ATTRIBUTES" in settings["env"]:
        from cli.resolve import Sources, resolve_merge
        from cli.resolvers import ResolveContext

        seed = settings["env"]["OTEL_RESOURCE_ATTRIBUTES"]
        merged = resolve_merge(
            manifest.by_key("OTEL_RESOURCE_ATTRIBUTES"),
            Sources(
                site_extra={"OTEL_RESOURCE_ATTRIBUTES": seed},
                derive_ctx=ResolveContext(user_id=user_id),
            ),
            create_if_absent=False,
        )
        if merged.raw is not None and merged.raw != seed:
            log.info("otel_resource_user_id_set", user_id=user_id)
            settings["env"]["OTEL_RESOURCE_ATTRIBUTES"] = merged.raw

    _write_settings(settings_path, settings)
    log.info(
        "settings_written",
        path=str(settings_path),
        gateway_url=gateway_url,
        admin_api_url=admin_api_url,
        otel_endpoint=otel_endpoint,
        api_key_helper=helper_path,
        user_id=user_id,
        managed_path=str(managed_path),
    )

    # ── 3. Persist the operator vars as permanent OS environment variables ──────
    #     Claude Code reads settings.json, but other tooling (and the customer's
    #     own scripts) read the process env — so by default we also register the
    #     four operator vars at the OS level. Best-effort: a failure here must not
    #     undo the successful settings write, so we log and continue.
    persisted: list[str] = []
    if persist_env:
        os_env_vars = _os_env_to_persist(gateway_url, admin_api_url, issuer_url, client_id)
        try:
            persisted = persist_env_vars(os_env_vars)
            log.info("os_env_persisted", vars=persisted)
        except Exception as e:  # noqa: BLE001 — never fail setup over env persistence
            log.warning("os_env_persist_failed", error=str(e))

    # ── 4. Gateway-bypassing OS env vars are NOT touched here ───────────────────
    #     A real shell/registry env var (e.g. CLAUDE_CODE_USE_BEDROCK or a stale
    #     ANTHROPIC_API_KEY) beats every settings file, so it can still defeat the
    #     managed write. By deliberate design, setup does NOT modify the user's
    #     system/OS environment variables — that is the user's own machine state.
    #     Instead, `gateway-cli verify` detects such a variable and prints
    #     step-by-step removal guidance (User and System/admin scope) so the user
    #     can remove it themselves and confirm the change. The settings.json `env`
    #     block IS reconciled above (that file is ours to manage); the persistent
    #     OS definition is left for the user, guided by verify.

    return SetupResult(
        settings_path=settings_path,
        managed_path=managed_path,
        persisted_env=persisted,
        cleaned_settings=cleaned_settings,
    )
