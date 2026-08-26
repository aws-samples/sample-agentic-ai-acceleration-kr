# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Managed configuration for Cowork (Claude Desktop) — the config channel.

Cowork is Claude Desktop and reads its managed config from an OS-native channel:

- **Windows (MSIX)**: the registry policy key ``HKCU\\SOFTWARE\\Policies\\Claude``.
  File-based ``configLibrary`` did NOT activate 3P on the tested MSIX build; the
  registry policy did. Values are ``REG_SZ``; ``inferenceModels`` is a JSON-array
  *string*.
- **macOS**: ``~/Library/Application Support/Claude-3p/configLibrary/<uuid>.json``
  selected by ``_meta.json``'s ``appliedId``. ``inferenceModels`` is a real JSON
  array here.

Two Cowork-native capabilities live here:

1. **site-extra allowlist injection** (:data:`COWORK_EXTRA_ALLOWLIST` +
   :func:`resolve_site_extra`) — only a curated set of documented Claude Desktop
   3P keys may be injected; core routing/credential keys can never be overridden.
2. **Invocation-scoped rollback** (:class:`ConfigWritePlan` / :func:`plan_write` /
   :meth:`ConfigWritePlan.compensate`) — snapshot-before-mutate undo layered on
   the durable marker/backup tier.

Ownership-aware: the registry key and macOS config file may already hold
org-managed values (e.g. ``deploymentOrganizationUuid``). We snapshot the prior
state before writing so ``remove`` reverts to *exactly* that state — deleting only
the values we added, restoring any we overwrote, and never touching keys we did
not write. On macOS we snapshot the whole prior file (ownership-preserving on
restore for the same reason).

HKLM precedence: on v1.19367.0+ any value under ``HKLM\\SOFTWARE\\Policies\\Claude``
makes the app ignore ``HKCU`` entirely. By default we write only ``HKCU`` (per-user)
and :func:`hklm_conflict` reports a *stale* HKLM key as a trap so the CLI can warn.
An operator can instead opt into a machine-wide install (``registry_scope="machine"``
— chosen at install time via the installer's scope checkbox, or ``setup --scope
machine``), which writes ``HKLM`` deliberately: one policy for every user on the box.
The two modes are mutually exclusive by design — HKLM, when present, wins.

Credential channel — two kinds:

- ``helper-script`` (default): writes ``inferenceCredentialHelper`` = the path to
  the api-key-helper shim, so the app auto-refreshes the ~1h Virtual Key on its own
  TTL schedule — no env vars, no relaunch on rotation.
- ``static``: writes a concrete ``inferenceGatewayApiKey``. The VK expires in ~1h
  and must be re-written to refresh.

Switching kinds removes the other kind's keys so no stale credential lingers.
Operations are best-effort and return a structured :class:`ConfigResult`.

Corporate proxy constants (:data:`EXPECTED_PROXY_URL` / :data:`FORBIDDEN_NO_PROXY_TOKEN`
/ :data:`NO_PROXY_VALUE`) live at the bottom of this module — the single source of
truth ``cli.verify`` validates the OS proxy environment against.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from cli import site_defaults, site_extra
from cli.paths import (
    _release_locked_env,
    data_dir,
    data_dir_for_scope,
    machine_data_dir,
)
from cli.utils import backup as backup_util

log = structlog.get_logger(component="cowork-config")

# The managed-policy location. Windows uses the registry; macOS uses configLibrary.
# The subkey is identical under both roots; only the root (HKCU vs HKLM) varies with
# the chosen registry scope.
_WIN_ROOT = "HKCU"  # default per-user root (see registry_scope for the machine mode)
_WIN_SUBKEY = r"SOFTWARE\Policies\Claude"
_WIN_HKLM_SUBKEY = r"SOFTWARE\Policies\Claude"  # same path under HKLM

# Valid registry scopes for a Windows install and the file the installer drops next
# to the exe to record the operator's wizard choice (read by resolve_registry_scope).
_REGISTRY_SCOPES = ("user", "machine")
_INSTALLER_SCOPE_FILENAME = "registry-scope.conf"


def _win_root_for_scope(scope: str) -> str:
    """Map a registry scope to its root alias (``"HKLM"`` / ``"HKCU"``)."""
    return "HKLM" if scope == "machine" else "HKCU"


def _win_hive_for_scope(scope: str):
    """Map a registry scope to the winreg predefined hive handle."""
    import winreg  # noqa: PLC0415 — platform-only import

    return (
        winreg.HKEY_LOCAL_MACHINE if scope == "machine" else winreg.HKEY_CURRENT_USER
    )


def installer_registry_scope() -> str | None:
    """Return the scope the Windows installer recorded next to the exe, or None.

    The Inno installer writes ``registry-scope.conf`` (``user`` / ``machine``) into
    the install dir so a later ``setup`` — which the user runs in their OWN elevated
    session, the only place the correct hive can be written — honors the wizard
    choice without a flag. Best-effort: unreadable/unknown content returns None so
    the caller falls back to the safe per-user default.
    """
    try:
        exe_dir = Path(sys.executable).resolve().parent
    except (OSError, ValueError):
        return None
    pref = exe_dir / _INSTALLER_SCOPE_FILENAME
    try:
        value = pref.read_text(encoding="utf-8").strip().lower()
    except OSError:
        return None
    return value if value in _REGISTRY_SCOPES else None


def resolve_registry_scope(explicit: str | None) -> str:
    """Resolve the effective scope: explicit flag > installer file > ``"user"``.

    ``explicit`` is the value of ``setup --scope`` (None when the flag was omitted).
    Off Windows the result is inert — the macOS writer ignores it.
    """
    if explicit in _REGISTRY_SCOPES:
        return explicit
    return installer_registry_scope() or "user"

# Owner tag for the backups this module writes (mirrors the file-backup convention).
_TOOL_NAME = "cowork"

# A small marker in our own data dir that records what `remove` needs to revert.
_MARKER_NAME = "cowork-config-backup.json"

# The complete set of value names this module owns — everything it may write OR
# remove across both credential kinds. Snapshotting this whole set (not just the
# ones written this run) lets `remove` cleanly revert a kind switch too.
_MANAGED_KEYS = (
    "inferenceProvider",
    "inferenceGatewayBaseUrl",
    "inferenceCredentialKind",
    "inferenceGatewayApiKey",
    "inferenceCredentialHelper",
    "inferenceCredentialHelperTtlSec",
    "inferenceGatewayAuthScheme",
    "inferenceModels",
)

# Keys gateway-cli itself owns and computes — routing, credential, and model
# selection. site-extra may NEVER set these: overriding them would silently break
# gateway routing (Layer B precedence rule). They are exactly the write set of
# _policy_values that is not the org-attribution passthrough.
_CORE_OWNED_KEYS = frozenset(_MANAGED_KEYS) | {"deploymentOrganizationUuid"}

