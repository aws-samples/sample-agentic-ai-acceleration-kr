# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Binary removal for Cowork (Claude Desktop) — delegate to the Inno uninstaller.

The installed suite (``gateway-cli-cowork.exe`` + ``api-key-helper.exe`` + the
shared PyInstaller ``_internal\\`` runtime) lives in one install directory. A
running exe holds an exclusive lock on its own image and every DLL loaded from
``_internal\\``, so the CLI can NEVER delete that directory from within its own
process. The Inno ``unins000.exe`` escapes this by self-copying to ``%TEMP%`` and
relaunching from there; it then reverts the PATH entry and the Add/Remove-Programs
(ARP) registration and deletes the whole install dir.

So this module does exactly ONE thing: resolve the uninstaller and hand off to it,
then return. Software-level state (managed config, tokens, VK, CA, backups) is
NOT this module's job — that is ``clear``. Because ``uninstall`` deletes the exe
that ``clear`` runs from, run ``clear`` FIRST (the ``uninstall --clear-first``
flag does this).

Scope: Windows only. On macOS/Linux the Cowork CLI is delivered differently (no
Inno uninstaller); those platforms report that binary removal is out of scope.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import structlog

log = structlog.get_logger(component="cowork-uninstall")

# The Inno AppId (installer.iss) — the ARP/uninstall key is "<AppId>_is1" under
# the Uninstall hive, in HKLM (admin install) or HKCU (per-user install), in the
# native and WOW6432 views. We resolve the UninstallString rather than hardcoding
# a path so a per-user install under %LOCALAPPDATA% is found too.
#
# We match on the bare GUID as a SUBSTRING of the subkey name (+ the "_is1"
# suffix) instead of reconstructing the full "{GUID}_is1" string: the shipped
# installer.iss uses `AppId={{GUID}}` whose trailing `}}` is emitted verbatim by
# Inno, so the REAL key on disk is `{GUID}}_is1` (double close brace). Substring
# matching is robust to that brace quirk (and to a future .iss fix that fixes it).
_APP_GUID = "806B6437-E3FA-47E8-8AD2-D0B85D8FC60D"
_UNINSTALL_ROOTS = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
)

# Identity anchors used to VALIDATE a resolved uninstaller before we elevate it
# (see _validate_uninstaller). The ARP hive is user-writable in the per-user
# (HKCU) case, so a GUID match alone is NOT enough to trust the UninstallString:
# an unelevated process could plant an entry pointing at an attacker exe and let
# our RunAs launch elevate it. We therefore also require the resolved path to be a
# real Inno uninstaller (`unins###.exe`) living inside our own, uniquely-named
# install directory — which also distinguishes Cowork from the Claude Code CLI
# (a DIFFERENT install dir).
#   - _EXPECTED_DIR_NAME: the leaf of DefaultDirName in installer.iss.
#   - _EXPECTED_DISPLAY_NAME: AppName in installer.iss (belt-and-suspenders).
#   - _UNINS_RE: Inno names its uninstaller unins000.exe, unins001.exe, ...
_EXPECTED_DIR_NAME = "gateway-cli-cowork"  # compared case-insensitively; leaf of C:\Gateway-CLI-Cowork
_EXPECTED_DISPLAY_NAME = "LLM Gateway CLI (Cowork)"
_UNINS_RE = re.compile(r"^unins\d{3}\.exe$", re.IGNORECASE)


@dataclass
class UninstallOutcome:
    """Result of an attempted binary uninstall.

    delegated  — the uninstaller was launched by this call (it runs detached and
                 removes the binaries after we exit).
    skipped    — nothing was launched (not found / SYSTEM / unsupported platform);
                 ``detail`` says why and ``hint`` carries the manual fallback.
    detail     — a short human-readable status line.
    uninstall_string — the resolved UninstallString, if any (for --dry-run).
    """

    delegated: bool
    skipped: bool
    detail: str
    hint: str | None = None
    uninstall_string: str | None = None
    warnings: list[str] = field(default_factory=list)


