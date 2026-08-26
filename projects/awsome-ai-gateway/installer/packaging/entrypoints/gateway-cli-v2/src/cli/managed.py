# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Managed settings file management for Claude Code.

Writes gateway configuration to the enterprise **managed settings** file, the
single highest-priority tier in Claude Code's settings hierarchy — it sits above
command-line arguments, project/local settings and the user's own
``~/.claude/settings.json``.

    macOS:        /Library/Application Support/ClaudeCode/managed-settings.json
    Linux / WSL:  /etc/claude-code/managed-settings.json
    Windows:      C:\\Program Files\\ClaudeCode\\managed-settings.json

We write our keys to **two** places so the gateway config wins in every case:

1. The primary ``managed-settings.json`` — deep-merged, our keys override, all
   other org keys preserved. This guarantees we win over an org that pre-seeds
   its own OTEL/env in the primary file.
2. A drop-in ``managed-settings.d/50-gateway.json`` — because a drop-in fragment
   BEATS the primary file, and its ``env`` merges key-by-key over the primary,
   our fragment need only carry OUR keys; org keys we don't set still apply from
   the primary. Operators pin their own OTEL collector via site-extra
   ``managed.env`` — those ``OTEL_*`` values ride into this fragment and win over
   the auto-derived default (see ``OTEL_PRECEDENCE.md``).
   Note the drop-in tie-break: among drop-ins the lexicographically-last filename
   wins, so an org fragment sorting after ``50-gateway.json`` (e.g. ``99-*.json``,
   or ``50-otel.json`` since ``o`` > ``g``) would still override us — keep OTEL in
   this file rather than a later-sorting one. Earlier builds wrote a
   ``99-gateway.json`` drop-in; we remove that stale fragment when it is clearly
   ours so it can't shadow the current config with an old endpoint.

Caveat that cannot be fixed here: an actual OS/shell environment variable of the
same name still takes precedence over any settings file. Document that for
operators; it is outside this tool's control.

Merges are reversible: every key we inject is recorded under a private
``_gatewayCli`` marker so :func:`remove_gateway_settings` removes only our keys
and leaves the org's untouched (deleting the file entirely only when we were the
ones who created it).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import structlog

from cli import site_defaults
from cli.site_extra import deep_merge, managed_extra
from cli.utils.backup import backup_config

log = structlog.get_logger(component="cli")

# Filename of Claude Code's primary (highest-priority) managed settings file.
MANAGED_SETTINGS_FILENAME = "managed-settings.json"

# Backwards-compat alias — earlier revisions wrote a 99-gateway.json drop-in.
GATEWAY_SETTINGS_FILENAME = MANAGED_SETTINGS_FILENAME

# Drop-in directory Claude Code merges over the primary file, and the fragment we
# own. A drop-in beats the primary, and its env merges key-by-key over it (both
# verified live — see OTEL_PRECEDENCE.md). Operator OTEL overrides come from
# site-extra managed.env and ride into this fragment. NOTE: among drop-ins the
# lexicographically-last filename wins, so a later-sorting org fragment (99-*, or
# 50-otel since o > g) would override this one — keep gateway/OTEL keys in this file.
MANAGED_SETTINGS_DROPIN_DIRNAME = "managed-settings.d"
GATEWAY_DROPIN_FILENAME = "50-gateway.json"

# Stale drop-in filename earlier builds wrote; removed when it is clearly ours so
# it cannot shadow the current config with an obsolete endpoint.
LEGACY_GATEWAY_DROPIN_FILENAME = "99-gateway.json"

# Private marker key recording which settings this tool injected, so a later
# `disable` can surgically unmerge them without touching the org's own keys.
GATEWAY_MARKER_KEY = "_gatewayCli"

# ── Corporate proxy configuration (single source of truth) ──────────────────
# These are the authoritative proxy values for the isolated network. `verify`
# imports them to validate what actually landed in the settings file / OS env,
# so setup and verify can never drift (see cli.verify._check_proxy_settings).
#
# All three are environment-specific real infrastructure, so they are injected at
# build time into `cli/_site_config.py` (written by build.ps1 from -ExpectedProxyUrl
# / -NoProxyValue / -ForbiddenNoProxyToken or the matching env vars). They are
# **blank by default** — a bare build bakes no proxy topology: NO_PROXY is not
# written into settings, and verify's corporate-proxy check is skipped entirely
# (see cli.verify._check_proxy_settings) rather than warning off-network.
#
# EXPECTED_PROXY_URL: the forward-proxy address HTTP_PROXY/HTTPS_PROXY must equal.
#   Supplied to Claude Code via site-extra managed.env; verify flags any mismatch
#   only when this is non-blank.
# NO_PROXY_VALUE: the bypass list gateway-cli owns and writes in build_gateway_env.
#   It lists the corporate domain suffixes and internal CIDR ranges reached
#   DIRECTLY. It must never contain FORBIDDEN_NO_PROXY_TOKEN — the gateway/OIDC
#   endpoints live under that suffix but are reached via the CIDR ranges, so
#   listing the suffix as direct would break routing. Blank ⇒ NO_PROXY unset.
EXPECTED_PROXY_URL = site_defaults._baked("EXPECTED_PROXY_URL", "")
FORBIDDEN_NO_PROXY_TOKEN = site_defaults._baked("FORBIDDEN_NO_PROXY_TOKEN", "")
NO_PROXY_VALUE = site_defaults._baked("NO_PROXY_VALUE", "")