# The Claude Desktop third-party managed-config keys an operator may inject via
# site-extra: the full documented 3P key set MINUS the core-owned keys above. The
# explicit allowlist means a typo'd or Claude-Code-only key (e.g. "env") is dropped
# with a warning instead of being written where the app silently ignores it.
# Source: https://claude.com/docs/third-party/claude-desktop/configuration
COWORK_EXTRA_ALLOWLIST = frozenset({
    # Connection
    "inferenceCustomHeaders", "inferenceSessionLifetimeSec",
    "inferenceCredentialHelperTimeoutSec", "inferenceCredentialHelperSilentRefreshEnabled",
    "userContentRendererUrl",
    # Gateway auth / OIDC
    "inferenceGatewayOidc", "inferenceGatewayOidcAuthFlow",
    # Models
    "modelDiscoveryEnabled",
    # Authentication surface
    "disableDeploymentModeChooser", "disableDeepLinkRegistration", "microsoftAuthBroker",
    # Surfaces
    "chatTabEnabled", "chatAdvancedFileAnalysisEnabled",
    "isClaudeCodeForDesktopEnabled", "coworkTabEnabled",
    # Workspace
    "disabledBuiltinTools", "disableBundledSkills", "skillCreationEnabled",
    "builtinToolPolicy", "autoModeEnabled", "toolSearchEnabled",
    "allowedWorkspaceFolders", "coworkEgressAllowedHosts", "requireCoworkFullVmSandbox",
    # Connectors & extensions
    "claudeAiImport", "isDesktopExtensionEnabled", "isDesktopExtensionSignatureRequired",
    "managedMcpServers", "mcpPersistentAlwaysAllowEnabled", "isLocalDevMcpEnabled",
    # Telemetry & updates
    "disableEssentialTelemetry", "disableNonessentialTelemetry",
    "disableNonessentialServices", "disableAutoUpdates",
    "autoUpdaterEnforcementHours", "updateViaUpdatesHost",
    # OTLP
    "otlpEndpoint", "otlpProtocol", "otlpHeaders", "otlpResourceAttributes",
    "otlpDesktopLogLevel", "otlpContentCapture", "otlpTracesEnabled",
    # Usage limits
    "inferenceMaxTokensPerWindow", "inferenceTokenWindowHours",
    # Appearance
    "endUserAttribution", "deploymentDisplayName", "deploymentDisplaySubtitle",
    "banner", "disableFeatureDiscovery",
    # Plugins & skills
    "orgPluginSettings",
})


# Every value name a registry *restore* is ever allowed to touch: the core managed
# keys PLUS the whole documented site-extra allowlist (a given run only writes a
# subset, but a persisted snapshot may legitimately name any accepted extra). A
# persisted snapshot naming anything OUTSIDE this set — or targeting a foreign
# subkey/root — is treated as tampering and refused before the elevated restore
# runs (see :func:`_validate_registry_snapshot`, R2-1).
_RESTORABLE_KEY_NAMES = frozenset(_MANAGED_KEYS) | COWORK_EXTRA_ALLOWLIST

# Windows environment roots that live under a single user's profile. A machine
# (HKLM) policy names ONE ``inferenceCredentialHelper`` path for EVERY user, so a
# helper under the installing user's profile is unreadable/unexecutable by everyone
# else — the all-user deployment silently fails auth. Machine scope therefore
# requires a machine-readable helper location (see :func:`_helper_under_user_profile`,
# R2-2).
_USER_PROFILE_ENV_VARS = ("USERPROFILE", "LOCALAPPDATA", "APPDATA")


def _helper_under_user_profile(helper_path: str) -> bool:
    """True if ``helper_path`` resolves under a per-user Windows profile directory.

    Best-effort and env-driven (``USERPROFILE`` / ``LOCALAPPDATA`` / ``APPDATA``)
    so it is exercised on CI too: a machine-scope helper under any of these is
    unreachable by other users on the box. Returns False when none of the vars are
    set (e.g. on macOS/Linux) or the path cannot be resolved.
    """
    try:
        resolved = Path(helper_path).resolve()
    except (OSError, ValueError):
        return False
    for var in _USER_PROFILE_ENV_VARS:
        root = os.environ.get(var)
        if not root:
            continue
        try:
            resolved.relative_to(Path(root).resolve())
            return True
        except (OSError, ValueError):
            continue
    return False


def _all_managed_key_names() -> list[str]:
    """Every value name this tool may write — core keys PLUS accepted site-extra.

    The registry snapshot/restore is ownership-scoped to exactly the names it
    captures: a name not in the snapshot is never touched on revert. So the
    snapshot MUST include the accepted site-extra keys, or a later ``disable`` /
    rollback would leave them orphaned in the policy (they were written but never
    recorded as ours). macOS snapshots the whole file, so it needs no equivalent.
    """
    accepted, _ = resolve_site_extra()
    # dict.fromkeys preserves order and de-dupes if extra somehow repeats a core name.
    return list(dict.fromkeys([*_MANAGED_KEYS, *accepted.keys()]))


def resolve_site_extra() -> tuple[dict, list[str]]:
    """Return ``(accepted, warnings)`` for the site-extra flat-map (Layer B).

    Accepts only keys on :data:`COWORK_EXTRA_ALLOWLIST`; every rejected key gets a
    one-line warning (typo, Claude-Code-only section, or a core-owned key a build
    tried to override). ``accepted`` maps 3P key -> raw JSON value (still native
    Python — serialization to REG_SZ vs configLibrary happens in _policy_values).
    """
    raw = site_extra.raw_extra()
    accepted: dict = {}
    warnings: list[str] = []
    for key, value in raw.items():
        if key in _CORE_OWNED_KEYS:
            warnings.append(
                f"site-extra key {key!r} is owned by gateway-cli and cannot be "
                "overridden — ignored."
            )
            continue
        if key not in COWORK_EXTRA_ALLOWLIST:
            warnings.append(
                f"site-extra key {key!r} is not a recognized Claude Desktop 3P "
                "managed-config key — ignored (the app would silently drop it)."
            )
            continue
        accepted[key] = value
    return accepted, warnings


@dataclass
class CoworkConfig:
    """The resolved values to write into the managed config.

    base_url        — the inference base URL (``inferenceGatewayBaseUrl``); must be
                      a publicly-trusted HTTPS origin (CloudFront). The app rejects
                      HTTP (``originPinned``).
    models          — ``inferenceModels`` roster; first entry is the default.
    credential_kind — ``"helper-script"`` or ``"static"``.
    helper_path     — path to the api-key-helper shim (helper-script kind).
    api_key         — a concrete Virtual Key (static kind).
    helper_ttl_sec  — optional cache TTL for the helper (default app value if None).
    org_uuid        — optional ``deploymentOrganizationUuid`` to stamp if provided
                      (never removes a pre-existing one).
    registry_scope  — Windows only: ``"user"`` writes ``HKCU\\SOFTWARE\\Policies\\
                      Claude`` (per-user, the default), ``"machine"`` writes the
                      same subkey under ``HKLM`` (all users, needs admin; overrides
                      every user's HKCU per the app's precedence rule). Ignored on
                      macOS, which always uses the per-user configLibrary.
    """

    base_url: str
    models: list[str] = field(default_factory=list)
    credential_kind: str = "helper-script"
    helper_path: str | None = None
    api_key: str | None = None
    helper_ttl_sec: int | None = None
    org_uuid: str | None = None
    registry_scope: str = "user"


@dataclass
class ConfigResult:
    """Outcome of a config operation.

    ok       — the operation succeeded (or was already in the desired state).
    changed  — the managed config was modified by this call.
    detail   — a short human-readable status line.
    location — which channel was touched (registry path / config file).
    warnings — non-fatal issues (e.g. an HKLM-precedence conflict).
    """

    ok: bool
    changed: bool
    detail: str
    location: str | None = None
    warnings: list[str] = field(default_factory=list)


# --- Config assembly ---------------------------------------------------------

