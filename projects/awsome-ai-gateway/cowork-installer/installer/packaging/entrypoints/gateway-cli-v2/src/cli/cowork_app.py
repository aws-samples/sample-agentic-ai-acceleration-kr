# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Claude Desktop (Cowork) app lifecycle: force-quit + relaunch.

Cowork is Claude *Desktop*, a GUI app. It reads its managed configuration
(model list, inference base URL, credential kind) **only at launch**, so any
*config* change requires a full quit + relaunch to take effect. *Credential*
rotation does NOT need a relaunch — the running app re-invokes the helper on its
own TTL schedule. This module implements the quit+relaunch half.

Platform specifics
------------------
- **Windows**: Cowork ships as an **MSIX** package. An MSIX app must be launched
  by its AUMID via the shell ``AppsFolder`` — never by running the versioned
  ``WindowsApps`` exe directly (ACL-restricted, and the path changes every
  update). The AUMID is resolved at runtime from the installed package's
  ``PackageFamilyName`` + manifest ``Application Id``.
- **macOS**: quit via ``osascript -e 'quit app "Claude"'``, relaunch via
  ``open -a Claude``.

Safety guards
------------
- **Windows / SYSTEM**: a per-user GUI app cannot be driven from session 0
  (e.g. SSM-invoked SYSTEM) — it never reaches the interactive desktop and reads
  the wrong registry hive. Relaunch refuses under SYSTEM and returns a hint.
- **POSIX / root**: likewise refuses under ``sudo``/root.
- Opt out entirely with ``COWORK_NO_RELAUNCH=1`` (returns the manual hint).

Best-effort throughout: operational failures (app not found, quit failed) never
raise — callers get a structured :class:`RelaunchOutcome` and, when relaunch is
unsafe or disabled, a manual instruction.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass

import structlog

log = structlog.get_logger(component="cowork-app")

# Manual steps shown whenever auto-relaunch is skipped (SYSTEM/root/opt-out) or fails.
_MANUAL_HINT_WIN = (
    "Claude Desktop reads its config ONLY at launch. Relaunch it manually:\n"
    "    1. Quit fully (system tray -> Quit, or: Stop-Process -Name Claude -Force)\n"
    "    2. Relaunch Claude Desktop from the Start menu\n"
    "    3. Open the Cowork tab, pick the model, and start a session"
)
_MANUAL_HINT_POSIX = (
    "Claude Desktop reads its config ONLY at launch. Relaunch it manually:\n"
    "    1. Quit fully:  Cmd+Q  (or: osascript -e 'quit app \"Claude\"')\n"
    "    2. Relaunch:    open -a Claude\n"
    "    3. Open the Cowork tab, pick the model, and start a session"
)


def manual_hint() -> str:
    """The platform-appropriate manual quit+relaunch instruction."""
    return _MANUAL_HINT_POSIX if sys.platform == "darwin" else _MANUAL_HINT_WIN


@dataclass
class RelaunchOutcome:
    """Result of an attempted relaunch.

    relaunched — the app was (re)launched by this call.
    skipped    — auto-relaunch was intentionally not performed (SYSTEM/root/opt-out
                 or app-not-found); ``hint`` carries the manual steps.
    detail     — a short human-readable status line.
    """

    relaunched: bool
    skipped: bool
    detail: str
    hint: str | None = None


# --- Privilege guards --------------------------------------------------------

def _running_as_system_windows() -> bool:
    """True when this Windows process runs as SYSTEM (S-1-5-18), e.g. via SSM.

    Best-effort: any failure to determine the identity returns False so we don't
    block a legitimate interactive relaunch on a detection hiccup.
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


def _running_as_root_posix() -> bool:
    """True when this POSIX process is effectively root."""
    if sys.platform == "win32":
        return False
    try:
        return os.geteuid() == 0  # type: ignore[attr-defined]
    except AttributeError:
        return False


# --- PowerShell plumbing (Windows) -------------------------------------------

def _run_powershell(script: str, timeout: int = 20) -> str:
    """Run a PowerShell snippet and return stdout (raises on non-zero exit).

    Pins both ends to UTF-8: on a Korean (or any non-UTF-8) Windows the console
    code page is CP949, so PowerShell's localized error text would be CP949 bytes
    that fail to decode as UTF-8 (main.py sets PYTHONUTF8=1), masking the real
    error with a UnicodeDecodeError/mojibake. The prologue forces the output
    stream to UTF-8 and encoding=/errors= make the Python decode match + never crash.
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