class ManagedSettingsPermissionError(PermissionError):
    """Raised when the managed settings file cannot be written for lack of privilege.

    The managed settings file lives under an admin-only location
    (``C:\\Program Files\\ClaudeCode`` / ``/etc/claude-code``). A non-admin,
    per-user install cannot write it. The setup step catches this specifically
    and degrades to user settings rather than aborting the whole flow.
    """


def _is_wsl() -> bool:
    """Return True when running inside Windows Subsystem for Linux."""
    try:
        return b"microsoft" in Path("/proc/version").read_bytes().lower()
    except OSError:
        return False


# The Linux-side managed-settings root read by a native-Linux Claude Code.
_LINUX_MANAGED_ROOT = Path("/etc/claude-code")
# The Windows-side managed-settings root a Windows Claude Code reads, reachable
# from inside WSL through the /mnt/c interop mount.
_WINDOWS_MANAGED_ROOT_FROM_WSL = Path("/mnt/c/Program Files/ClaudeCode")


def _resolved_claude_binary() -> Path | None:
    """Return the fully-resolved path of the ``claude`` binary on PATH, or None.

    The name on PATH is often a symlink (e.g. ``/usr/bin/claude`` →
    ``…/claude-code/bin/claude.exe``), so we resolve it before inspecting.
    """
    found = shutil.which("claude")
    if not found:
        return None
    try:
        return Path(found).resolve()
    except OSError:
        return Path(found)


def _binary_is_windows_pe(path: Path) -> bool | None:
    """Classify a binary by its magic bytes: True=Windows PE, False=ELF, None=unknown.

    The filename is NOT a reliable signal — a native-Linux Claude Code ships as an
    ELF executable literally named ``claude.exe`` (Anthropic packaging quirk). Only
    the file header tells the truth: ``MZ`` (0x4D5A) is a Windows PE, ``\\x7fELF``
    is a Linux binary.
    """
    try:
        with open(path, "rb") as fh:
            magic = fh.read(4)
    except OSError:
        return None
    if magic[:4] == b"\x7fELF":
        return False
    if magic[:2] == b"MZ":
        return True
    return None


def _wsl_targets_windows_claude() -> bool:
    """In WSL, decide whether the managed settings must go to the Windows path.

    A WSL environment can host EITHER a native-Linux Claude Code (reads
    ``/etc/claude-code``) OR the Windows Claude Code binary (reads
    ``C:\\Program Files\\ClaudeCode`` — the ``/mnt/c`` mount from WSL). Writing to
    the wrong root leaves the real managed file untouched and the gateway
    un-enforced (this is exactly what broke statusLine on the WSL test instance:
    native-Linux Claude Code read an empty ``/etc/claude-code`` while setup wrote
    to ``/mnt/c``).

    Decide by the actual binary format of the ``claude`` on PATH — NOT its
    filename, since a native-Linux build is named ``claude.exe``. ELF → Linux
    root; Windows PE → the ``/mnt/c`` Windows root. When ``claude`` is absent or
    its format is unreadable, fall back to the Windows path only if a Windows
    Claude Code install is actually visible on the mount; otherwise default to the
    native-Linux root (the plain-Linux location, written via sudo).
    """
    claude = _resolved_claude_binary()
    if claude is not None:
        is_pe = _binary_is_windows_pe(claude)
        if is_pe is not None:
            return is_pe
    # Undeterminable: only assume Windows when there's positive evidence of a
    # Windows Claude Code install reachable through the interop mount.
    return _WINDOWS_MANAGED_ROOT_FROM_WSL.is_dir()