def build_config(
    *,
    base_url: str | None = None,
    models: list[str] | None = None,
    credential_kind: str = "helper-script",
    helper_path: str | None = None,
    api_key: str | None = None,
    helper_ttl_sec: int | None = None,
    org_uuid: str | None = None,
    registry_scope: str = "user",
) -> CoworkConfig:
    """Assemble a :class:`CoworkConfig`, filling gaps from baked site defaults.

    Raises ``ValueError`` when a required value is missing or invalid so the CLI
    fails fast with a clear message instead of writing a policy the app rejects.
    """
    if registry_scope not in _REGISTRY_SCOPES:
        raise ValueError(
            f"unsupported registry_scope {registry_scope!r} "
            f"(expected one of {', '.join(_REGISTRY_SCOPES)})."
        )
    resolved_url = (base_url or site_defaults.cowork_gateway_url() or "").strip()
    if not resolved_url:
        raise ValueError(
            "no Cowork gateway base URL configured — pass --base-url or bake "
            "COWORK_GATEWAY_URL at build time."
        )
    if not resolved_url.lower().startswith("https://"):
        raise ValueError(
            f"Cowork rejects a non-HTTPS base URL (originPinned): {resolved_url!r}. "
            "Use the CloudFront/HTTPS inference URL."
        )

    resolved_models = models or site_defaults.cowork_default_models()
    if not resolved_models:
        raise ValueError("no inferenceModels roster resolved (empty models list).")

    if credential_kind == "helper-script":
        if not helper_path:
            raise ValueError(
                "credential_kind 'helper-script' requires a helper_path "
                "(the api-key-helper shim)."
            )
        if registry_scope == "machine" and _helper_under_user_profile(helper_path):
            # A machine-wide (HKLM) policy points EVERY user at this one helper path.
            # If it sits under the installing user's profile, other users cannot
            # traverse/execute it, so helper-script auth fails across the supposedly
            # all-user deployment. Require a machine-readable location instead.
            raise ValueError(
                "credential_kind 'helper-script' under the machine (HKLM) scope "
                f"needs a machine-readable helper path, but {helper_path!r} is under "
                "a per-user profile directory that other users cannot execute — the "
                "machine-wide policy would fail authentication for everyone else. "
                "Re-install per-machine (elevated, into Program Files) so the helper "
                "is shared, or use '--scope user' for a per-user policy."
            )
    elif credential_kind == "static":
        if not api_key:
            raise ValueError("credential_kind 'static' requires an api_key (VK).")
        if registry_scope == "machine":
            # HKLM\SOFTWARE\Policies\Claude is readable by EVERY local user, so a
            # concrete VK written there hands them the configuring user's gateway
            # identity, budget, and audit attribution. Refuse it. Machine-wide
            # 'helper-script' is safe: each user's Claude Desktop runs the helper in
            # THEIR own session against THEIR own OIDC cache, minting their own VK.
            raise ValueError(
                "credential_kind 'static' cannot be used with the machine (HKLM) "
                "scope: HKLM is readable by all local users, so the Virtual Key "
                "would leak your gateway identity to everyone on this PC. Use "
                "'--credential-kind helper-script' for a machine-wide policy "
                "(each user mints their own key), or '--scope user' for a per-user "
                "static key."
            )
    else:
        raise ValueError(
            f"unsupported credential_kind {credential_kind!r} "
            "(expected 'helper-script' or 'static')."
        )

    return CoworkConfig(
        base_url=resolved_url,
        models=list(resolved_models),
        credential_kind=credential_kind,
        helper_path=helper_path,
        api_key=api_key,
        helper_ttl_sec=helper_ttl_sec,
        org_uuid=(org_uuid or site_defaults.cowork_org_uuid()),
        registry_scope=registry_scope,
    )


def _extra_value_for_store(value, *, as_string: bool):
    """Serialize a site-extra value for the target store.

    On Windows (``as_string=True``) every value is a REG_SZ string, matching the
    3P write-format spec ("all values written as strings, even booleans and
    arrays"): a bool becomes ``"true"``/``"false"`` (never Python's ``"True"``),
    an int a decimal string, and an object/array a compact JSON string. On macOS
    (``as_string=False``) the native value is kept for the configLibrary JSON.
    """
    if not as_string:
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


def _policy_values(config: CoworkConfig, *, models_as_json_string: bool) -> dict:
    """The managed key/value set for ``config`` (core keys + accepted site-extra).

    ``inferenceModels`` is a JSON-array *string* on Windows (REG_SZ) and a real
    JSON array on macOS — hence ``models_as_json_string``. The Windows string is
    emitted compact (no spaces after separators).

    Layer B precedence: accepted site-extra keys are laid down FIRST, then the
    gateway-cli-owned keys overwrite them, so a site-extra value can never break
    gateway routing. ``resolve_site_extra`` already strips core-owned keys.
    """
    # site-extra first (lowest precedence within the store we write).
    values: dict = {}
    accepted, _ = resolve_site_extra()
    for key, raw_value in accepted.items():
        values[key] = _extra_value_for_store(raw_value, as_string=models_as_json_string)

    models_value = (
        json.dumps(config.models, separators=(",", ":"))
        if models_as_json_string
        else list(config.models)
    )
    # Core gateway-cli-owned keys — always win over site-extra.
    values.update({
        "inferenceProvider": "gateway",
        "inferenceGatewayBaseUrl": config.base_url,
        "inferenceCredentialKind": config.credential_kind,
        "inferenceGatewayAuthScheme": "bearer",
        "inferenceModels": models_value,
    })
    if config.credential_kind == "helper-script":
        values["inferenceCredentialHelper"] = config.helper_path
        if config.helper_ttl_sec is not None:
            values["inferenceCredentialHelperTtlSec"] = config.helper_ttl_sec
    else:  # static
        values["inferenceGatewayApiKey"] = config.api_key
    if config.org_uuid:
        values["deploymentOrganizationUuid"] = config.org_uuid
    return values


def _keys_to_remove(config: CoworkConfig) -> list[str]:
    """Managed keys that must be *cleared* for this credential kind.

    Switching to helper-script must drop a stale static VK, and vice-versa, so no
    inactive credential lingers in the policy.
    """
    if config.credential_kind == "helper-script":
        return ["inferenceGatewayApiKey"]
    return ["inferenceCredentialHelper", "inferenceCredentialHelperTtlSec"]


# --- Marker bookkeeping (what `remove` needs) --------------------------------
# The revert marker is scope-located: user-scope (HKCU) state lives in the
# invoking user's per-user data dir; machine-scope (HKLM) state lives in the
# machine-wide data dir (ProgramData) so a DIFFERENT admin/user can find and
# revert the shared HKLM policy. macOS is always per-user, so it uses "user".

# The scopes whose marker locations `remove`/`clear` must consult, machine first
# (HKLM shadows HKCU, so revert the in-force hive first).
_MARKER_SCOPES = ("machine", "user")

# Machine-scope setup serialization: a lock file next to the machine marker so two
# elevated admins cannot interleave a snapshot/write/marker sequence on the shared
# HKLM policy. A lock older than this many seconds is treated as crash-leftover and
# stolen so a dead process never wedges every future machine-scope setup.
_LOCK_NAME = ".machine-setup.lock"
_LOCK_STALE_SEC = 60.0

# Core keys whose values identify OUR gateway policy. `machine_scope_active`
# fingerprints these to prove the live HKLM policy is the one this tool wrote
# (not a stale/MDM/different gateway policy that merely shadows HKCU).
_FINGERPRINT_KEYS = (
    "inferenceProvider",
    "inferenceGatewayBaseUrl",
    "inferenceModels",
    "inferenceCredentialKind",
)


def _managed_fingerprint(values: dict) -> str:
    """Stable hash over the identifying core keys of a policy value-map.

    Computed identically at write time (over the values we wrote) and at check
    time (over the live hive read), so a match proves the hive still holds exactly
    the config this tool last wrote. Values are the REG_SZ string form on Windows.
    """
    material = {k: values.get(k) for k in _FINGERPRINT_KEYS}
    blob = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _marker_path(scope: str = "user") -> Path:
    return data_dir_for_scope(scope) / _MARKER_NAME


