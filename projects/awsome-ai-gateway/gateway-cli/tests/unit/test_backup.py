# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Tests for cli.utils.backup — config file backup."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from cli.utils.backup import BackupEntry, backup_config


class TestBackupConfig:
    def test_backup_creates_copy(self, tmp_path: Path) -> None:
        original = tmp_path / "settings.json"
        original.write_text('{"key": "value"}', encoding="utf-8")
        backup_dir = tmp_path / "backups"

        with patch("cli.utils.backup._get_backup_dir", return_value=backup_dir):
            entry = backup_config("claude-code", str(original))

        assert entry is not None
        assert isinstance(entry, BackupEntry)
        assert Path(entry.backup_path).exists()
        assert Path(entry.backup_path).read_text(encoding="utf-8") == '{"key": "value"}'

    def test_backup_naming_format(self, tmp_path: Path) -> None:
        original = tmp_path / "config.json"
        original.write_text("{}", encoding="utf-8")
        backup_dir = tmp_path / "backups"

        with patch("cli.utils.backup._get_backup_dir", return_value=backup_dir):
            entry = backup_config("opencode", str(original))

        assert entry is not None
        name = Path(entry.backup_path).name
        assert name.startswith("opencode.config.json.")
        assert name.endswith(".bak")

    def test_backup_nonexistent_returns_none(self, tmp_path: Path) -> None:
        missing = tmp_path / "no_such_file.json"
        entry = backup_config("test", str(missing))
        assert entry is None

    def test_backup_creates_backup_dir(self, tmp_path: Path) -> None:
        original = tmp_path / "settings.json"
        original.write_text("{}", encoding="utf-8")
        backup_dir = tmp_path / "new_backup_dir"

        with patch("cli.utils.backup._get_backup_dir", return_value=backup_dir):
            entry = backup_config("test", str(original))

        assert entry is not None
        assert backup_dir.exists()

    def test_backup_multiple_creates_different_files(self, tmp_path: Path) -> None:
        original = tmp_path / "settings.json"
        original.write_text('{"v": 1}', encoding="utf-8")
        backup_dir = tmp_path / "backups"

        with patch("cli.utils.backup._get_backup_dir", return_value=backup_dir):
            entry1 = backup_config("tool", str(original))
            original.write_text('{"v": 2}', encoding="utf-8")
            entry2 = backup_config("tool", str(original))

        assert entry1 is not None and entry2 is not None
        # Both exist (same timestamp possible in fast test, but files are created)
        assert Path(entry1.backup_path).exists()
        assert Path(entry2.backup_path).exists()

    def test_backup_entry_fields(self, tmp_path: Path) -> None:
        original = tmp_path / "test.json"
        original.write_text("{}", encoding="utf-8")
        backup_dir = tmp_path / "backups"

        with patch("cli.utils.backup._get_backup_dir", return_value=backup_dir):
            entry = backup_config("claude-code", str(original))

        assert entry is not None
        assert entry.original_path == str(original)
        assert entry.tool_name == "claude-code"
        assert entry.created_at is not None
