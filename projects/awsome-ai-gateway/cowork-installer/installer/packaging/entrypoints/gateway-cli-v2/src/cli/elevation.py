# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Windows UAC self-elevation for ``setup``.

``setup`` writes the OS-native managed config, which on Windows requires an
elevated token: ``HKLM\\SOFTWARE\\Policies`` is machine-wide, and even
``HKCU\\SOFTWARE\\Policies`` is Group-Policy/admin-controlled. Windows cannot
elevate a running process in place, so when the caller is a **local admin
running non-elevated** we relaunch THIS exe with the same arguments through a
UAC "runas" prompt, wait for the elevated child, and propagate its exit code.

Why only a local admin (a UAC *consent* prompt), never a genuine standard user:

* For an admin, UAC shows a Yes/No **consent** prompt and the elevated process
  keeps the SAME user token — so the per-user ``HKCU`` hive still resolves to the
  caller's own profile. Both ``user`` and ``machine`` scope are correct.
* For a standard user, UAC shows a **credential** prompt asking for a DIFFERENT
  admin login; the elevated process then runs as THAT admin, and a ``user``-scope
  write would land in the wrong profile's ``HKCU\\...\\Policies\\Claude`` (the
  exact hive-aliasing hazard the installer/``managed`` notes warn about). A true
  standard user cannot complete setup anyway (their own ``HKCU`` policy hive is
  GP-controlled), so we let the normal flow surface its "run as administrator"
  guidance instead of firing a prompt that would silently corrupt the hive.

The whole feature is Windows-only and only engages for the packaged (frozen) exe;
a source/dev checkout re-runs from an elevated prompt by hand.
"""

from __future__ import annotations

import subprocess
import sys

import structlog

log = structlog.get_logger(__name__)

# TOKEN_ELEVATION_TYPE (winnt.h). LIMITED means an admin whose token has been
# UAC-filtered — i.e. a non-elevated admin who can elevate via a consent prompt
# WITHOUT changing identity. DEFAULT is a standard user (or an admin with UAC
# disabled, who is already FULL and thus caught by is_elevated first).
_TOKEN_ELEVATION_TYPE_DEFAULT = 1
_TOKEN_ELEVATION_TYPE_FULL = 2
_TOKEN_ELEVATION_TYPE_LIMITED = 3


class ElevationError(Exception):
    """Auto-elevation could not be performed."""


class ElevationCancelled(ElevationError):
    """The user declined the UAC prompt (ERROR_CANCELLED)."""


def is_windows() -> bool:
    return sys.platform == "win32"


def is_elevated() -> bool:
    """True when this process already holds an elevated (admin) token."""
    if not is_windows():
        return False
    try:
        import ctypes  # noqa: PLC0415 — platform-only import

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception as exc:  # noqa: BLE001 — detection must never raise
        log.debug("is_elevated_failed", error=str(exc))
        return False


def _elevation_type() -> int | None:
    """The process token's TOKEN_ELEVATION_TYPE, or None if it cannot be read."""
    if not is_windows():
        return None
    try:
        import ctypes  # noqa: PLC0415 — platform-only import
        from ctypes import wintypes  # noqa: PLC0415 — Windows-only submodule

        TOKEN_QUERY = 0x0008
        TokenElevationType = 18  # TOKEN_INFORMATION_CLASS

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)
        ):
            return None
        try:
            elevation_type = wintypes.DWORD()
            returned = wintypes.DWORD()
            if not advapi32.GetTokenInformation(
                token,
                TokenElevationType,
                ctypes.byref(elevation_type),
                ctypes.sizeof(elevation_type),
                ctypes.byref(returned),
            ):
                return None
            return int(elevation_type.value)
        finally:
            kernel32.CloseHandle(token)
    except Exception as exc:  # noqa: BLE001 — detection must never raise
        log.debug("elevation_type_failed", error=str(exc))
        return None


def can_consent_elevate() -> bool:
    """True when the caller is a local admin running non-elevated.

    Such a token elevates via a UAC *consent* prompt that preserves the user
    identity (and therefore the ``HKCU`` hive) — the only case in which
    auto-elevating ``setup`` is safe for the per-user scope.
    """
    return _elevation_type() == _TOKEN_ELEVATION_TYPE_LIMITED


def should_auto_elevate() -> bool:
    """Whether ``setup`` should relaunch itself elevated before doing any work.

    True only for the packaged Windows exe, when NOT already elevated, and when
    the caller is a consent-elevatable local admin (see module docstring). A
    standard user, an already-elevated process (incl. SYSTEM, which is elevated),
    a source/dev run, or any non-Windows platform returns False.
    """
    if not is_windows() or is_elevated():
        return False
    if not getattr(sys, "frozen", False):
        return False
    return can_consent_elevate()


def build_relaunch_params(
    argv: list[str], executable: str, frozen: bool
) -> tuple[str, str]:
    """(file, parameters) for a faithful elevated relaunch of this invocation.

    Frozen exe: the executable is our exe and the CLI args are ``argv[1:]``.
    Source/dev run: re-run the interpreter with the whole ``argv``. Arguments are
    quoted with :func:`subprocess.list2cmdline` so paths with spaces survive.
    """
    args = argv[1:] if frozen else argv
    return executable, subprocess.list2cmdline(args)


def relaunch_elevated_and_wait(file: str, parameters: str) -> int:
    """Launch ``file parameters`` elevated via UAC, wait, return its exit code.

    Uses ``ShellExecuteExW`` with the "runas" verb and ``SEE_MASK_NOCLOSEPROCESS``
    so we get a process handle to wait on and read the exit code from. ``file`` and
    ``parameters`` are passed as separate Win32 arguments (no intermediate shell to
    re-parse them). Raises :class:`ElevationCancelled` if the user declines the
    prompt, :class:`ElevationError` on any other failure.
    """
    if not is_windows():
        raise ElevationError("UAC elevation is only available on Windows")

    import ctypes  # noqa: PLC0415 — platform-only import
    from ctypes import wintypes  # noqa: PLC0415 — Windows-only submodule

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SW_SHOWNORMAL = 1
    INFINITE = 0xFFFFFFFF
    ERROR_CANCELLED = 1223

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = (
            ("cbSize", wintypes.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIcon", wintypes.HANDLE),  # DUMMYUNIONNAME (hIcon / hMonitor)
            ("hProcess", wintypes.HANDLE),
        )

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = file
    info.lpParameters = parameters
    info.nShow = SW_SHOWNORMAL

    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        err = ctypes.get_last_error()
        if err == ERROR_CANCELLED:
            raise ElevationCancelled("the administrator elevation prompt was declined")
        raise ElevationError(f"ShellExecuteExW('runas') failed (error {err})")

    if not info.hProcess:
        raise ElevationError("the elevated process handle was not returned")

    try:
        kernel32.WaitForSingleObject(info.hProcess, INFINITE)
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code)):
            raise ElevationError("could not read the elevated process exit code")
        return int(code.value)
    finally:
        kernel32.CloseHandle(info.hProcess)