def _write_marker(record: dict, scope: str = "user") -> None:
    path = _marker_path(scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")


def _read_marker(scope: str = "user") -> dict | None:
    path = _marker_path(scope)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def _clear_marker(scope: str = "user") -> None:
    try:
        _marker_path(scope).unlink()
    except FileNotFoundError:
        pass


def _find_active_marker() -> tuple[dict | None, str]:
    """The revert marker in force and its scope, or ``(None, "user")``.

    Consults machine scope first (HKLM shadows HKCU) so ``remove``/``clear`` revert
    whatever hive is actually configured without the caller passing a scope — at
    teardown time the operator does not re-specify it.
    """
    for scope in _MARKER_SCOPES:
        marker = _read_marker(scope)
        if marker is not None:
            return marker, scope
    return None, "user"


def _marker_registry_root(scope: str) -> str | None:
    """The registry root recorded by the ``scope`` revert marker, or None.

    Used to detect a mid-life scope switch: the durable snapshot is captured only
    on the FIRST setup, so if a later setup targets a different hive, ``disable``
    would revert only the originally-recorded one. Best-effort — a missing/gone
    backup returns None.
    """
    marker = _read_marker(scope)
    if not marker or marker.get("kind") != "registry":
        return None
    regbak = marker.get("regbackup_path")
    if not regbak or not Path(regbak).is_file():
        return None
    try:
        return backup_util.load_registry_backup(regbak).root
    except (ValueError, OSError):
        return None


# Well-known SIDs used to lock down the machine-state dir, locale-independent:
#   S-1-5-18      = NT AUTHORITY\SYSTEM
#   S-1-5-32-544  = BUILTIN\Administrators
_ICACLS_SYSTEM_SID = "*S-1-5-18"
_ICACLS_ADMINS_SID = "*S-1-5-32-544"


def _harden_machine_dir() -> tuple[bool, str]:
    """Restrict the machine-wide state dir to Administrators/SYSTEM. Returns (ok, detail).

    ProgramData's *inherited* ACL lets standard users create files, so without this
    a standard user could plant marker/backup state that a later elevated ``disable``
    would trust and replay into HKLM (R2-1). We drop inheritance and grant only
    SYSTEM + Administrators full control (by well-known SID, so it works regardless
    of UI language). Windows-only; a no-op elsewhere (CI/macOS). Fails **closed** —
    the caller refuses machine setup if the dir cannot be secured — so we never
    write elevated rollback state into a world-writable directory.
    """
    directory = machine_data_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"could not create machine state dir {directory}: {exc}"
    if sys.platform != "win32":
        return True, "non-Windows: machine-dir ACL hardening skipped"
    import subprocess  # noqa: PLC0415 — only needed on the machine-scope write path

    try:
        # Copy-then-remove inheritance, then replace the ACL with SYSTEM + Admins.
        subprocess.run(
            ["icacls", str(directory), "/inheritance:r"],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            [
                "icacls", str(directory), "/grant:r",
                f"{_ICACLS_SYSTEM_SID}:(OI)(CI)F",
                f"{_ICACLS_ADMINS_SID}:(OI)(CI)F",
            ],
            check=True, capture_output=True, text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        return False, f"could not secure machine state dir {directory}: {detail}"
    return True, "secured machine state dir (Administrators/SYSTEM only)"


@contextmanager
def _machine_setup_lock(timeout: float = 15.0, poll: float = 0.25):
    """Serialize machine-scope (HKLM) setup across concurrent elevated sessions.

    Exclusive-creates ``machine_data_dir()/.machine-setup.lock``; retries until
    ``timeout`` then raises ``TimeoutError``. A lock older than ``_LOCK_STALE_SEC``
    is stolen (crash-leftover). Released in ``finally``. Dependency-free — no
    portalocker/msvcrt needed; the exclusive create IS the mutex.
    """
    lock = machine_data_dir() / _LOCK_NAME
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            # Steal a stale lock left by a crashed prior run.
            try:
                age = time.time() - lock.stat().st_mtime
                if age > _LOCK_STALE_SEC:
                    lock.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "another machine-scope setup is in progress "
                    f"(lock held at {lock}); retry once it completes."
                )
            time.sleep(poll)
    try:
        yield
    finally:
        try:
            lock.unlink(missing_ok=True)
        except OSError:
            pass


# --- Windows: registry policy ------------------------------------------------

def hklm_conflict() -> bool:
    """True if ``HKLM\\SOFTWARE\\Policies\\Claude`` holds any value (precedence trap).

    When it does, the app ignores HKCU entirely (v1.19367.0+), so an HKCU policy
    we write silently won't apply. Best-effort; returns False off Windows or on
    any detection error.
    """
    if sys.platform != "win32":
        return False
    try:
        import winreg  # noqa: PLC0415

        try:
            handle = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, _WIN_HKLM_SUBKEY, 0, winreg.KEY_READ
            )
        except FileNotFoundError:
            return False
        try:
            value_count = winreg.QueryInfoKey(handle)[1]
            return value_count > 0
        finally:
            winreg.CloseKey(handle)
    except Exception as exc:  # noqa: BLE001 — detection must never raise
        log.debug("hklm_check_failed", error=str(exc))
        return False


def machine_scope_active() -> bool:
    """True when ``HKLM`` holds OUR gateway policy (the intentional machine-wide mode).

    Ownership is proven, not inferred: (1) a machine-scope revert marker exists in
    the machine-wide data dir — i.e. THIS tool wrote HKLM — and (2) the live HKLM
    values still match the fingerprint recorded at write time. A stale, MDM-managed,
    or *different* gateway policy has no marker (or a mismatched fingerprint) and is
    therefore NOT treated as active, so ``verify`` reports it as the precedence trap
    it is instead of a false pass. Best-effort; False off Windows or on read error.
    """
    if sys.platform != "win32":
        return False
    marker = _read_marker("machine")
    if not marker or marker.get("kind") != "registry":
        return False
    try:
        import winreg  # noqa: PLC0415

        values = _read_hive_values(winreg.HKEY_LOCAL_MACHINE, _WIN_SUBKEY)
    except Exception as exc:  # noqa: BLE001 — detection must never raise
        log.debug("machine_scope_check_failed", error=str(exc))
        return False
    if not values or values.get("inferenceProvider") != "gateway":
        return False
    expected = marker.get("fingerprint")
    # Older markers predate the fingerprint; a marker + provider match is enough.
    if not expected:
        return True
    return _managed_fingerprint(values) == expected


def _write_windows(config: CoworkConfig) -> ConfigResult:
    """Write the Windows registry policy, serializing machine (HKLM) scope.

    Machine scope mutates the shared HKLM hive plus machine-wide rollback state, so
    the whole snapshot→write→marker sequence runs under a cross-process lock; two
    concurrent elevated admins can therefore never interleave. Per-user (HKCU) scope
    is unaffected — its state is private, so it stays lock-free.
    """
    if config.registry_scope == "machine":
        try:
            with _machine_setup_lock():
                return _write_windows_impl(config)
        except TimeoutError as exc:
            return ConfigResult(
                False, False, str(exc),
                location=f"HKLM\\{_WIN_SUBKEY}",
            )
    return _write_windows_impl(config)