_MANUAL_HINT_WIN = (
    "Remove the binaries manually:\n"
    "    1. Settings -> Apps -> Installed apps -> 'LLM Gateway CLI (Cowork)' -> Uninstall\n"
    "       (or run C:\\Gateway-CLI-Cowork\\unins000.exe)\n"
    "    2. This reverts the PATH entry and removes the install directory."
)
_MANUAL_HINT_POSIX = (
    "Binary removal via this command is Windows-only. On macOS/Linux remove the\n"
    "gateway-cli-cowork package the same way you installed it."
)


def manual_hint() -> str:
    """The platform-appropriate manual uninstall instruction."""
    return _MANUAL_HINT_POSIX if sys.platform != "win32" else _MANUAL_HINT_WIN


def _run_powershell(script: str, timeout: int = 20) -> str:
    """Run a PowerShell snippet and return stdout (raises on non-zero exit).

    Pins both ends to UTF-8: on a non-UTF-8 Windows (e.g. Korean, CP949 console)
    PowerShell's localized error text would be CP949 bytes that fail to decode as
    UTF-8 (main.py sets PYTHONUTF8=1), masking the real error. The prologue forces
    the output stream to UTF-8 and encoding=/errors= make the decode match + never
    crash.
    """
    utf8_script = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + script
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", utf8_script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"powershell exited {proc.returncode}")
    return proc.stdout


def _shell_execute_runas(file: str, parameters: str) -> None:
    """Launch ``file`` elevated (UAC) + detached via ShellExecuteW "runas".

    ``file`` and ``parameters`` are passed as SEPARATE arguments to the Win32
    API — there is no intermediate shell to reparse them, so an embedded quote or
    metacharacter in ``file`` cannot break out into a command (unlike building a
    PowerShell string). Raises OSError on failure (ShellExecuteW returns an
    HINSTANCE <= 32, e.g. the user declining the UAC prompt).
    """
    import ctypes  # noqa: PLC0415 — platform-only import

    # SW_SHOWNORMAL = 1. ShellExecuteW returns a value > 32 on success.
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", file, parameters, None, 1)
    if int(rc) <= 32:
        raise OSError(f"ShellExecuteW('runas') failed (code {int(rc)})")


def _running_as_system_windows() -> bool:
    """True when this Windows process runs as SYSTEM (S-1-5-18), e.g. via SSM.

    Elevating the uninstaller from a SYSTEM/session-0 context cannot drive the
    interactive UAC/relaunch a real uninstall needs, and would target the wrong
    per-user hive — so ``uninstall`` refuses there, mirroring the relaunch guard.
    """
    if sys.platform != "win32":
        return False
    try:
        out = _run_powershell(
            "[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value"
        )
        return out.strip() == "S-1-5-18"
    except Exception as exc:  # noqa: BLE001 — detection must never raise
        log.debug("system_check_failed", error=str(exc))
        return False


@dataclass
class _ArpEntry:
    """The subset of an ARP subkey we read + the hive it came from."""

    uninstall_string: str
    display_name: str | None
    install_location: str | None
    hive_is_hklm: bool  # HKLM is machine-scoped (admin-only writable); HKCU is not


