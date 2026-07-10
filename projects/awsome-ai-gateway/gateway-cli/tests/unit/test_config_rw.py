# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Tests for cli.utils.config_rw — atomic write and JSON read."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from cli.utils.config_rw import atomic_write_json, read_json, read_yaml


class TestReadJson:
    def test_read_existing_file(self, tmp_path: Path) -> None:
        path = tmp_path / "test.json"
        path.write_text('{"key": "value"}', encoding="utf-8")
        result = read_json(path)
        assert result == {"key": "value"}

    def test_read_nonexistent_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "missing.json"
        result = read_json(path)
        assert result == {}

    def test_read_invalid_json_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text("{invalid json", encoding="utf-8")
        with pytest.raises(ValueError, match="JSON parse error"):
            read_json(path)

    def test_read_empty_file_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.json"
        path.write_text("", encoding="utf-8")
        with pytest.raises(ValueError, match="JSON parse error"):
            read_json(path)

    def test_read_nested_json(self, tmp_path: Path) -> None:
        data = {"env": {"KEY": "val"}, "list": [1, 2]}
        path = tmp_path / "nested.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        assert read_json(path) == data


class TestAtomicWriteJson:
    def test_write_creates_file(self, tmp_path: Path) -> None:
        path = tmp_path / "out.json"
        atomic_write_json(path, {"hello": "world"})
        assert path.exists()
        assert json.loads(path.read_text(encoding="utf-8")) == {"hello": "world"}

    def test_write_pretty_prints(self, tmp_path: Path) -> None:
        path = tmp_path / "out.json"
        atomic_write_json(path, {"a": 1})
        content = path.read_text(encoding="utf-8")
        assert content == '{\n  "a": 1\n}\n'

    def test_write_trailing_newline(self, tmp_path: Path) -> None:
        path = tmp_path / "out.json"
        atomic_write_json(path, {})
        assert path.read_text(encoding="utf-8").endswith("\n")

    def test_write_creates_parent_dirs(self, tmp_path: Path) -> None:
        path = tmp_path / "sub" / "dir" / "out.json"
        atomic_write_json(path, {"ok": True})
        assert path.exists()

    def test_write_preserves_permissions(self, tmp_path: Path) -> None:
        path = tmp_path / "perm.json"
        path.write_text("{}", encoding="utf-8")
        if os.name != "nt":
            os.chmod(str(path), 0o644)
            atomic_write_json(path, {"updated": True})
            mode = stat.S_IMODE(os.stat(str(path)).st_mode)
            assert mode == 0o644

    def test_write_new_file_mode_600(self, tmp_path: Path) -> None:
        path = tmp_path / "new.json"
        atomic_write_json(path, {"new": True})
        if os.name != "nt":
            mode = stat.S_IMODE(os.stat(str(path)).st_mode)
            assert mode == 0o600

    def test_write_unicode(self, tmp_path: Path) -> None:
        path = tmp_path / "unicode.json"
        atomic_write_json(path, {"msg": "한국어"})
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["msg"] == "한국어"

    def test_write_no_leftover_tmp(self, tmp_path: Path) -> None:
        path = tmp_path / "out.json"
        atomic_write_json(path, {"ok": True})
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert len(tmp_files) == 0


class TestReadYaml:
    def test_read_yaml_file(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("gateway_url: https://gw.example.com\n", encoding="utf-8")
        result = read_yaml(path)
        assert result == {"gateway_url": "https://gw.example.com"}

    def test_read_yaml_nonexistent(self, tmp_path: Path) -> None:
        assert read_yaml(tmp_path / "missing.yaml") == {}

    def test_read_yaml_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.yaml"
        path.write_text("", encoding="utf-8")
        assert read_yaml(path) == {}