def _write_windows_impl(config: CoworkConfig) -> ConfigResult:
    import winreg  # noqa: PLC0415

    scope = config.registry_scope
    root_alias = _win_root_for_scope(scope)
    hive = _win_hive_for_scope(scope)
    other_scope = "user" if scope == "machine" else "machine"

    warnings: list[str] = []
    _, extra_warnings = resolve_site_extra()
    warnings.extend(extra_warnings)

    # Fail-closed on a scope switch. The durable pre-setup snapshot is captured
    # only on the first setup for a given scope, so writing a DIFFERENT hive while
    # the other scope still holds a gateway policy would leave that other hive
    # orphaned — and an orphaned HKLM policy overrides EVERY user's HKCU forever,
    # even after this tool is uninstalled. We check BOTH signals (R2-3): the other
    # scope's revert marker AND the live opposite hive. A live gateway policy with
    # no marker (version skew / corruption / manual cleanup) still blocks the
    # switch — otherwise an HKCU→HKLM switch could silently strand HKCU (it
    # reactivates after `disable`), and an HKLM→HKCU switch would write a policy
    # that stays shadowed while setup reports success.
    other_marker = _read_marker(other_scope)
    other_live_policy = _hive_has_gateway_policy(other_scope)
    if other_marker is not None or other_live_policy:
        other_root = _win_root_for_scope(other_scope)
        detail = (
            f"a gateway policy is present in the {other_root} scope "
            f"({other_root}\\{_WIN_SUBKEY}); switching to {root_alias} now would "
            f"leave that {other_root} policy in place with no clean revert."
        )
        if other_marker is None:
            # Markerless: `disable` cannot revert it automatically — the operator
            # must adopt or clear it by hand before switching.
            detail += (
                f" Its rollback marker is missing, so `disable` cannot revert it "
                f"automatically — clear {other_root}\\{_WIN_SUBKEY} manually, then "
                f"re-run setup with the {scope} scope."
            )
        else:
            detail += f" Run `disable` first, then re-run setup with the {scope} scope."
        return ConfigResult(
            False, False, detail,
            location=f"{root_alias}\\{_WIN_SUBKEY}",
            warnings=warnings,
        )

    if scope == "machine":
        # Machine-scope rollback state is written into %ProgramData% and later
        # replayed by an ELEVATED `disable`, so the dir must be Administrators/
        # SYSTEM-only before we drop any marker/snapshot in it (R2-1). Fail closed
        # if it cannot be secured — never trust a world-writable state dir.
        ok, detail = _harden_machine_dir()
        if not ok:
            return ConfigResult(
                False, False,
                f"refusing machine-scope setup: {detail}. The machine-wide rollback "
                "state must be writable only by Administrators/SYSTEM.",
                location=f"{root_alias}\\{_WIN_SUBKEY}",
                warnings=warnings,
            )
        # Writing HKLM is the deliberate machine-wide mode. Make its blast radius
        # explicit: it applies to every user and, per the app's precedence rule,
        # shadows any per-user HKCU policy those users may have set.
        warnings.append(
            "Writing a MACHINE-WIDE policy to HKLM\\SOFTWARE\\Policies\\Claude — it "
            "applies to every user on this PC and OVERRIDES any per-user HKCU policy "
            "(v1.19367.0+). Use `setup --scope user` for a per-user policy instead."
        )
    elif hklm_conflict():
        warnings.append(
            "HKLM\\SOFTWARE\\Policies\\Claude has values — the app IGNORES HKCU "
            "entirely (v1.19367.0+). This HKCU policy will NOT take effect until "
            "HKLM is cleared, or re-run with `setup --scope machine`."
        )

    values = _policy_values(config, models_as_json_string=True)
    fingerprint = _managed_fingerprint(
        {k: str(v) for k, v in values.items()}
    )
    remove = _keys_to_remove(config)

    # Snapshot the prior state of every key we may write or clear, so `remove`
    # reverts exactly. Capture the baseline ONLY on the first setup: a marker means
    # a prior setup already recorded the true pre-setup state. Re-snapshotting on a
    # repeat setup would capture the already-configured hive as the baseline, so a
    # later `disable` would revert to a dirty state and never clean up. The snapshot
    # records the root it captured, so `remove` reverts the SAME hive we wrote, and
    # its persisted backup lands in this scope's data dir (machine-wide for HKLM).
    existing = _read_marker(scope)
    if existing is None:
        snapshot = backup_util.snapshot_registry_values(
            _TOOL_NAME, root_alias, _WIN_SUBKEY, _all_managed_key_names(),
            backup_dir=data_dir_for_scope(scope),
        )
        if snapshot is not None and snapshot.backup_path:
            _write_marker(
                {
                    "kind": "registry",
                    "regbackup_path": snapshot.backup_path,
                    "root": root_alias,
                    "fingerprint": fingerprint,
                },
                scope,
            )
    else:
        # Repeat same-scope setup (credential/model refresh): keep the first-setup
        # baseline, but refresh the ownership fingerprint to the config we now write
        # so `machine_scope_active` still recognizes the live hive as ours.
        existing["fingerprint"] = fingerprint
        _write_marker(existing, scope)

    try:
        key = winreg.CreateKeyEx(hive, _WIN_SUBKEY, 0, winreg.KEY_ALL_ACCESS)
    except PermissionError:
        # SOFTWARE\Policies is ACL-protected under BOTH roots: standard-user tokens
        # get only ReadKey there (policy keys are admin-controlled), so creating the
        # Claude subkey needs an elevated token. For the per-user (HKCU) scope,
        # same-user UAC elevation is safe — HKCU still resolves to this user's own
        # hive; the machine (HKLM) scope simply requires admin. Surface an
        # actionable message instead of a bare WinError 5.
        return ConfigResult(
            False,
            False,
            "config write needs elevation: access denied writing "
            f"{root_alias}\\{_WIN_SUBKEY}. This registry policy key is "
            "admin-controlled. Re-run this command from an elevated prompt "
            "(right-click → \"Run as administrator\").",
            location=f"{root_alias}\\{_WIN_SUBKEY}",
            warnings=warnings,
        )
    try:
        for name in remove:
            try:
                winreg.DeleteValue(key, name)
            except FileNotFoundError:
                pass
        for name, value in values.items():
            # Per the Claude Desktop 3P managed-config spec, EVERY value in an OS
            # preference store is written as a string — integers as decimal strings
            # (e.g. "3600"), not native REG_DWORD, which the app would read back as
            # the wrong type.
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, str(value))
    finally:
        winreg.CloseKey(key)

    location = f"{root_alias}\\{_WIN_SUBKEY}"
    return ConfigResult(
        True, True, f"wrote {len(values)} gateway keys to {location}",
        location=location, warnings=warnings,
    )


def _read_hive_values(hive, subkey) -> dict | None:
    """Read all values under ``hive\\subkey``; None if the key is absent/unreadable."""
    import winreg  # noqa: PLC0415

    try:
        handle = winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ)
    except FileNotFoundError:
        return None
    out: dict = {}
    try:
        count = winreg.QueryInfoKey(handle)[1]
        for i in range(count):
            name, value, _ = winreg.EnumValue(handle, i)
            out[name] = value
    finally:
        winreg.CloseKey(handle)
    return out


def _hive_has_gateway_policy(scope: str) -> bool:
    """True if the LIVE hive for ``scope`` holds a gateway (3P) policy.

    Reads the actual registry, independent of any marker, so a scope switch can be
    refused even when the other scope's marker was lost (version skew, corruption,
    manual cleanup) while its hive still strands/shadows a gateway policy (R2-3).
    Best-effort; False off Windows or on any read error.
    """
    if sys.platform != "win32":
        return False
    try:
        # _win_hive_for_scope / _read_hive_values import winreg internally.
        values = _read_hive_values(_win_hive_for_scope(scope), _WIN_SUBKEY)
    except Exception as exc:  # noqa: BLE001 — detection must never raise
        log.debug("opposite_hive_read_failed", scope=scope, error=str(exc))
        return False
    return bool(values and values.get("inferenceProvider") == "gateway")