def resolve_msix_aumid() -> str | None:
    """Resolve the Cowork MSIX AUMID (``<PackageFamilyName>!<AppId>``), or None.

    Find the installed Claude MSIX package, read its manifest Application Id, and
    join it to the PackageFamilyName. Returns None when no MSIX Claude package is
    present (legacy per-user exe install, or the app isn't installed).
    """
    if sys.platform != "win32":
        return None
    script = (
        "$p = Get-AppxPackage *Claude* | Select-Object -First 1; "
        "if (-not $p) { $p = Get-AppxPackage -AllUsers *Claude* | Select-Object -First 1 }; "
        "if (-not $p) { return }; "
        "$appId = 'Claude'; "
        "try { [xml]$m = Get-Content (Join-Path $p.InstallLocation 'AppxManifest.xml'); "
        "$first = @($m.Package.Applications.Application)[0]; "
        "if ($first.Id) { $appId = $first.Id } } catch { }; "
        "Write-Output ($p.PackageFamilyName + '!' + $appId)"
    )
    try:
        out = _run_powershell(script).strip()
        return out or None
    except Exception as exc:  # noqa: BLE001
        log.debug("aumid_resolve_failed", error=str(exc))
        return None


# --- Quit + launch primitives ------------------------------------------------

def _quit_and_launch_windows(aumid: str) -> None:
    """Force-quit any running Claude, then relaunch the MSIX app by AUMID."""
    # Graceful close first, then hard-kill survivors, then launch via AppsFolder.
    script = (
        "$procs = Get-Process -Name 'Claude','claude' -ErrorAction SilentlyContinue; "
        "if ($procs) { $procs | ForEach-Object { $_.CloseMainWindow() | Out-Null }; "
        "Start-Sleep -Seconds 2; "
        "Get-Process -Name 'Claude','claude' -ErrorAction SilentlyContinue | "
        "Stop-Process -Force -ErrorAction SilentlyContinue; Start-Sleep -Seconds 1 }; "
        f"Start-Process 'shell:AppsFolder\\{aumid}'"
    )
    _run_powershell(script)


def _quit_and_launch_macos() -> None:
    """Force-quit Claude Desktop, then relaunch it via ``open -a Claude``."""
    # Graceful quit; ignore failures (app may not be running).
    subprocess.run(
        ["osascript", "-e", 'quit app "Claude"'],
        capture_output=True, text=True, timeout=15, check=False,
    )
    # Give it a moment, then hard-kill any survivor before relaunching.
    subprocess.run(["pkill", "-x", "Claude"], capture_output=True, check=False)
    subprocess.run(["open", "-a", "Claude"], capture_output=True, text=True,
                   timeout=15, check=False)


# --- Public entry point ------------------------------------------------------

def relaunch_app(*, no_relaunch: bool = False) -> RelaunchOutcome:
    """Force-quit and relaunch Claude Desktop as the current interactive user.

    This is the orchestration a *config* change needs to take effect; credential
    rotation does not need it. No-ops with a manual hint when running as
    SYSTEM/root or when opted out (``no_relaunch`` / ``COWORK_NO_RELAUNCH=1``).
    """
    hint = manual_hint()
    if no_relaunch or os.environ.get("COWORK_NO_RELAUNCH") == "1":
        return RelaunchOutcome(False, True, "auto-relaunch disabled (opt-out)", hint)

    if _running_as_system_windows():
        return RelaunchOutcome(
            False, True,
            "running as SYSTEM (SSM?) — cannot drive a per-user GUI app from "
            "session 0; run interactively as the target user.",
            hint,
        )
    if _running_as_root_posix():
        return RelaunchOutcome(
            False, True,
            "running as root — cannot drive a per-user GUI app; run as the user.",
            hint,
        )

    try:
        if sys.platform == "win32":
            aumid = resolve_msix_aumid()
            if not aumid:
                return RelaunchOutcome(
                    False, True,
                    "Claude Desktop MSIX package not found — is the app installed?",
                    hint,
                )
            _quit_and_launch_windows(aumid)
            return RelaunchOutcome(True, False, f"relaunched MSIX app ({aumid})")
        if sys.platform == "darwin":
            if not os.path.isdir("/Applications/Claude.app"):
                return RelaunchOutcome(
                    False, True,
                    "Claude.app not found in /Applications — is the app installed?",
                    hint,
                )
            _quit_and_launch_macos()
            return RelaunchOutcome(True, False, "relaunched Claude Desktop")
        # Linux: Cowork is macOS + Windows only.
        return RelaunchOutcome(
            False, True,
            "Cowork Desktop is only available on macOS and Windows.",
            hint,
        )
    except Exception as exc:  # noqa: BLE001 — operational failure -> hint, not crash
        log.debug("relaunch_failed", error=str(exc))
        return RelaunchOutcome(False, True, f"relaunch failed: {exc}", hint)