def _managed_root() -> Path:
    """Return the ClaudeCode managed-settings directory for the current platform.

    This is the directory that holds the primary ``managed-settings.json``:

      Windows:       C:\\Program Files\\ClaudeCode
      macOS:         /Library/Application Support/ClaudeCode
      Linux:         /etc/claude-code
      WSL:           /etc/claude-code (native-Linux Claude Code) OR
                     /mnt/c/Program Files/ClaudeCode (Windows Claude Code)

    WSL runs as Linux (sys.platform == "linux") but the Claude Code process may
    be EITHER the native-Linux build (reads the Linux path) or the Windows binary
    (reads the Windows path via the /mnt/c mount). We detect WSL via
    /proc/version and then pick the root the actual Claude Code install reads —
    see :func:`_wsl_targets_windows_claude`.
    """
    if sys.platform == "win32":
        return Path(r"C:\Program Files\ClaudeCode")
    if sys.platform == "darwin":
        return Path("/Library/Application Support/ClaudeCode")
    if _is_wsl() and _wsl_targets_windows_claude():
        return _WINDOWS_MANAGED_ROOT_FROM_WSL
    return _LINUX_MANAGED_ROOT


def _targets_windows_filesystem() -> bool:
    """Whether the managed write goes to a Windows filesystem location.

    True on native Windows, and in WSL only when we target the Windows Claude
    Code root under /mnt/c (which is written directly, not via ``sudo``). A
    native-Linux Claude Code in WSL writes to ``/etc/claude-code`` through
    ``sudo`` exactly like plain Linux, so this returns False there.
    """
    if sys.platform == "win32":
        return True
    return _is_wsl() and _wsl_targets_windows_claude()


def _managed_file() -> Path:
    return _managed_root() / MANAGED_SETTINGS_FILENAME


def _dropin_dir() -> Path:
    return _managed_root() / MANAGED_SETTINGS_DROPIN_DIRNAME


def _dropin_file() -> Path:
    """Our drop-in fragment (beats the primary file; also holds operator OTEL)."""
    return _dropin_dir() / GATEWAY_DROPIN_FILENAME


def _legacy_dropin_file() -> Path:
    """The stale drop-in earlier builds wrote (removed when clearly ours)."""
    return _dropin_dir() / LEGACY_GATEWAY_DROPIN_FILENAME


def _shadowing_dropins() -> list[str]:
    """Foreign ``managed-settings.d`` fragments that sort AFTER our 50-gateway.json.

    Among drop-ins the lexicographically-last filename wins and its ``env`` merges
    key-by-key over the earlier ones, so any ``*.json`` fragment sorting after
    ``50-gateway.json`` (e.g. ``99-*.json``, or ``50-otel.json`` since ``o`` >
    ``g``) can override the gateway/OTEL/proxy keys we just wrote — silently
    breaking routing. We must NOT touch a drop-in an org authored, so this only
    *reports* the shadowing fragments (a verify hint); the operator renames them
    to sort earlier. Our own fragment and the legacy one we retire are excluded.
    """
    try:
        names = sorted(
            p.name for p in _dropin_dir().iterdir() if p.is_file() and p.suffix == ".json"
        )
    except OSError:
        return []  # dir absent/unreadable — nothing we can enumerate
    ours = {GATEWAY_DROPIN_FILENAME, LEGACY_GATEWAY_DROPIN_FILENAME}
    return [name for name in names if name not in ours and name > GATEWAY_DROPIN_FILENAME]


def is_gateway_enabled() -> bool:
    """Check if this tool has injected gateway settings into managed settings."""
    data = _read_existing()
    return bool(data) and isinstance(data.get(GATEWAY_MARKER_KEY), dict)


def read_gateway_settings() -> dict | None:
    """Read the current managed settings file, or None if not present/unreadable."""
    path = _managed_file()
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _read_existing() -> dict:
    """Read the primary managed-settings.json, returning {} when absent.

    Raises OSError/JSONDecodeError-derived ValueError if the file exists but is
    unparseable — we must never silently overwrite an org's managed settings.
    """
    path = _managed_file()
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    return json.loads(text)


def _quote_helper_path(path: str) -> str:
    """Quote the helper path if it contains spaces.

    Claude Code executes the apiKeyHelper value as a shell string
    (cmd.exe /c <value> on Windows, sh -c <value> on POSIX).  When the
    path contains spaces (e.g. C:\\Program Files\\...) the shell splits on
    the space and misidentifies the executable.  Wrapping in double-quotes
    fixes this on both cmd.exe and POSIX sh.
    """
    if " " in path and not path.startswith('"'):
        return f'"{path}"'
    return path


