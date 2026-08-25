# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Timestamped, non-overwriting backups of config files.

Both files ``setup`` writes (the user ``settings.json`` and the system
``managed-settings.json``) may already hold values the user or their org set up.
Before we merge our keys in, we snapshot the current file so nothing is ever
silently lost.

Unlike a single ``<file>.bak`` sidecar (which a second ``setup`` run would
overwrite), each backup carries a UTC timestamp and lands in a dedicated backups
directory, so **every** pre-write state is retained:

    <backup-dir>/{tool}.{filename}.{YYYYMMDDTHHMMSS}.bak
    e.g. claude-code.settings.json.20260709T142530.bak

The backup directory is the platform user-data dir's ``backups/`` subfolder
(overridable via ``GATEWAY_CLI_BACKUP_DIR`` for tests), created ``0700`` so the
snapshots — which can contain tokens/endpoints — are not world-readable.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cli.paths import data_dir


@dataclass
class BackupEntry:
    """Record of a single config-file backup."""

    original_path: str
    backup_path: str
    created_at: datetime
    tool_name: str


def _backup_dir() -> Path:
    """Return the backups directory, creating it 0700 if needed.

    Defaults to ``<user-data-dir>/backups``; ``GATEWAY_CLI_BACKUP_DIR`` overrides
    it (used by tests and by anyone who wants snapshots kept elsewhere).
    """
    override = os.environ.get("GATEWAY_CLI_BACKUP_DIR")
    backup_dir = Path(override) if override else data_dir() / "backups"
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


def backup_values(tool_name: str, name: str, values: Mapping[str, str]) -> BackupEntry | None:
    """Snapshot an in-memory ``{name: value}`` map to disk before it is replaced.

    Some state has no file to :func:`backup_config` — notably Windows HKCU
    registry values, which ``env --persist`` overwrites in place. This serialises
    the prior values to a timestamped JSON file in the SAME backups dir, so every
    pre-write state stays recoverable under one policy. ``name`` labels the source
    (e.g. ``"Environment"`` for the HKCU env key).

    Returns None when ``values`` is empty — a first-time write clobbers nothing,
    so there is nothing to preserve. Never overwrites an existing snapshot: a
    same-second collision gets a numeric suffix.
    """
    if not values:
        return None

    backup_dir = _backup_dir()
    base = f"{tool_name}.{name}.{_timestamp()}"
    backup_path = backup_dir / f"{base}.json.bak"
    counter = 1
    while backup_path.exists():
        backup_path = backup_dir / f"{base}.{counter}.json.bak"
        counter += 1

    backup_path.write_text(
        json.dumps(dict(values), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    return BackupEntry(
        original_path=f"{name} (in-memory snapshot)",
        backup_path=str(backup_path),
        created_at=datetime.now(timezone.utc),
        tool_name=tool_name,
    )