def _effective_windows_root() -> str | None:
    """The root the app actually reads: ``"HKLM"`` if it holds any value, else the
    per-user ``"HKCU"`` if that holds any, else None (unconfigured).

    Mirrors the app's precedence (HKLM shadows HKCU), so status/verify report the
    hive that is actually in force regardless of which scope setup wrote.
    """
    try:
        import winreg  # noqa: PLC0415
    except ImportError:
        return None
    if _read_hive_values(winreg.HKEY_LOCAL_MACHINE, _WIN_SUBKEY):
        return "HKLM"
    if _read_hive_values(winreg.HKEY_CURRENT_USER, _WIN_SUBKEY):
        return "HKCU"
    return None


def _read_windows() -> dict | None:
    try:
        import winreg  # noqa: PLC0415

        # Read the hive the app honors: HKLM wins over HKCU when it has values.
        hklm = _read_hive_values(winreg.HKEY_LOCAL_MACHINE, _WIN_SUBKEY)
        if hklm:
            return hklm
        return _read_hive_values(winreg.HKEY_CURRENT_USER, _WIN_SUBKEY)
    except Exception as exc:  # noqa: BLE001
        log.debug("read_windows_failed", error=str(exc))
        return None


# --- macOS: configLibrary ----------------------------------------------------

def _mac_app_support() -> Path:
    return Path.home() / "Library" / "Application Support" / "Claude-3p"


def _mac_config_dir() -> Path:
    return _mac_app_support() / "configLibrary"


def _mac_applied_config_path() -> Path:
    """The active ``<uuid>.json`` path; creates the configLibrary skeleton if absent.

    Mirrors ``cowork-test.sh applied_config_path``: ``_meta.json``'s ``appliedId``
    selects the active entry; if there is no meta yet we create one with a fresh
    lowercase UUID.
    """
    import uuid as uuidlib  # noqa: PLC0415

    cfg_dir = _mac_config_dir()
    meta = cfg_dir / "_meta.json"
    if meta.is_file():
        applied = json.loads(meta.read_text(encoding="utf-8"))["appliedId"]
    else:
        cfg_dir.mkdir(parents=True, exist_ok=True)
        applied = str(uuidlib.uuid4()).lower()
        meta.write_text(
            json.dumps(
                {"appliedId": applied, "entries": [{"id": applied, "name": "Default"}]},
                indent=2,
            ),
            encoding="utf-8",
        )
    return cfg_dir / f"{applied}.json"


def _mac_applied_config_path_readonly() -> Path | None:
    """The active ``<uuid>.json`` path WITHOUT creating the configLibrary skeleton.

    Unlike :func:`_mac_applied_config_path`, this never writes ``_meta.json`` or
    the dir — it is for the read-only pre-mutation snapshot in :func:`plan_write`.
    Returns None when there is no ``_meta.json`` yet (nothing has been applied), in
    which case ``apply`` will create both and the compensation re-resolves after.
    """
    meta = _mac_config_dir() / "_meta.json"
    if not meta.is_file():
        return None
    try:
        applied = json.loads(meta.read_text(encoding="utf-8"))["appliedId"]
    except (ValueError, OSError, KeyError):
        return None
    return _mac_config_dir() / f"{applied}.json"


def _write_macos(config: CoworkConfig) -> ConfigResult:
    cfg = _mac_applied_config_path()
    existed = cfg.is_file()

    data: dict = {}
    if existed:
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            data = {}

    # Snapshot the pre-setup config + write the revert marker ONLY on the first
    # setup. On a repeat setup a marker already records the true pre-setup state;
    # re-snapshotting the already-configured file would poison `disable` so it
    # reverts to a gateway-configured state instead of clean (mirrors Windows above).
    if _read_marker() is None:
        # Full-file snapshot before we merge — ownership-preserving on restore.
        entry = backup_util.backup_config(_TOOL_NAME, cfg) if existed else None
        _write_marker({
            "kind": "file",
            "config_path": str(cfg),
            "backup_path": entry.backup_path if entry else None,
            "existed": existed,
        })

    # Drop bedrock-direct keys when switching to gateway mode; preserve others.
    for k in list(data):
        if k.startswith("inferenceBedrock"):
            data.pop(k)
    for name in _keys_to_remove(config):
        data.pop(name, None)

    data.update(_policy_values(config, models_as_json_string=False))
    cfg.write_text(json.dumps(data, indent=2), encoding="utf-8")

    _, extra_warnings = resolve_site_extra()
    return ConfigResult(
        True, True, f"wrote gateway config to {cfg}",
        location=str(cfg), warnings=extra_warnings,
    )


def _read_macos() -> dict | None:
    cfg_dir = _mac_config_dir()
    meta = cfg_dir / "_meta.json"
    if not meta.is_file():
        return None
    try:
        applied = json.loads(meta.read_text(encoding="utf-8"))["appliedId"]
        cfg = cfg_dir / f"{applied}.json"
        if not cfg.is_file():
            return None
        return json.loads(cfg.read_text(encoding="utf-8"))
    except (ValueError, OSError, KeyError):
        return None


# --- Public operations -------------------------------------------------------