def set_resource_user_id(existing: str | None, user_id: str) -> str:
    """Return an ``OTEL_RESOURCE_ATTRIBUTES`` value with ``user.id`` set to ``user_id``.

    ``OTEL_RESOURCE_ATTRIBUTES`` is a comma-separated ``key=value`` list (e.g.
    ``service.name=claude-code,user.id=alice@corp``). This replaces (or appends)
    ONLY the ``user.id`` entry, leaving every other attribute — notably
    ``service.name`` — untouched. That lets a site pin static resource attributes
    via site-extra while the CLI still stamps a real, per-user ``user.id`` at
    setup time (the caller resolves ``user_id`` from the logged-in identity).
    """
    parts: list[str] = []
    replaced = False
    for raw in (existing or "").split(","):
        part = raw.strip()
        if not part:
            continue
        if part.split("=", 1)[0].strip() == "user.id":
            parts.append(f"user.id={user_id}")
            replaced = True
        else:
            parts.append(part)
    if not replaced:
        parts.append(f"user.id={user_id}")
    return ",".join(parts)


def build_gateway_env(
    gateway_url: str,
    admin_api_url: str,
    otel_endpoint: str | None,
    otel_auth_token: str | None,
    user_id: str | None,
    ca_bundle: str | None = None,
) -> dict[str, str]:
    """Build the env-var block this tool owns (excludes org-provided keys).

    Public so the setup step can reuse it as a fallback: when the managed
    settings file cannot be written (e.g. a non-admin, per-user install), the
    same OTEL/gateway env is folded into ~/.claude/settings.json instead, so
    telemetry and routing still work — just at a lower settings tier.

    ``ca_bundle`` is the corporate TLS CA/PEM path on the *target* machine. When
    set it is exported to Claude Code (Node) via NODE_EXTRA_CA_CERTS and to our
    Python/boto3 helpers via REQUESTS_CA_BUNDLE / AWS_CA_BUNDLE / SSL_CERT_FILE,
    so the whole stack trusts the corporate CA on the isolated network.
    """
    # Every key below is an OWNED field in the manifest catalog; its value is
    # resolved once (manifest constant, per-signal derivation, or the merge-attrs
    # user.id stamp) and encoded for its managed-tier target via the single emit
    # path (T5.2 — supersedes the hand-written literal dict, finding: fragmentation
    # / R2). The explicit key order below is the contract the characterization
    # golden pins (test_char_managed_env); the manifest groups fields by category,
    # so order lives here rather than in FIELDS iteration.
    from typing import Any

    from cli.emit import emit, for_settings_tier
    from cli.manifest import Tier, by_key
    from cli.resolve import Sources, resolve, resolve_merge
    from cli.resolvers import ResolveContext

    to_managed = for_settings_tier(Tier.MANAGED)
    env: dict[str, str] = {}

    def put(key: str, typed: Any) -> None:
        """Encode ``typed`` for ``key``'s managed-tier target and record it.

        ``typed is None`` emits nothing (the field stays absent) — that is how the
        optional CA / OTEL / token / user.id keys keep their conditional gating.
        """
        for fragment in emit(by_key(key), typed, to_managed):
            env[fragment.key] = fragment.value

    def const(key: str) -> Any:
        """The field's manifest literal/baked default, typed (no runtime source)."""
        return resolve(by_key(key), Sources()).value

    def derived_endpoint(key: str, base: str) -> Any:
        """A per-signal OTEL endpoint derived from ``base`` via the catalog deriver."""
        return resolve(by_key(key), Sources(derive_ctx=ResolveContext(otel_endpoint=base))).value

    # Gateway routing (owned) — already-resolved inputs, encoded via the catalog.
    put("ANTHROPIC_BASE_URL", gateway_url)
    put("GATEWAY_CLI_GATEWAY_URL", admin_api_url)

    # Corporate TLS CA — one bundle path fanned out to Node + the Py/boto helpers.
    if ca_bundle:
        for key in ("NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE", "AWS_CA_BUNDLE", "SSL_CERT_FILE"):
            put(key, ca_bundle)

    # Claude Code client-side OTEL (metrics + traces + logs).
    if otel_endpoint:
        put("CLAUDE_CODE_ENABLE_TELEMETRY", const("CLAUDE_CODE_ENABLE_TELEMETRY"))
        put("CLAUDE_CODE_ENHANCED_TELEMETRY_BETA", const("CLAUDE_CODE_ENHANCED_TELEMETRY_BETA"))

        put("OTEL_METRICS_EXPORTER", const("OTEL_METRICS_EXPORTER"))
        put("OTEL_LOGS_EXPORTER", const("OTEL_LOGS_EXPORTER"))
        put("OTEL_TRACES_EXPORTER", const("OTEL_TRACES_EXPORTER"))

        put("OTEL_EXPORTER_OTLP_PROTOCOL", const("OTEL_EXPORTER_OTLP_PROTOCOL"))
        put("OTEL_EXPORTER_OTLP_ENDPOINT",
            derived_endpoint("OTEL_EXPORTER_OTLP_ENDPOINT", otel_endpoint))
        put("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
            derived_endpoint("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", otel_endpoint))
        put("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
            derived_endpoint("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", otel_endpoint))
        put("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
            derived_endpoint("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", otel_endpoint))
        put("OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE",
            const("OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE"))

        put("OTEL_LOG_USER_PROMPTS", const("OTEL_LOG_USER_PROMPTS"))
        put("OTEL_LOG_TOOL_DETAILS", const("OTEL_LOG_TOOL_DETAILS"))
        put("OTEL_LOG_TOOL_CONTENT", const("OTEL_LOG_TOOL_CONTENT"))

        put("OTEL_METRICS_INCLUDE_VERSION", const("OTEL_METRICS_INCLUDE_VERSION"))
        put("OTEL_METRICS_INCLUDE_ENTRYPOINT", const("OTEL_METRICS_INCLUDE_ENTRYPOINT"))

        if user_id:
            # Stamp a real per-user user.id via the MERGE_ATTRS resolver (R1 / Fix 1).
            # create_if_absent=True matches the managed-tier semantics: the key is
            # injected even with no site-extra seed, byte-identically to the former
            # set_resource_user_id(None, user_id) (proven in test_compose_user_id).
            # A static site-extra OTEL_RESOURCE_ATTRIBUTES is reconciled against this
            # in write_gateway_settings so the dynamic user.id always wins.
            merged = resolve_merge(
                by_key("OTEL_RESOURCE_ATTRIBUTES"),
                Sources(derive_ctx=ResolveContext(user_id=user_id)),
                create_if_absent=True,
            )
            put("OTEL_RESOURCE_ATTRIBUTES", merged.value)

        if otel_auth_token:
            # The header value (Authorization=Bearer <token>) is computed, not a
            # catalogued literal; the catalog only types/places the key.
            put("OTEL_EXPORTER_OTLP_HEADERS", f"Authorization=Bearer {otel_auth_token}")

    # Owned proxy-bypass list (baked constant — CSV field, normalised on emit).
    put("NO_PROXY", const("NO_PROXY"))

    return env


