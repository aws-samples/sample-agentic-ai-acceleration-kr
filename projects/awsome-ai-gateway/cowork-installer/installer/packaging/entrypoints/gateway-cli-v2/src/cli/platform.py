# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Runtime platform helpers — WSL detection and WSL-aware utilities.

WSL (Windows Subsystem for Linux) is a Linux kernel running inside Windows.
From Python's perspective sys.platform == "linux", but several user-facing
behaviours differ from a native Linux environment:

  - The browser must be opened on the Windows side via wslview / cmd.exe.
  - The user's Downloads folder lives at a Windows path (/mnt/c/Users/…).

All WSL-specific logic is isolated here so callers stay platform-agnostic.
"""

from __future__ import annotations

import os
import subprocess
import sys
import webbrowser
from functools import lru_cache
from pathlib import Path
from shutil import which


@lru_cache(maxsize=1)
def is_wsl() -> bool:
    """Return True when running inside Windows Subsystem for Linux (WSL 1 or 2)."""
    platform: str = sys.platform
    if platform != "linux":
        return False
    try:
        version = Path("/proc/version").read_text(encoding="utf-8", errors="ignore").lower()
        return "microsoft" in version
    except OSError:
        return False


def _wslpath(win_path: str) -> Path | None:
    """Convert a Windows path to a WSL mount path using wslpath, or None on failure.

    wslpath is the canonical tool:  C:\\Users\\Alice  →  /mnt/c/Users/Alice
    Available in all WSL 2 distros.
    """
    try:
        result = subprocess.run(
            ["wslpath", "-u", win_path.strip()],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0:
            p = Path(result.stdout.strip())
            if p.is_dir():
                return p
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return None


def wsl_windows_home() -> Path | None:
    """Return the WSL mount path of the Windows user home (e.g. /mnt/c/Users/Administrator).

    Resolution order (verified against a rootfs-imported WSL 2 instance where
    Windows env vars are NOT inherited):
      1. wslpath -u $USERPROFILE  — fast path when env var is present.
      2. wslpath -u $(cmd.exe /c echo %USERPROFILE%)  — query Windows directly.
      3. Scan /mnt/<drive>/Users/ for the first non-system account dir.
    """
    if not is_wsl():
        return None

    # 1. USERPROFILE env var (set when WSL inherits the Windows environment).
    userprofile = os.environ.get("USERPROFILE", "").strip()
    if userprofile:
        p = _wslpath(userprofile)
        if p:
            return p

    # 2. Ask cmd.exe — works in interactive sessions with WSL interop enabled.
    try:
        result = subprocess.run(
            ["cmd.exe", "/c", "echo", "%USERPROFILE%"],
            capture_output=True, text=True, timeout=3,
        )
        win_path = result.stdout.strip()
        if result.returncode == 0 and win_path and "%" not in win_path:
            p = _wslpath(win_path)
            if p:
                return p
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    # 3. Scan /mnt/<drive>/Users/ for the first non-system account directory.
    _SYSTEM_NAMES = {"Public", "Default", "Default User", "All Users", "desktop.ini"}
    try:
        for drive_dir in sorted(Path("/mnt").iterdir()):
            if drive_dir.name == "wsl":   # /mnt/wsl is a WSL-internal tmpfs, not a Windows drive
                continue
            users = drive_dir / "Users"
            if not users.is_dir():
                continue
            for user_dir in sorted(users.iterdir()):
                if user_dir.name not in _SYSTEM_NAMES and user_dir.is_dir():
                    return user_dir
    except OSError:
        pass

    return None


def open_browser(url: str) -> None:
    """Open *url* in a browser, with WSL interop handled transparently.

    - WSL: tries ``wslview`` (wslu, pre-installed on Ubuntu 22.04 in the Store)
      then falls back to ``cmd.exe /c start`` so the URL opens on the Windows
      side where a real browser exists.
    - Native Linux / macOS / Windows: delegates to the stdlib ``webbrowser``
      module as before.
    """
    if is_wsl():
        # wslview is part of wslu (pre-installed on Ubuntu 22.04 WSL images).
        try:
            subprocess.run(["wslview", url], check=False, timeout=5)
            return
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        # powershell.exe Start-Process quotes the URL properly — cmd.exe /c start
        # splits on & in query strings without quoting.
        try:
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", f'Start-Process "{url}"'],
                check=False, timeout=5,
            )
            return
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    try:
        webbrowser.open(url)
    except webbrowser.Error:
        pass


def browser_available() -> bool:
    """Best-effort check for whether a browser can actually be opened.

    Used to decide whether to auto-fall-back to the headless email+password
    login. Deliberately conservative: returns True only when we have concrete
    evidence a browser opener exists, so a genuinely headless host (SSH session,
    minimal Linux, container) reliably drops to the fallback.

    - WSL: True only if wslview or powershell.exe is on PATH (the two openers
      open_browser() actually uses to reach the Windows-side browser).
    - macOS / Windows: assumed True (a system browser + `open`/webbrowser always
      exists).
    - Native Linux: True only if $DISPLAY (or $WAYLAND_DISPLAY) is set AND a
      known opener (xdg-open / a real browser binary) is present. A bare
      sensible-browser with no DISPLAY does not count.
    """
    if is_wsl():
        return which("wslview") is not None or which("powershell.exe") is not None

    if sys.platform in ("darwin", "win32"):
        return True

    # Native Linux: need both a display and a real opener.
    has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    if not has_display:
        return False
    for opener in ("xdg-open", "google-chrome", "google-chrome-stable",
                   "chromium", "chromium-browser", "firefox"):
        if which(opener):
            return True
    return False
