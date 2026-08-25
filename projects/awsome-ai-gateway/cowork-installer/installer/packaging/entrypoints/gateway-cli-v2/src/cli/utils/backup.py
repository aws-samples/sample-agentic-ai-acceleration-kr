# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Timestamped, non-overwriting backups of config files — and, on Windows, an
ownership-aware snapshot/restore of the managed-policy registry key.

File backups
------------
Both files ``setup`` writes (the user ``settings.json`` and the system
``managed-settings.json``) may already hold values the user or their org set up.
Before we merge our keys in, we snapshot the current file so nothing is ever
silently lost. On macOS the Cowork config lands in a ``configLibrary`` JSON file,
so ``backup_config`` covers that too.

Unlike a single ``<file>.bak`` sidecar (which a second ``setup`` run would
overwrite), each backup carries a UTC timestamp and lands in a dedicated backups
directory, so **every** pre-write state is retained:

    <backup-dir>/{tool}.{filename}.{YYYYMMDDTHHMMSS}.bak
    e.g. claude-code.settings.json.20260709T142530.bak

The backup directory is the platform user-data dir's ``backups/`` subfolder
(overridable via ``GATEWAY_CLI_BACKUP_DIR`` for tests), created ``0700`` so the
snapshots — which can contain tokens/endpoints — are not world-readable.

Registry backups (Windows)
---------------------------
Cowork's managed config on Windows MSIX is a **registry** key
(``HKCU\\SOFTWARE\\Policies\\Claude``), not a file — a path copy can't capture it,
and the key may already hold org-managed values we must never clobber. The
``*_registry_*`` helpers here snapshot the prior state of exactly the value names
we are about to write (plus whether the key existed at all) and later revert to
that precise state: values we added are deleted, values we overwrote are put
back, and a key we created is removed only if we left it empty. This is the
ownership-aware rollback the file-only ``backup_config`` cannot provide.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cli.paths import _release_locked_env, data_dir


@dataclass
class BackupEntry:
    """Record of a single config-file backup."""

    original_path: str
    backup_path: str
    created_at: datetime
    tool_name: str


def _backup_dir(base_dir: Path | None = None) -> Path:
    """Return the backups directory, creating it 0700 if needed.

    Defaults to ``<user-data-dir>/backups``; ``GATEWAY_CLI_BACKUP_DIR`` overrides
    it (used by tests and by anyone who wants snapshots kept elsewhere). Pass
    ``base_dir`` to root the ``backups/`` subfolder somewhere other than the
    per-user data dir — machine-scope (HKLM) snapshots use the machine-wide data
    dir so a different user can still find and revert them (the override, when
    set, still wins so tests keep a single sink).

    The override is release-locked (:func:`cli.paths._release_locked_env`): a
    frozen shipped exe ignores it, so an unprivileged environment cannot redirect
    the elevated machine-scope rollback state to an attacker-controlled directory.
    """
    override = _release_locked_env("GATEWAY_CLI_BACKUP_DIR")
    if override:
        backup_dir = Path(override)
    else:
        backup_dir = (base_dir or data_dir()) / "backups"
    if not backup_dir.exists():
        backup_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(backup_dir, 0o700)
        except OSError:
            pass  # Windows may not honour chmod; ACLs govern there.
    return backup_dir