def write_gateway_settings(
    gateway_url: str,
    admin_api_url: str,
    api_key_helper_path: str,
    statusline_path: str = "statusline",
    otel_endpoint: str | None = None,
    otel_auth_token: str | None = None,
    user_id: str | None = None,
    ca_bundle: str | None = None,
) -> Path:
    """Deep-merge gateway configuration into the primary managed-settings.json.

    gateway_url:     Gateway proxy URL (ANTHROPIC_BASE_URL — for Claude Code API calls)
    admin_api_url:   Admin API URL (GATEWAY_CLI_GATEWAY_URL — for api-key-helper VK issuance)
    statusline_path: Resolved path or bare command name for the statusline binary.
    user_id:         AWS caller user id (part after ':' in UserId) for OTEL_RESOURCE_ATTRIBUTES

    Our gateway/routing env keys (ANTHROPIC_BASE_URL, GATEWAY_CLI_GATEWAY_URL,
    the TLS CA bundle vars, …), apiKeyHelper and statusLine override any
    org-provided values of the same name; every other key already in the file is
    preserved. Telemetry (``OTEL_*``) keys are the deliberate exception: a value
    set in site-extra ``managed.env`` wins over gateway-cli's auto-derived
    default, so a corporate site can pin its own OTEL collector. See
    ``OTEL_PRECEDENCE.md`` for the full precedence order.
    A timestamped backup of the pre-existing file is snapshotted first (see
    :func:`_backup_existing`), so repeated runs never lose an earlier state.

    Requires root/admin — uses sudo on Linux/WSL.  Returns the path written.
    """
    gateway_env = build_gateway_env(
        gateway_url=gateway_url,
        admin_api_url=admin_api_url,
        otel_endpoint=otel_endpoint,
        otel_auth_token=otel_auth_token,
        user_id=user_id,
        ca_bundle=ca_bundle,
    )
    quoted_helper = _quote_helper_path(api_key_helper_path)
    status_line = {"type": "command", "command": statusline_path}

    # Read the org's existing managed settings (empty dict if the file is new).
    existing = _read_existing()
    file_pre_existed = bool(existing)

    # Our top-level keys (besides env), so removal knows exactly what we own.
    top_keys = ["apiKeyHelper", "statusLine"]

    settings = dict(existing)  # shallow copy; we replace nested dicts explicitly

    # Build-time custom keys (guideline 1-4). Deep-merged first so our
    # gateway-owned keys below always win; site-extra's own top-level keys
    # (excluding env, which is merged key-by-key next) are recorded so `disable`
    # can unmerge them. site-extra env vars are folded into gateway_env so they
    # land in the marker's envKeys and are removed cleanly too.
    extra = managed_extra()
    extra_env = extra.pop("env", None) if isinstance(extra, dict) else None
    if isinstance(extra_env, dict):
        # site-extra env first, our gateway/routing env wins on conflict — EXCEPT
        # telemetry keys. Corporate sites must be able to pin their own OTEL
        # collector, so any OTEL_*-prefixed key set in site-extra wins over
        # gateway-cli's auto-derived value (see OTEL_PRECEDENCE.md). The base
        # endpoint is already resolved from site-extra in setup.run_setup so the
        # derived per-signal endpoints stay consistent; this additionally lets an
        # operator override individual per-signal endpoints if they need to.
        gateway_env = {**extra_env, **gateway_env}
        for key, value in extra_env.items():
            if key.startswith("OTEL_"):
                gateway_env[key] = value
        # OTEL_RESOURCE_ATTRIBUTES is the deliberate exception to "site-extra
        # OTEL_* wins": a site pins static resource attributes (service.name,
        # deployment.environment, …) there, but ``user.id`` MUST stay dynamic —
        # otherwise the same baked value (e.g. user.id=user@example.com) lands
        # on every PC. Keep the site-extra attributes but overwrite user.id with
        # the resolved per-user id.
        #
        # This is the MERGE_ATTRS composition (authoritative_keys=("user.id",))
        # routed through the single resolver. The managed tier uses
        # ``create_if_absent=True`` — matching build_gateway_env, which injects a
        # user.id even with no site seed — but here we only reach this branch when
        # the site *did* seed the attr, so the merge reconciles the seeded map.
        # Output is identical to set_resource_user_id (proven in test_compose_user_id).
        if user_id and "OTEL_RESOURCE_ATTRIBUTES" in extra_env:
            from cli.manifest import by_key
            from cli.resolve import Sources, resolve_merge
            from cli.resolvers import ResolveContext

            merged = resolve_merge(
                by_key("OTEL_RESOURCE_ATTRIBUTES"),
                Sources(
                    site_extra={"OTEL_RESOURCE_ATTRIBUTES": extra_env["OTEL_RESOURCE_ATTRIBUTES"]},
                    derive_ctx=ResolveContext(user_id=user_id),
                ),
                create_if_absent=True,
            )
            if merged.raw is not None:
                gateway_env["OTEL_RESOURCE_ATTRIBUTES"] = merged.raw
    if extra:
        deep_merge(settings, extra)
        for key in extra:
            if key not in top_keys:
                top_keys.append(key)
        log.info("site_extra_managed_merged", keys=sorted(extra.keys()))

    # Merge env key-by-key so org env vars we don't manage survive untouched.
    merged_env = dict(existing.get("env") or {})
    merged_env.update(gateway_env)
    settings["env"] = merged_env

    settings["apiKeyHelper"] = quoted_helper
    settings["statusLine"] = status_line

    # Record what we injected so `disable` can cleanly unmerge it. If a prior
    # marker exists, keep its original `fileExisted` (the true pre-gateway state).
    prior_marker = existing.get(GATEWAY_MARKER_KEY)
    if isinstance(prior_marker, dict) and "fileExisted" in prior_marker:
        file_existed_before_gateway = bool(prior_marker["fileExisted"])
    else:
        file_existed_before_gateway = file_pre_existed
    settings[GATEWAY_MARKER_KEY] = {
        "managed": True,
        "comment": "LLM Gateway - keys managed by gateway-cli",
        "envKeys": sorted(gateway_env.keys()),
        "topKeys": top_keys,
        "fileExisted": file_existed_before_gateway,
    }

    content = json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
    target = _managed_file()

    # Snapshot the original (timestamped) before we merge our keys into it.
    if file_pre_existed:
        _backup_existing(target)

    on_win = _targets_windows_filesystem()
    try:
        if on_win:
            _write_windows(target, content)
        else:
            _write_unix(target, content)
    except PermissionError as e:
        raise ManagedSettingsPermissionError(
            f"insufficient privileges to write {target}"
        ) from e
    except subprocess.CalledProcessError as e:
        # sudo failed / was denied on POSIX — treat as a privilege problem.
        raise ManagedSettingsPermissionError(
            f"insufficient privileges to write {target} (sudo failed)"
        ) from e

    # Also write the drop-in so we win over the primary file's copy of the same
    # keys (a drop-in beats the primary file). The fragment carries
    # ONLY our keys — org keys it doesn't set still apply from the primary,
    # because a drop-in's env merges key-by-key over the primary.
    _write_gateway_dropin(gateway_env, quoted_helper, status_line, on_win)

    log.info("managed_settings_written", path=str(target), merged=file_pre_existed)
    return target


