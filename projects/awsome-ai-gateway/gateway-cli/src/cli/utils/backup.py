# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Config file backup utilities (BR-BACKUP-01~04)."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from platformdirs import user_config_dir


@dataclass
class BackupEntry:
    """Record of a config file backup."""

    original_path: str
    backup_path: str
    created_at: datetime
    tool_name: str


def _get_backup_dir() -> Path:
    """Return backup directory, creating it with mode 700 if needed (BR-BACKUP-01)."""
    backup_dir = Path(user_config_dir("gateway-cli")) / "backups"
    if not backup_dir.exists():
        backup_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(str(backup_dir), 0o700)
        except OSError:
            pass  # Windows may not fully support chmod
    return backup_dir


def backup_config(tool_name: str, original_path: str | Path) -> BackupEntry | None:
    """Back up a config file before modification (BR-BACKUP-03).

    Returns None if original file doesn't exist (skip backup).
    Naming: {tool_name}.{original_filename}.{YYYYMMDDTHHMMSS}.bak (BR-BACKUP-02)
    """
    original_path = Path(original_path)
    if not original_path.exists():
        return None

    backup_dir = _get_backup_dir()
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y%m%dT%H%M%S")
    backup_name = f"{tool_name}.{original_path.name}.{timestamp}.bak"
    backup_path = backup_dir / backup_name

    # Copy with permission preservation (BR-BACKUP-04)
    shutil.copy2(str(original_path), str(backup_path))

    return BackupEntry(
        original_path=str(original_path),
        backup_path=str(backup_path),
        created_at=now,
        tool_name=tool_name,
    )