def write_config(config: CoworkConfig) -> ConfigResult:
    """Write the managed config to the OS-native channel for this platform."""
    try:
        if sys.platform == "win32":
            return _write_windows(config)
        if sys.platform == "darwin":
            return _write_macos(config)
        return ConfigResult(
            False, False,
            "Cowork managed config is only supported on Windows and macOS.",
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("write_config_failed", error=str(exc))
        return ConfigResult(False, False, f"config write failed: {exc}")


@dataclass
class ConfigWritePlan:
    """Invocation-scoped plan/apply/compensate for a single managed-config write.

    This is the **in-process, invocation-scoped** undo for :func:`write_config`,
    layered on top of the durable marker/backup tier. :func:`plan_write` captures a
    fresh pre-mutation snapshot of *this run's* starting state; :meth:`apply` does
    the write; :meth:`compensate` restores that snapshot — reverting only *this
    run's* delta, NOT the first-setup baseline that :func:`remove_config` reverts to.

    The snapshot is taken in :func:`plan_write` (BEFORE any live mutation) so a
    write that partially mutates and then fails is still covered. :meth:`compensate`
    returns a :class:`ConfigResult` so the rollback loop can check ``.ok`` rather
    than only catching exceptions.

    The invocation snapshot is deliberately in-memory (``persist=False`` on
    Windows) and separate from the durable regbak/`.bak`/marker that ``write_config``
    writes on the first setup — the durable tier is left on disk so crash-recovery
    via ``clear`` still works even if we die mid-rollback.
    """

    config: CoworkConfig
    # Windows: in-memory snapshot of _MANAGED_KEYS' prior state (this run's start).
    _win_snapshot: object | None = None
    # macOS: (config_path, prior_existed, prior_text) captured read-only at plan.
    _mac_path: Path | None = None
    _mac_existed: bool = False
    _mac_prior_text: str | None = None
    _applied: bool = False

    def apply(self) -> ConfigResult:
        """Perform the managed-config write (delegates to :func:`write_config`)."""
        self._applied = True
        return write_config(self.config)

    def compensate(self) -> ConfigResult:
        """Revert this run's write to the pre-mutation snapshot (invocation-scoped).

        Idempotent and no-op-safe: restoring twice, or reverting an already-clean
        key/file, is harmless. Never touches the durable marker/backup.
        """
        try:
            if sys.platform == "win32":
                if self._win_snapshot is None:
                    return ConfigResult(True, False,
                                        "no pre-setup registry snapshot — nothing to undo.")
                changed = backup_util.restore_registry_values(self._win_snapshot)
                # Label the hive we actually snapshotted (matches the write scope).
                root_alias = _win_root_for_scope(self.config.registry_scope)
                return ConfigResult(
                    True, changed,
                    "reverted registry policy to this run's pre-setup state",
                    location=f"{root_alias}\\{_WIN_SUBKEY}",
                )
            if sys.platform == "darwin":
                return self._compensate_macos()
            return ConfigResult(False, False, "unsupported platform")
        except Exception as exc:  # noqa: BLE001 — surface as not-ok, never mask
            log.debug("config_compensate_failed", error=str(exc))
            return ConfigResult(False, False, f"config rollback failed: {exc}")

    def _compensate_macos(self) -> ConfigResult:
        if self._mac_existed:
            # We overwrote a file that existed before this run — put it back.
            if self._mac_path is not None and self._mac_prior_text is not None:
                self._mac_path.write_text(self._mac_prior_text, encoding="utf-8")
                return ConfigResult(True, True, "restored config file to this run's start",
                                    location=str(self._mac_path))
            return ConfigResult(False, False, "prior config text unavailable")
        # The file did not exist at plan time — this run created it; remove what we
        # created (re-resolve read-only in case apply() created the _meta skeleton).
        cfg = self._mac_path or _mac_applied_config_path_readonly()
        if cfg is not None and cfg.is_file():
            cfg.unlink(missing_ok=True)
            return ConfigResult(True, True, "removed config file created by this run",
                                location=str(cfg))
        return ConfigResult(True, False, "no config file to remove")


def plan_write(config: CoworkConfig) -> ConfigWritePlan:
    """Capture this run's pre-mutation state and return an invocation-scoped plan.

    READ-ONLY: takes a snapshot of the current managed-config state (Windows
    registry values / macOS config file) without mutating anything, so the caller
    can arm :meth:`ConfigWritePlan.compensate` BEFORE calling
    :meth:`ConfigWritePlan.apply`. See :class:`ConfigWritePlan`.
    """
    plan = ConfigWritePlan(config=config)
    if sys.platform == "win32":
        # In-memory (persist=False) — the durable snapshot is taken by write_config
        # itself on the first setup; this one is only for this run's rollback.
        plan._win_snapshot = backup_util.snapshot_registry_values(
            _TOOL_NAME,
            _win_root_for_scope(config.registry_scope),
            _WIN_SUBKEY,
            _all_managed_key_names(),
            persist=False,
        )
    elif sys.platform == "darwin":
        cfg = _mac_applied_config_path_readonly()
        plan._mac_path = cfg
        if cfg is not None and cfg.is_file():
            plan._mac_existed = True
            try:
                plan._mac_prior_text = cfg.read_text(encoding="utf-8")
            except OSError:
                plan._mac_prior_text = None
    return plan


def read_config() -> dict | None:
    """Return the current managed config values, or None if unconfigured."""
    if sys.platform == "win32":
        return _read_windows()
    if sys.platform == "darwin":
        return _read_macos()
    return None


def config_location() -> str:
    """The OS-native channel this tool writes to, for display in status/verify.

    Windows: the registry policy key (``HKCU\\SOFTWARE\\Policies\\Claude``) — the
    exact equivalent of the file path Claude Code's verify prints for its
    ``managed-settings.json``. macOS: the active ``configLibrary/<uuid>.json``
    path (read-only resolve — never creates the skeleton just to name it).
    """
    if sys.platform == "win32":
        # Name the hive actually in force (HKLM if machine-wide, else HKCU); fall
        # back to the per-user default label when nothing is configured yet.
        root = _effective_windows_root() or _WIN_ROOT
        return f"{root}\\{_WIN_SUBKEY}"
    if sys.platform == "darwin":
        cfg = _mac_applied_config_path_readonly()
        return str(cfg) if cfg is not None else str(_mac_config_dir())
    return "(unsupported platform)"


def is_gateway_enabled() -> bool:
    """True when the managed config currently selects gateway (3P) mode."""
    current = read_config()
    return bool(current and current.get("inferenceProvider") == "gateway")


def _backup_within_scope_dir(regbackup_path: str, scope: str) -> bool:
    """True if ``regbackup_path`` resolves INSIDE this scope's ``backups`` dir.

    The machine marker is shared, world-readable state; refusing a
    ``regbackup_path`` that points outside ``%ProgramData%\\...\\backups`` stops a
    redirected marker from feeding an attacker-chosen file into the elevated
    restore (R2-1). Honours the (release-locked) ``GATEWAY_CLI_BACKUP_DIR`` override
    so it agrees with where the snapshot was actually persisted.
    """
    override = _release_locked_env("GATEWAY_CLI_BACKUP_DIR")
    base = Path(override) if override else data_dir_for_scope(scope) / "backups"
    try:
        Path(regbackup_path).resolve().relative_to(base.resolve())
        return True
    except (OSError, ValueError):
        return False


def _validate_registry_snapshot(snapshot, *, expected_root: str) -> str | None:
    """Return a rejection reason if a persisted snapshot is unsafe to restore, else None.

    Defends the machine (HKLM) rollback trust boundary (R2-1): ``remove_config``
    runs elevated, so a snapshot whose location or contents were tampered with (or
    that simply belongs to a different tool) must never drive
    :func:`restore_registry_values`. A safe snapshot targets EXACTLY our managed
    policy subkey under the root this scope owns, and names only value names this
    tool can own — never an attacker-chosen key or value elsewhere in the hive.
    """
    if snapshot.root != expected_root:
        return (
            f"snapshot root {snapshot.root!r} does not match the expected "
            f"{expected_root!r} for this scope"
        )
    if snapshot.subkey != _WIN_SUBKEY:
        return (
            f"snapshot targets {snapshot.subkey!r}, not the managed policy key "
            f"{_WIN_SUBKEY!r}"
        )
    stray = set(snapshot.values) - _RESTORABLE_KEY_NAMES
    if stray:
        return "snapshot names value(s) this tool never writes: " + ", ".join(
            sorted(stray)
        )
    return None


def remove_config() -> ConfigResult:
    """Revert the managed config to the exact state before this tool wrote it.

    Finds the marker in force (machine scope first — HKLM shadows HKCU — then
    per-user) without the caller passing a scope: at teardown the operator does not
    re-specify it. On Windows the registry snapshot is restored (only our values
    reverted); on macOS the prior config file is put back (or removed if there was
    none). A machine-scope revert runs under the setup lock so it cannot race a
    concurrent machine-scope setup. No-op with a clear message when this tool never
    wrote anything.
    """
    marker, scope = _find_active_marker()
    if marker is None:
        return ConfigResult(
            True, False, "no config recorded by this tool — nothing to remove."
        )
    if scope == "machine":
        try:
            with _machine_setup_lock():
                return _remove_config_from_marker(marker, scope)
        except TimeoutError as exc:
            return ConfigResult(False, False, str(exc))
    return _remove_config_from_marker(marker, scope)


def _remove_config_from_marker(marker: dict, scope: str) -> ConfigResult:
    try:
        if marker.get("kind") == "registry":
            regbackup_path = marker.get("regbackup_path")
            # The root this scope owns — derived from the scope, NOT read from the
            # (potentially tampered) marker/snapshot, so it anchors validation.
            expected_root = _win_root_for_scope(scope)
            if not regbackup_path or not Path(regbackup_path).is_file():
                return ConfigResult(
                    False, False,
                    f"registry backup is gone; revert {expected_root}\\{_WIN_SUBKEY} "
                    "manually.",
                )
            # R2-1: never feed an elevated restore a backup outside our own state
            # dir, nor one that targets a foreign key/value. Both are refused before
            # `restore_registry_values` touches the registry.
            if not _backup_within_scope_dir(regbackup_path, scope):
                return ConfigResult(
                    False, False,
                    f"refusing to restore from a backup outside the {scope}-scope "
                    f"state directory ({regbackup_path}); revert "
                    f"{expected_root}\\{_WIN_SUBKEY} manually if needed.",
                )
            snapshot = backup_util.load_registry_backup(regbackup_path)
            reason = _validate_registry_snapshot(snapshot, expected_root=expected_root)
            if reason:
                return ConfigResult(
                    False, False,
                    f"refusing to restore a registry snapshot that is not tool-owned: "
                    f"{reason}. Revert {expected_root}\\{_WIN_SUBKEY} manually if needed.",
                )
            changed = backup_util.restore_registry_values(snapshot)
            _clear_marker(scope)
            return ConfigResult(
                True, changed,
                "reverted registry policy to its pre-setup state",
                location=f"{snapshot.root}\\{_WIN_SUBKEY}",
            )
        if marker.get("kind") == "file":
            cfg = Path(marker["config_path"])
            if not marker.get("existed"):
                # We created it — remove it.
                changed = cfg.is_file()
                cfg.unlink(missing_ok=True)
                _clear_marker(scope)
                return ConfigResult(
                    True, changed, "removed config file (did not exist before setup)",
                    location=str(cfg),
                )
            backup_path = marker.get("backup_path")
            if not backup_path or not Path(backup_path).is_file():
                return ConfigResult(
                    False, False,
                    f"prior config backup is gone; revert {cfg} manually.",
                )
            cfg.write_text(
                Path(backup_path).read_text(encoding="utf-8"), encoding="utf-8"
            )
            _clear_marker(scope)
            return ConfigResult(
                True, True, "restored config file from pre-setup backup",
                location=str(cfg),
            )
        return ConfigResult(False, False, f"unknown backup kind {marker.get('kind')!r}")
    except Exception as exc:  # noqa: BLE001
        log.debug("remove_config_failed", error=str(exc))
        return ConfigResult(False, False, f"config remove failed: {exc}")


def marker_exists() -> bool:
    """True when a config-revert marker is still on disk (setup ran, no disable yet).

    Used by ``clear`` to enforce ordering: the marker/backup sweep must run AFTER
    ``remove_config`` has consumed the marker, so a rollback snapshot is never
    destroyed while it is still the only way back to the pre-setup state.
    """
    return any(_marker_path(s).is_file() for s in _MARKER_SCOPES)


def sweep_backups() -> list[Path]:
    """Delete the marker + backup files this tool wrote in its own data dirs.

    Removes, and only removes, files under an explicit **allowlist** rooted at the
    CLI's own data dirs — BOTH the per-user dir (HKCU/macOS state) and the
    machine-wide dir (HKLM state), so a machine-scope teardown clears its
    ProgramData snapshots too:
      • the config-revert marker (``cowork-config-backup.json``);
      • every registry/config snapshot in ``<data-dir>/backups/`` this tool wrote
        (``cowork.*.regbak.json`` and ``cowork.*.bak`` produced by
        :mod:`cli.utils.backup` — the ``cowork.`` prefix scopes the sweep to our
        own files even when the backup dir is shared with another tool or user).

    It never touches anything outside those data dirs — in particular it can never
    reach the app-owned ``claude_desktop_config.json`` or the Claude package dir.
    Returns the list of paths actually removed. Best-effort: a file that vanishes
    or refuses to delete is skipped, not raised (a non-admin ``clear`` may lack
    permission to remove the machine-wide ProgramData files — that is fine, the
    sweep just skips them).

    Callers that also want the managed config reverted must run
    :func:`remove_config` FIRST (see :func:`marker_exists`) — this only sweeps the
    on-disk snapshots, it does not revert the registry/config itself.
    """
    removed: list[Path] = []

    # The revert marker in each scope's data-dir root.
    for scope in _MARKER_SCOPES:
        marker = _marker_path(scope)
        if marker.is_file():
            try:
                marker.unlink()
                removed.append(marker)
            except OSError as exc:
                log.debug("sweep_marker_failed", path=str(marker), error=str(exc))

    # Snapshot backups under each <data-dir>/backups/ — honour the same override the
    # backup module uses so a redirected backup dir is still swept. When the override
    # is set, both scopes resolve to it; de-dupe so we sweep it once.
    #
    # BOTH globs are ownership-scoped to the ``cowork.`` prefix that
    # :mod:`cli.utils.backup` stamps onto every file it writes
    # (``cowork.<name>.<ts>.bak`` / ``cowork.<key>.<ts>.regbak.json``). This matters
    # because GATEWAY_CLI_BACKUP_DIR may point at a directory SHARED with the Claude
    # Code CLI (which writes ``claude-code.*.bak`` in the same format) or another
    # user — an unscoped ``*.bak`` would delete their snapshots. The relative_to()
    # check below only prevents directory escape; the prefix is what establishes
    # ownership.
    override = os.environ.get("GATEWAY_CLI_BACKUP_DIR")
    if override:
        backup_dirs = [Path(override)]
    else:
        backup_dirs = [data_dir() / "backups", machine_data_dir() / "backups"]

    seen: set[Path] = set()
    for backup_dir in backup_dirs:
        try:
            resolved_dir = backup_dir.resolve()
        except OSError:
            resolved_dir = backup_dir
        if resolved_dir in seen:
            continue
        seen.add(resolved_dir)
        if not backup_dir.is_dir():
            continue
        for pattern in ("cowork.*.regbak.json", "cowork.*.bak"):
            for path in sorted(backup_dir.glob(pattern)):
                # Defence in depth: never follow a glob outside the backups dir.
                try:
                    path.resolve().relative_to(resolved_dir)
                except ValueError:
                    continue
                try:
                    path.unlink()
                    removed.append(path)
                except OSError as exc:
                    log.debug("sweep_backup_failed", path=str(path), error=str(exc))

    return removed


# ============================================================================
# Corporate proxy configuration (single source of truth for cli.verify)
# ============================================================================
# The authoritative proxy values `cli.verify` validates against. All three are
# environment-specific, so they are injected at build time into
# `cli/_site_config.py` (written by build.ps1 from -ExpectedProxyUrl /
# -NoProxyValue / -ForbiddenNoProxyToken or the matching env vars). The literal
# fallbacks below are generic placeholders so no real corporate address is
# committed to source; a dev checkout runs against them harmlessly.
#
# EXPECTED_PROXY_URL: the forward-proxy address HTTP_PROXY/HTTPS_PROXY must equal.
# NO_PROXY_VALUE: the bypass list gateway-cli owns — corporate domain suffixes and
#   internal CIDR ranges reached DIRECTLY. It must never contain
#   FORBIDDEN_NO_PROXY_TOKEN: the gateway/OIDC endpoints live under that suffix but
#   are reached via the CIDR ranges, so listing the suffix as direct breaks routing.
EXPECTED_PROXY_URL = site_defaults._baked("EXPECTED_PROXY_URL", "http://proxy.example.com:8080")
FORBIDDEN_NO_PROXY_TOKEN = site_defaults._baked("FORBIDDEN_NO_PROXY_TOKEN", ".example.net")
NO_PROXY_VALUE = site_defaults._baked(
    "NO_PROXY_VALUE",
    "localhost,10.0.0.0/8,192.168.0.0/16,.example.com",
)
