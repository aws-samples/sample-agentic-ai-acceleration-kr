# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""T6.2: `env --persist` snapshots prior HKCU values before overwriting them.

Windows persist replaces HKCU\\Environment values in place via
``winreg.SetValueEx`` — no undo. :func:`cli.env._persist_windows` now reads each
value first and snapshots any pre-existing one via
:func:`cli.utils.backup.backup_values`, so a user value the persist step
clobbered stays recoverable.

There is no real registry on the POSIX dev box, so we inject a minimal fake
``winreg`` module (dict-backed) that mirrors the handful of calls the code
makes, including ``QueryValueEx`` raising ``FileNotFoundError`` for an absent
value — the real winreg contract.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from cli import env as env_mod


class _FakeKey:
    """Context-manager registry handle backed by a shared dict."""

    def __init__(self, store: dict[str, str]) -> None:
        self.store = store

    def __enter__(self) -> "_FakeKey":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _FakeWinreg:
    """Just enough of the ``winreg`` surface ``_persist_windows`` touches."""

    HKEY_CURRENT_USER = "HKCU"
    KEY_SET_VALUE = 0x0002
    KEY_QUERY_VALUE = 0x0001
    REG_SZ = 1

    def __init__(self, initial: dict[str, str]) -> None:
        self.store = dict(initial)

    def OpenKey(self, root: str, sub: str, res: int, access: int) -> _FakeKey:  # noqa: N802
        return _FakeKey(self.store)

    def QueryValueEx(self, key: _FakeKey, name: str) -> tuple[str, int]:  # noqa: N802
        if name not in key.store:
            raise FileNotFoundError(name)  # real winreg raises this for a missing value
        return key.store[name], self.REG_SZ

    def SetValueEx(  # noqa: N802
        self, key: _FakeKey, name: str, res: int, typ: int, val: str
    ) -> None:
        key.store[name] = val


def _snapshots(backup_dir: Path) -> list[Path]:
    return sorted(backup_dir.glob("gateway-cli-hkcu-env.Environment.*.json.bak"))


def test_prior_hkcu_value_snapshotted_before_overwrite(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An existing HKCU value is captured to a recoverable snapshot before replace."""
    fake = _FakeWinreg({"ANTHROPIC_BASE_URL": "https://old.example.com", "UNRELATED": "keep"})
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setenv("GATEWAY_CLI_BACKUP_DIR", str(tmp_path))

    written = env_mod._persist_windows(
        {"ANTHROPIC_BASE_URL": "https://new.example.com", "OIDC_CLIENT_ID": "cid"}
    )

    assert set(written) == {"ANTHROPIC_BASE_URL", "OIDC_CLIENT_ID"}
    # New values landed in the registry; an unrelated key is untouched.
    assert fake.store["ANTHROPIC_BASE_URL"] == "https://new.example.com"
    assert fake.store["OIDC_CLIENT_ID"] == "cid"
    assert fake.store["UNRELATED"] == "keep"

    # Exactly one snapshot, and it recovers the prior value of the replaced key.
    snaps = _snapshots(tmp_path)
    assert len(snaps) == 1
    restored = json.loads(snaps[0].read_text(encoding="utf-8"))
    assert restored == {"ANTHROPIC_BASE_URL": "https://old.example.com"}
    # OIDC_CLIENT_ID had no prior value → nothing to recover, absent from snapshot.
    assert "OIDC_CLIENT_ID" not in restored


def test_no_snapshot_when_no_prior_value_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A first-time write clobbers nothing, so no snapshot file is produced."""
    fake = _FakeWinreg({})
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setenv("GATEWAY_CLI_BACKUP_DIR", str(tmp_path))

    env_mod._persist_windows({"ANTHROPIC_BASE_URL": "https://new.example.com"})

    assert fake.store["ANTHROPIC_BASE_URL"] == "https://new.example.com"
    assert _snapshots(tmp_path) == []