def _write_gateway_dropin(
    gateway_env: dict[str, str],
    quoted_helper: str,
    status_line: dict,
    on_win: bool,
) -> None:
    """Write our ``managed-settings.d/50-gateway.json`` fragment.

    Carries only gateway-owned keys so it overrides the primary file's copy of
    the same keys without disturbing anything else. Best-effort: the drop-in is a
    belt-and-suspenders win over the primary tier, so if it can't be written we
    log and continue — the primary managed file (already written) still applies.
    Also removes the stale ``99-gateway.json`` earlier builds wrote, so it can't
    shadow the current config with an obsolete endpoint.
    """
    fragment = {
        GATEWAY_MARKER_KEY: {
            "managed": True,
            "comment": "LLM Gateway - drop-in written by gateway-cli (add OTEL keys here)",
            "envKeys": sorted(gateway_env.keys()),
            "topKeys": ["apiKeyHelper", "statusLine"],
        },
        "env": dict(gateway_env),
        "apiKeyHelper": quoted_helper,
        "statusLine": status_line,
    }
    content = json.dumps(fragment, indent=2, ensure_ascii=False) + "\n"
    target = _dropin_file()
    # Snapshot any pre-existing drop-in (ours from a prior run, or one an org put
    # at this exact path) before we overwrite it — same timestamped policy as the
    # primary file, so no prior drop-in state is ever silently lost.
    _backup_existing(target, label="claude-code-managed-dropin")
    try:
        if on_win:
            _write_windows(target, content)
        else:
            _write_unix(target, content)
    except (PermissionError, OSError, subprocess.CalledProcessError) as e:
        log.warning("gateway_dropin_write_failed", path=str(target), error=str(e))
        return

    # Retire the stale fragment from earlier builds (only when clearly ours).
    _remove_legacy_dropin(on_win)
    log.info("gateway_dropin_written", path=str(target))

    # Tie-break guard (T6.3): warn if a foreign drop-in sorts AFTER ours and could
    # override the gateway/OTEL/proxy keys we just wrote. We never rename another
    # party's file — surface it so an operator can reorder it. See PROXY_PRECEDENCE.md.
    shadowing = _shadowing_dropins()
    if shadowing:
        log.warning(
            "gateway_dropin_may_be_shadowed",
            our_dropin=GATEWAY_DROPIN_FILENAME,
            later_sorting=shadowing,
            hint=(
                "these drop-ins sort after 50-gateway.json and their env merges "
                "over ours — rename them to sort earlier if they override gateway keys"
            ),
        )


