# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Config file read/write utilities with atomic write support (RP-03)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def read_json(path: str | Path) -> dict:
    """Read and parse a JSON config file.

    Returns empty dict if file doesn't exist.
    Raises ValueError on parse error (BR-CONFIG-03).
    """
    path = Path(path)
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON parse error in {path}: {exc}") from exc


def atomic_write_json(path: str | Path, data: dict, indent: int = 2) -> None:
    """Write JSON atomically using temp file + rename (RP-03).

    - Preserves original file permissions if file exists (BR-CONFIG-04)
    - Creates parent directories if needed
    - New files get mode 0o600 (owner read/write only)
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    content = json.dumps(data, indent=indent, ensure_ascii=False) + "\n"

    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)

        # Preserve original file permissions (BR-CONFIG-04)
        if path.exists():
            st = os.stat(path)
            os.chmod(tmp_path, st.st_mode)
        else:
            os.chmod(tmp_path, 0o600)

        os.replace(tmp_path, str(path))  # atomic on POSIX, best-effort on Windows
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def read_yaml(path: str | Path) -> dict:
    """Read and parse a YAML config file using safe_load (SECURITY-13)."""
    import yaml

    path = Path(path)
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}