def _read_arp_entry(hive, uninstall_root: str, hive_is_hklm: bool) -> _ArpEntry | None:
    """Scan one Uninstall hive for our product's ARP entry.

    Enumerates the child keys under ``uninstall_root`` and returns the first
    whose name contains our GUID and ends with the Inno ``_is1`` suffix, along
    with its ``DisplayName`` and ``InstallLocation``. Matching on the GUID
    substring (not a hardcoded ``{GUID}_is1``) tolerates the ``}}_is1`` brace
    quirk from installer.iss. Returns None (does NOT trust the entry) when there
    is no ``UninstallString`` — validation happens later in _validate_uninstaller.
    """
    import winreg  # noqa: PLC0415 — platform-only import

    try:
        root = winreg.OpenKey(hive, uninstall_root, 0, winreg.KEY_READ)
    except FileNotFoundError:
        return None
    try:
        index = 0
        while True:
            try:
                name = winreg.EnumKey(root, index)
            except OSError:
                break  # no more subkeys
            index += 1
            if _APP_GUID not in name.upper() or not name.endswith("_is1"):
                continue
            try:
                sub = winreg.OpenKey(root, name, 0, winreg.KEY_READ)
            except FileNotFoundError:
                continue
            try:
                uninstall_string = _query_str(sub, "UninstallString")
                display_name = _query_str(sub, "DisplayName")
                install_location = _query_str(sub, "InstallLocation")
            finally:
                winreg.CloseKey(sub)
            if uninstall_string:
                # Inno stores the bare (quoted) exe path in UninstallString.
                return _ArpEntry(
                    uninstall_string.strip().strip('"'),
                    display_name,
                    install_location,
                    hive_is_hklm,
                )
    finally:
        winreg.CloseKey(root)
    return None


def _query_str(sub, value_name: str) -> str | None:
    """QueryValueEx wrapper returning None for a missing value (never raises)."""
    import winreg  # noqa: PLC0415 — platform-only import

    try:
        value, _ = winreg.QueryValueEx(sub, value_name)
    except FileNotFoundError:
        return None
    return value if isinstance(value, str) else None


def _validate_uninstaller(entry: _ArpEntry) -> tuple[str | None, str | None]:
    """Vet an ARP entry before we ever elevate its uninstaller.

    Returns ``(validated_path, None)`` when the entry is trustworthy, else
    ``(None, reason)``. We refuse to elevate anything that is not, verifiably,
    OUR Inno uninstaller. Checks, all of which must pass:

      1. The path resolves to an existing file named ``unins###.exe`` (Inno's
         uninstaller naming) — not an arbitrary command line.
      2. That file lives inside a directory whose leaf is our uniquely-named
         install dir (``Gateway-CLI-Cowork``). This both proves ownership and
         rejects the Claude Code CLI's uninstaller (different install dir).
      3. If the entry carries a DisplayName, it matches our product name.

    The install dir is the trust anchor: because the ARP hive is user-writable
    for a per-user install, we cannot trust the raw UninstallString — but an
    unelevated attacker cannot place a file inside an admin-owned Program Files
    subdir, and a per-user install still lands in a dir with our unique name.
    """
    raw = entry.uninstall_string
    # A trustworthy Inno UninstallString is a single bare exe path. Anything with
    # shell metacharacters or arguments is not something we will hand to RunAs.
    if any(ch in raw for ch in ('"', "'", "&", "|", ";", "\n", "\r")):
        return None, f"uninstall command has unexpected shell characters: {raw!r}"

    try:
        path = Path(raw).resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        return None, f"uninstall path does not resolve to an existing file: {exc}"

    if not path.is_file():
        return None, f"uninstall path is not a file: {path}"
    if not _UNINS_RE.match(path.name):
        return None, f"uninstall target is not an Inno uninstaller (unins###.exe): {path.name}"
    if path.parent.name.lower() != _EXPECTED_DIR_NAME:
        return None, (
            f"uninstaller is not in the Cowork install directory "
            f"('{_EXPECTED_DIR_NAME}'): {path.parent}"
        )
    if entry.display_name and entry.display_name.strip() != _EXPECTED_DISPLAY_NAME:
        return None, (
            f"ARP DisplayName {entry.display_name!r} does not match "
            f"{_EXPECTED_DISPLAY_NAME!r}"
        )
    return str(path), None