def _timestamp() -> str:
    """UTC timestamp used in the backup filename (second resolution)."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


def backup_config(tool_name: str, original_path: str | Path) -> BackupEntry | None:
    """Snapshot ``original_path`` into the backups dir before it is modified.

    Returns None (no-op) when the file does not exist yet — a first-time write
    has nothing to preserve. Never overwrites an existing backup: if two runs
    land in the same second, a numeric suffix keeps them distinct.

    ``shutil.copy2`` preserves permissions and timestamps on the copy.
    """
    original = Path(original_path)
    if not original.is_file():
        return None

    backup_dir = _backup_dir()
    base = f"{tool_name}.{original.name}.{_timestamp()}"
    backup_path = backup_dir / f"{base}.bak"
    # Same-second collisions get a counter so no snapshot is ever clobbered.
    counter = 1
    while backup_path.exists():
        backup_path = backup_dir / f"{base}.{counter}.bak"
        counter += 1

    shutil.copy2(original, backup_path)

    return BackupEntry(
        original_path=str(original),
        backup_path=str(backup_path),
        created_at=datetime.now(timezone.utc),
        tool_name=tool_name,
    )


# --- Windows registry snapshot / ownership-aware restore --------------------
# Cowork's managed config on Windows is a registry key, not a file. These helpers
# capture the prior state of only the value names we intend to write (and whether
# the key existed at all), so a later `disable`/rollback reverts to exactly that
# state without ever clobbering a pre-existing org policy value we didn't write.

# Root aliases we support. HKCU is the Cowork managed-policy hive; HKLM is here so
# a snapshot can also record (never blindly delete) an HKLM-precedence conflict.
_REG_ROOTS = {"HKCU": "HKEY_CURRENT_USER", "HKLM": "HKEY_LOCAL_MACHINE"}


@dataclass
class RegistryBackup:
    """Prior state of a registry key + the specific values we are about to write.

    root        — root alias, ``"HKCU"`` or ``"HKLM"``.
    subkey      — path under the root, e.g. ``SOFTWARE\\Policies\\Claude``.
    key_existed — whether the subkey existed before we wrote anything.
    values      — ``{name: {"present": bool, "type": int, "data": <encoded>}}``
                  for each value name in scope. ``present=False`` means the value
                  was absent before, so restore should delete it.
    backup_path — where this record was persisted (JSON), if any.
    tool_name   — owner tag, mirrors the file-backup convention.
    created_at  — UTC capture time.
    """

    root: str
    subkey: str
    key_existed: bool
    values: dict
    tool_name: str
    created_at: datetime
    backup_path: str | None = None


def _require_winreg():
    """Import winreg or raise a clear error off Windows."""
    if sys.platform != "win32":
        raise RuntimeError("registry backup/restore is only available on Windows")
    import winreg  # noqa: PLC0415 — platform-only import

    return winreg


def _encode_reg_data(data):
    """Make a QueryValueEx value JSON-serialisable (bytes -> base64 wrapper)."""
    if isinstance(data, bytes):
        return {"__b64__": base64.b64encode(data).decode("ascii")}
    return data


def _decode_reg_data(data):
    """Inverse of :func:`_encode_reg_data`."""
    if isinstance(data, dict) and "__b64__" in data:
        return base64.b64decode(data["__b64__"])
    return data


def snapshot_registry_values(
    tool_name: str,
    root: str,
    subkey: str,
    value_names,
    *,
    persist: bool = True,
    backup_dir: Path | None = None,
) -> RegistryBackup | None:
    """Capture the prior state of ``value_names`` under ``root\\subkey``.

    Records, for each name, whether it existed and (if so) its type + data, plus
    whether the key itself existed. Returns None on non-Windows. When ``persist``
    is set, the record is also written as a timestamped JSON file in the backups
    dir so a later process (e.g. the ``disable`` command) can restore it. Pass
    ``backup_dir`` to persist somewhere other than the per-user data dir — an HKLM
    snapshot roots its ``backups/`` under the machine-wide data dir so any admin
    can revert it (ignored when ``GATEWAY_CLI_BACKUP_DIR`` is set).
    """
    if sys.platform != "win32":
        return None
    winreg = _require_winreg()
    if root not in _REG_ROOTS:
        raise ValueError(f"unsupported registry root {root!r}")
    hive = getattr(winreg, _REG_ROOTS[root])

    values: dict = {}
    key_existed = True
    try:
        handle = winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ)
    except FileNotFoundError:
        key_existed = False
        handle = None

    if handle is not None:
        try:
            for name in value_names:
                try:
                    data, vtype = winreg.QueryValueEx(handle, name)
                    values[name] = {
                        "present": True,
                        "type": vtype,
                        "data": _encode_reg_data(data),
                    }
                except FileNotFoundError:
                    values[name] = {"present": False}
        finally:
            winreg.CloseKey(handle)
    else:
        for name in value_names:
            values[name] = {"present": False}

    backup = RegistryBackup(
        root=root,
        subkey=subkey,
        key_existed=key_existed,
        values=values,
        tool_name=tool_name,
        created_at=datetime.now(timezone.utc),
    )

    if persist:
        backup.backup_path = _write_registry_backup(backup, base_dir=backup_dir)
    return backup


def _write_registry_backup(backup: RegistryBackup, *, base_dir: Path | None = None) -> str:
    """Persist a registry snapshot as timestamped JSON; return its path."""
    backup_dir = _backup_dir(base_dir)
    safe_key = (backup.root + "_" + backup.subkey).replace("\\", "_").replace("/", "_")
    base = f"{backup.tool_name}.{safe_key}.{_timestamp()}"
    path = backup_dir / f"{base}.regbak.json"
    counter = 1
    while path.exists():
        path = backup_dir / f"{base}.{counter}.regbak.json"
        counter += 1
    record = {
        "root": backup.root,
        "subkey": backup.subkey,
        "key_existed": backup.key_existed,
        "values": backup.values,
        "tool_name": backup.tool_name,
        "created_at": backup.created_at.isoformat(),
    }
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return str(path)


def load_registry_backup(path: str | Path) -> RegistryBackup:
    """Load a snapshot previously written by :func:`snapshot_registry_values`."""
    p = Path(path)
    record = json.loads(p.read_text(encoding="utf-8"))
    return RegistryBackup(
        root=record["root"],
        subkey=record["subkey"],
        key_existed=record["key_existed"],
        values=record["values"],
        tool_name=record["tool_name"],
        created_at=datetime.fromisoformat(record["created_at"]),
        backup_path=str(p),
    )


def restore_registry_values(backup: RegistryBackup) -> bool:
    """Revert the snapshotted values to their exact prior state.

    For each value name in the snapshot: one we added (absent before) is deleted;
    one we overwrote is restored to its prior type + data. If the key did not
    exist before we wrote it, and it is left with no values after the revert, the
    key we created is removed. Returns True if anything was changed.

    Never touches value names outside the snapshot, so a pre-existing org policy
    value we never wrote is left exactly as it was.
    """
    if sys.platform != "win32":
        return False
    winreg = _require_winreg()
    hive = getattr(winreg, _REG_ROOTS[backup.root])

    try:
        handle = winreg.OpenKey(hive, backup.subkey, 0, winreg.KEY_ALL_ACCESS)
    except FileNotFoundError:
        # Nothing to restore — the key is already gone.
        return False

    changed = False
    try:
        for name, prior in backup.values.items():
            if prior.get("present"):
                # We overwrote an existing value — put its prior data back.
                winreg.SetValueEx(
                    handle, name, 0, prior["type"], _decode_reg_data(prior["data"])
                )
                changed = True
            else:
                # We added this value — delete it (ignore if already gone).
                try:
                    winreg.DeleteValue(handle, name)
                    changed = True
                except FileNotFoundError:
                    pass
    finally:
        winreg.CloseKey(handle)

    # If we created the key and it is now empty, remove the key we created.
    if not backup.key_existed:
        try:
            probe = winreg.OpenKey(hive, backup.subkey, 0, winreg.KEY_READ)
            try:
                subkey_count, value_count, _ = winreg.QueryInfoKey(probe)
            finally:
                winreg.CloseKey(probe)
            if value_count == 0 and subkey_count == 0:
                winreg.DeleteKey(hive, backup.subkey)
                changed = True
        except FileNotFoundError:
            pass

    return changed
