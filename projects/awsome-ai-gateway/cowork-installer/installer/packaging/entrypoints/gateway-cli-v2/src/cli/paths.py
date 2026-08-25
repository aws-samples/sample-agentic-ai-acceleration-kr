# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""OS-specific data directory paths for gateway-cli-v2.

All runtime files (token cache, VK cache, config) live in the platform-native
user-data directory so they end up in the right place on every OS:

  macOS   ~/Library/Application Support/gateway-cli-cowork/
  Linux   ~/.local/share/gateway-cli-cowork/
  Windows C:\\Users\\<user>\\AppData\\Local\\gateway-cli-cowork\\

Individual paths can be overridden by environment variables (useful for tests
and for pointing at the legacy ~/.gateway-cli/ location when migrating).
"""

from __future__ import annotations

import os
import shutil
import sys
import sysconfig
from pathlib import Path

from platformdirs import site_data_dir, user_data_dir

_APP_NAME = "gateway-cli-cowork"
_APP_AUTHOR = False  # do not create a vendor subdirectory on Windows


def _release_locked_env(name: str) -> str | None:
    """Return env override ``name``, or None in a frozen (release) build.

    Machine-state path overrides — ``GATEWAY_CLI_MACHINE_DATA_DIR`` and
    ``GATEWAY_CLI_BACKUP_DIR`` — are a redirection vector for the *elevated*
    machine-scope rollback: a standard user who can set one could point a later
    admin ``disable`` at attacker-controlled marker/backup state (see the
    registry-scope hardening plan, R2-1). They exist only for tests and non-default
    staging, so they are ignored once the CLI is frozen into the shipped exe —
    the shipped machine path is then fixed at ``%ProgramData%`` and cannot be moved
    by an unprivileged caller's environment.
    """
    if getattr(sys, "frozen", False):
        return None
    return os.environ.get(name)


def prog_name() -> str:
    """The command name to show in user-facing hints (e.g. "run `X setup`").

    Derived from the invoked executable (``sys.argv[0]`` basename, extension
    stripped) — the same source Click uses for its usage line — so hints stay
    correct for the Cowork exe (``gateway-cli-cowork``) as well as any other
    name the binary is shipped under. Falls back to the app name when argv[0]
    is empty (embedded/REPL use)."""
    if sys.argv and sys.argv[0]:
        name = os.path.splitext(os.path.basename(sys.argv[0]))[0]
        if name:
            return name
    return _APP_NAME


def data_dir() -> Path:
    """Return the platform-native user data directory for gateway-cli."""
    override = os.environ.get("GATEWAY_CLI_DATA_DIR")
    if override:
        return Path(override)
    return Path(user_data_dir(_APP_NAME, appauthor=_APP_AUTHOR))


def machine_data_dir() -> Path:
    """Return the machine-wide (all-users) data directory for gateway-cli.

    Used ONLY for machine-scope (HKLM) state — the ownership marker, rollback
    snapshot, and setup lock. HKLM is shared by every user on the box, so its
    rollback state must live somewhere every (admin) user can find it, not in the
    invoking user's LocalAppData. On Windows this resolves to
    ``%ProgramData%\\gateway-cli-cowork``. ProgramData's *inherited* ACL still lets
    standard users create files, so ``cli.managed`` resets this dir's ACL to
    Administrators/SYSTEM-only on the first machine-scope write (see
    ``_harden_machine_dir``); the elevated rollback cannot be trusted to a dir a
    standard user could plant state in.

    ``GATEWAY_CLI_MACHINE_DATA_DIR`` overrides it for tests / non-default staging,
    but is IGNORED in a frozen release build (:func:`_release_locked_env`) so an
    unprivileged caller's environment cannot redirect the elevated machine-scope
    rollback.
    """
    override = _release_locked_env("GATEWAY_CLI_MACHINE_DATA_DIR")
    if override:
        return Path(override)
    return Path(site_data_dir(_APP_NAME, appauthor=_APP_AUTHOR))


def data_dir_for_scope(scope: str) -> Path:
    """Data dir for a registry scope: machine-wide for ``"machine"``, else per-user.

    ``"machine"`` → :func:`machine_data_dir` (ProgramData); anything else (incl.
    ``"user"`` and the non-Windows platforms that ignore scope) → :func:`data_dir`.
    """
    return machine_data_dir() if scope == "machine" else data_dir()


def oidc_tokens_path() -> Path:
    """Path to the cached OIDC tokens file."""
    override = os.environ.get("GATEWAY_CLI_OIDC_CACHE")
    if override:
        return Path(override)
    return data_dir() / "oidc-tokens.json"


def vk_cache_path() -> Path:
    """Path to the cached Virtual Key file."""
    override = os.environ.get("GATEWAY_CLI_VK_CACHE")
    if override:
        return Path(override)
    return data_dir() / "vk-cache.json"


# --- Companion-binary resolution --------------------------------------------
# The Cowork `setup --credential-kind helper-script` path resolves the
# api-key-helper binary via _resolve_api_key_helper.


def _is_wsl() -> bool:
    """Return True when running inside Windows Subsystem for Linux."""
    try:
        return b"microsoft" in Path("/proc/version").read_bytes().lower()
    except OSError:
        return False


def _search_binary(name: str) -> str | None:
    """Locate a companion binary named ``name``; return its bare path or None.

    Resolution order:
      1. Next to the running executable (PyInstaller bundle / same-dir install).
      2. sysconfig scripts dir — canonical pip install location.
      3. Anywhere on PATH via shutil.which.

    The returned path is NEVER quoted. Cowork is Claude *Desktop*: the resolved
    path lands in the managed-config ``inferenceCredentialHelper`` value, which
    Desktop validates as an absolute local filesystem path — a leading ``"``
    makes it non-absolute and the app rejects it ("Path must be absolute and on
    the local filesystem"). This is the opposite of Claude Code, whose
    ``apiKeyHelper`` is a shell string run via ``cmd.exe /c`` and so DOES need a
    space-containing path double-quoted. That quoting stays in the Claude Code
    installer (the fixed incumbent); it must not leak into this Cowork tree.
    """
    # 1. Same directory as the running executable.
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).parent
    else:
        exe_dir = Path(sys.argv[0]).resolve().parent if sys.argv else Path(__file__).parent
    candidate = exe_dir / name
    if candidate.is_file():
        return str(candidate)

    # 2. pip scripts directory for the active Python installation.
    scripts_dir = sysconfig.get_path("scripts")
    if scripts_dir:
        candidate = Path(scripts_dir) / name
        if candidate.is_file():
            return str(candidate)

    # 3. PATH lookup.
    found = shutil.which(name)
    if found:
        return found

    return None


def _resolve_binary(name_posix: str, name_win: str) -> str:
    """Return the absolute path (or bare name fallback) for a companion binary.

    The path is returned unquoted on every platform — see :func:`_search_binary`
    for why Cowork/Desktop must never receive a quoted path.

    Windows: resolve the ``.exe``.

    Linux / macOS: resolve the native POSIX binary.

    WSL: a WSL environment can host EITHER a native-Linux Claude Desktop (which
    reads Linux-side config and invokes the helper natively) OR the Windows
    Claude Desktop binary (which invokes the ``.exe``). Prefer the native Linux
    helper whenever it is actually installed inside WSL: handing a Windows
    ``.exe`` to a native-Linux app yields an empty API key — the ``.exe`` cannot
    reach the Linux token store over WSL interop — and every gateway call then
    fails with HTTP 401. Only when no native helper is present do we fall back
    to the ``.exe`` (the Windows-Desktop-from-WSL case).
    """
    is_win = sys.platform == "win32"
    is_wsl = (not is_win) and _is_wsl()

    if is_win:
        return _search_binary(name_win) or name_win

    if is_wsl:
        return (
            _search_binary(name_posix)
            or _search_binary(name_win)
            or name_posix
        )

    return _search_binary(name_posix) or name_posix


def _resolve_api_key_helper() -> str:
    return _resolve_binary("api-key-helper", "api-key-helper.exe")