def _remove_legacy_dropin(on_win: bool) -> None:
    """Delete ``99-gateway.json`` if it exists and carries our marker.

    We never touch a drop-in an org authored — only the one earlier gateway-cli
    builds wrote (identified by the ``_gatewayCli`` marker). Best-effort.
    """
    legacy = _legacy_dropin_file()
    if not legacy.is_file():
        return
    try:
        data = json.loads(legacy.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # Unrecognizable — do not guess; leave it for a human.
        return
    if not isinstance(data, dict) or not isinstance(data.get(GATEWAY_MARKER_KEY), dict):
        return
    # Snapshot before we retire it, so the old drop-in state is never lost.
    _backup_existing(legacy, label="claude-code-managed-dropin")
    try:
        if on_win:
            _remove_windows(legacy)
        else:
            _remove_unix(legacy)
        log.info("legacy_gateway_dropin_removed", path=str(legacy))
    except (PermissionError, OSError, subprocess.CalledProcessError) as e:
        log.warning("legacy_gateway_dropin_remove_failed", path=str(legacy), error=str(e))


def _remove_gateway_dropin(on_win: bool) -> bool:
    """Delete our ``50-gateway.json`` drop-in. Returns True if it was removed.

    The fragment is entirely ours (always carries the marker), so — unlike the
    primary file — we can remove it outright. Best-effort; a stray org drop-in of
    a different name is never touched.
    """
    target = _dropin_file()
    if not target.is_file():
        return False
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(data, dict) or not isinstance(data.get(GATEWAY_MARKER_KEY), dict):
        # Something else claimed our filename — do not delete it.
        return False
    # Snapshot before removing, so a later `disable` never loses the drop-in
    # state (it may carry operator-added OTEL keys merged into our fragment).
    _backup_existing(target, label="claude-code-managed-dropin")
    try:
        if on_win:
            _remove_windows(target)
        else:
            _remove_unix(target)
    except (PermissionError, OSError, subprocess.CalledProcessError) as e:
        log.warning("gateway_dropin_remove_failed", path=str(target), error=str(e))
        return False
    log.info("gateway_dropin_removed", path=str(target))
    return True


def remove_gateway_settings() -> bool:
    """Remove only the keys this tool injected from managed-settings.json.

    Org-provided keys are preserved.  If the file existed before gateway-cli
    ever touched it, the reduced file is written back; if gateway-cli created
    the file, it is deleted entirely.  Returns True if anything was removed.

    A timestamped backup of the pre-removal file is snapshotted first (see
    :func:`_backup_existing`), so a `disable` is reversible even in the
    delete-the-file branch — mirroring the pre-merge backup in
    :func:`write_gateway_settings`.
    """
    on_win = _targets_windows_filesystem()

    # Remove our drop-in first (and any stale legacy fragment), so
    # `disable` fully unwinds the two-location write. Done regardless of the
    # primary file's state below — the fragments are entirely ours.
    removed_dropin = _remove_gateway_dropin(on_win)
    _remove_legacy_dropin(on_win)

    target = _managed_file()
    if not target.is_file():
        return removed_dropin

    try:
        data = _read_existing()
    except (json.JSONDecodeError, OSError):
        # Unparseable — refuse to guess. Leave it for a human.
        log.warning("managed_settings_unparseable_on_remove", path=str(target))
        return removed_dropin

    marker = data.get(GATEWAY_MARKER_KEY)
    if not isinstance(marker, dict):
        # Not managed by us (or already removed) — do not touch the org's file.
        return removed_dropin

    env = data.get("env")
    if isinstance(env, dict):
        for key in marker.get("envKeys", []):
            env.pop(key, None)
        if not env:
            data.pop("env", None)

    for key in marker.get("topKeys", []):
        data.pop(key, None)

    data.pop(GATEWAY_MARKER_KEY, None)

    file_existed_before = bool(marker.get("fileExisted", False))

    # Snapshot the current on-disk file before we unmerge our keys / delete it, so
    # a `disable` is reversible (T6.1). The write path already backs up before a
    # merge; the removal path did not, so an org that later regretted disabling had
    # no restore point. The timestamped, non-overwriting helper captures the full
    # pre-removal state (org keys + ours) in BOTH branches below — including the
    # delete-the-file case, where the snapshot is the only surviving copy.
    _backup_existing(target)

    if data or file_existed_before:
        # Org keys remain (or the file predated us) — write the reduced file.
        content = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        if on_win:
            _write_windows(target, content)
        else:
            _write_unix(target, content)
    else:
        # We created the file and nothing of ours-or-theirs remains — remove it.
        if on_win:
            _remove_windows(target)
        else:
            _remove_unix(target)

    log.info("managed_settings_removed", path=str(target), file_kept=bool(data) or file_existed_before)
    return True


# ---------------------------------------------------------------------------
# Platform-specific write/remove (requires elevated privileges)
# ---------------------------------------------------------------------------

def _backup_existing(target: Path, label: str = "claude-code-managed") -> None:
    """Snapshot ``target`` before we overwrite/merge into it.

    Uses the timestamped, non-overwriting backup helper so **every** pre-write
    state is retained (a repeated ``setup`` never clobbers an earlier snapshot),
    unlike the old once-only ``.gateway-cli.bak`` sidecar. ``label`` distinguishes
    the primary managed file from the drop-in fragment in the backups dir.

    The backups directory lives under the invoking user's data dir, which is
    writable without elevation even though the managed file itself is not — so
    this needs no sudo/admin and never blocks the (already privileged) write.
    A failure here must not abort setup, so backup errors are swallowed with a
    warning; the merge preserves org keys regardless. ``backup_config`` no-ops
    when the file does not exist yet, so this is safe to call unconditionally.
    """
    try:
        entry = backup_config(label, target)
    except OSError as e:
        log.warning("managed_settings_backup_failed", path=str(target), error=str(e))
        return
    if entry is not None:
        log.info("managed_settings_backed_up", backup=entry.backup_path)


def _write_unix(target: Path, content: str) -> None:
    """Write via sudo tee (no temp file needed, atomic enough)."""
    parent = target.parent
    # Ensure directory exists
    subprocess.run(
        ["sudo", "mkdir", "-p", str(parent)],
        check=True,
    )
    # Write content via sudo tee
    subprocess.run(
        ["sudo", "tee", str(target)],
        input=content.encode(),
        stdout=subprocess.DEVNULL,
        check=True,
    )
    # Set permissions: root-owned, world-readable
    subprocess.run(["sudo", "chmod", "644", str(target)], check=True)
    group = "wheel" if sys.platform == "darwin" else "root"
    subprocess.run(["sudo", "chown", f"root:{group}", str(target)], check=True)


def _remove_unix(target: Path) -> None:
    subprocess.run(["sudo", "rm", "-f", str(target)], check=True)


def _write_windows(target: Path, content: str) -> None:
    """Write directly — requires running as admin on Windows."""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _remove_windows(target: Path) -> None:
    target.unlink(missing_ok=True)