def resolve_uninstall_string() -> str | None:
    """Return a VALIDATED path to our Inno ``unins###.exe``, or None.

    Searches, in order, the native and WOW6432 Uninstall hives under HKLM (admin
    install) then HKCU (per-user install), matching our product by GUID substring.
    Every candidate is run through :func:`_validate_uninstaller`; the first that
    passes is returned. Returns None when the product is not registered or when
    no candidate validates (a planted/forged entry, or a broken install).

    HKLM is searched before HKCU on purpose: a machine-scoped entry is only
    writable by admins, so it is the more trustworthy of the two.
    """
    if sys.platform != "win32":
        return None
    import winreg  # noqa: PLC0415 — platform-only import

    for hive, is_hklm in (
        (winreg.HKEY_LOCAL_MACHINE, True),
        (winreg.HKEY_CURRENT_USER, False),
    ):
        for uninstall_root in _UNINSTALL_ROOTS:
            entry = _read_arp_entry(hive, uninstall_root, is_hklm)
            if not entry:
                continue
            validated, reason = _validate_uninstaller(entry)
            if validated:
                return validated
            log.debug(
                "uninstaller_rejected",
                reason=reason,
                hklm=is_hklm,
                root=uninstall_root,
            )
    return None


def uninstall(*, dry_run: bool = False) -> UninstallOutcome:
    """Delegate binary removal to the Inno uninstaller (never self-deletes).

    Resolves ``unins000.exe`` from the ARP ``UninstallString`` and launches it
    ``/VERYSILENT /NORESTART`` via ``Start-Process -Verb RunAs`` (interactive UAC
    elevation). The uninstaller runs detached from ``%TEMP%`` and removes both
    exes, the shared ``_internal\\`` runtime, the PATH entry, and the ARP key
    after this process exits.

    ``dry_run`` resolves and reports the ``UninstallString`` without launching
    anything. No-ops with a manual hint when running as SYSTEM or off Windows.
    """
    if sys.platform != "win32":
        return UninstallOutcome(
            False, True, "binary uninstall is Windows-only on this build",
            hint=manual_hint(),
        )

    uninstall_string = resolve_uninstall_string()
    if not uninstall_string:
        return UninstallOutcome(
            False, True,
            "the Cowork CLI is not registered in Add/Remove Programs — nothing to "
            "uninstall (already removed, or installed without the Inno installer).",
            hint=manual_hint(),
        )

    if dry_run:
        return UninstallOutcome(
            False, True,
            f"dry-run — would launch: {uninstall_string} /VERYSILENT /NORESTART "
            "(elevated). No binaries removed.",
            uninstall_string=uninstall_string,
        )

    if _running_as_system_windows():
        return UninstallOutcome(
            False, True,
            "running as SYSTEM (SSM?) — cannot drive the interactive elevated "
            "uninstaller from session 0; run interactively as the installing user, "
            "or invoke unins000.exe /VERYSILENT from your deployment tool.",
            hint=manual_hint(),
            uninstall_string=uninstall_string,
        )

    # Launch the uninstaller elevated + detached, then return so this process (and
    # the exe the uninstaller must delete) exits promptly. ShellExecuteW with the
    # "runas" verb raises the UAC prompt; the uninstaller self-copies to %TEMP% so
    # it can delete our dir.
    #
    # We use the structured Win32 API rather than building a PowerShell command
    # line: the (already validated) path is passed as its own argument and the
    # parameters as a separate argument, so there is NO shell that could reparse
    # embedded quotes/metacharacters — closing the string-interpolation injection
    # vector by construction, not by escaping.
    try:
        _shell_execute_runas(uninstall_string, "/VERYSILENT /NORESTART")
    except Exception as exc:  # noqa: BLE001
        log.debug("uninstall_launch_failed", error=str(exc))
        return UninstallOutcome(
            False, True,
            f"failed to launch the uninstaller: {exc}",
            hint=manual_hint(),
            uninstall_string=uninstall_string,
        )
    return UninstallOutcome(
        True, False,
        "uninstaller launched — it will remove the binaries, PATH entry, and "
        "Add/Remove-Programs entry after this process exits.",
        uninstall_string=uninstall_string,
    )
