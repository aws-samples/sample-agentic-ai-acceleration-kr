# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Scope-aware rollback state, machine-setup lock, and ownership fingerprint.

These cover the HKLM/HKCU hardening (see
``docs/cowork-registry-scope-hardening-plan.md``) that does NOT need a live
registry: the marker/backup location split (per-user vs machine-wide ProgramData),
the cross-process setup lock, the ownership fingerprint, and the scope-spanning
teardown sweep. The actual ``winreg`` writes/reads are Windows-only and validated
on the box; here we drive the pure-Python bookkeeping via the data-dir env
overrides (``GATEWAY_CLI_DATA_DIR`` / ``GATEWAY_CLI_MACHINE_DATA_DIR``).
"""

from __future__ import annotations

import json
import os
import sys

import pytest

from cli import managed, paths


@pytest.fixture
def scoped_dirs(tmp_path, monkeypatch):
    """Point per-user and machine-wide data dirs at isolated tmp subdirs."""
    user = tmp_path / "user"
    machine = tmp_path / "machine"
    monkeypatch.setenv("GATEWAY_CLI_DATA_DIR", str(user))
    monkeypatch.setenv("GATEWAY_CLI_MACHINE_DATA_DIR", str(machine))
    # Ensure the shared-sink override is not set — we want the two-dir behaviour.
    monkeypatch.delenv("GATEWAY_CLI_BACKUP_DIR", raising=False)
    return user, machine


# --- marker location split ---------------------------------------------------

def test_marker_paths_differ_by_scope(scoped_dirs):
    user, machine = scoped_dirs
    assert managed._marker_path("user").parent == user
    assert managed._marker_path("machine").parent == machine


def test_write_read_clear_marker_is_scoped(scoped_dirs):
    managed._write_marker({"kind": "registry", "root": "HKLM"}, "machine")
    # The user scope must not see the machine marker and vice-versa.
    assert managed._read_marker("machine") == {"kind": "registry", "root": "HKLM"}
    assert managed._read_marker("user") is None
    managed._clear_marker("machine")
    assert managed._read_marker("machine") is None


# --- _find_active_marker: machine wins, then user ----------------------------

def test_find_active_marker_prefers_machine(scoped_dirs):
    managed._write_marker({"kind": "registry", "root": "HKCU"}, "user")
    managed._write_marker({"kind": "registry", "root": "HKLM"}, "machine")
    marker, scope = managed._find_active_marker()
    assert scope == "machine"
    assert marker["root"] == "HKLM"


def test_find_active_marker_user_only(scoped_dirs):
    managed._write_marker({"kind": "registry", "root": "HKCU"}, "user")
    marker, scope = managed._find_active_marker()
    assert scope == "user"
    assert marker["root"] == "HKCU"


def test_find_active_marker_none(scoped_dirs):
    marker, scope = managed._find_active_marker()
    assert marker is None
    assert scope == "user"


def test_marker_exists_spans_scopes(scoped_dirs):
    assert managed.marker_exists() is False
    managed._write_marker({"kind": "registry"}, "machine")
    assert managed.marker_exists() is True


# --- ownership fingerprint ---------------------------------------------------

def test_fingerprint_stable_and_order_independent():
    a = managed._managed_fingerprint(
        {"inferenceProvider": "gateway", "inferenceGatewayBaseUrl": "https://x",
         "inferenceModels": "[\"m\"]", "inferenceCredentialKind": "helper-script"}
    )
    b = managed._managed_fingerprint(
        {"inferenceCredentialKind": "helper-script", "inferenceModels": "[\"m\"]",
         "inferenceGatewayBaseUrl": "https://x", "inferenceProvider": "gateway",
         "someExtraKey": "ignored"}
    )
    assert a == b


def test_fingerprint_changes_with_core_value():
    base = {"inferenceProvider": "gateway", "inferenceGatewayBaseUrl": "https://x",
            "inferenceModels": "[\"m\"]", "inferenceCredentialKind": "helper-script"}
    changed = {**base, "inferenceGatewayBaseUrl": "https://y"}
    assert managed._managed_fingerprint(base) != managed._managed_fingerprint(changed)


# --- machine setup lock ------------------------------------------------------

def test_lock_is_mutually_exclusive(scoped_dirs):
    with managed._machine_setup_lock():
        with pytest.raises(TimeoutError):
            with managed._machine_setup_lock(timeout=0.2, poll=0.05):
                pass  # pragma: no cover — should never enter


def test_lock_released_after_context(scoped_dirs):
    with managed._machine_setup_lock():
        pass
    # A second acquisition must succeed once the first context exits.
    with managed._machine_setup_lock(timeout=0.5):
        pass


def test_lock_steals_stale(scoped_dirs, monkeypatch):
    _, machine = scoped_dirs
    machine.mkdir(parents=True, exist_ok=True)
    lock = machine / managed._LOCK_NAME
    lock.write_text("", encoding="utf-8")
    # Backdate well past the staleness threshold so it is treated as crash-leftover.
    old = os.stat(lock).st_mtime - (managed._LOCK_STALE_SEC + 30)
    os.utime(lock, (old, old))
    with managed._machine_setup_lock(timeout=1.0):
        pass  # steals the stale lock instead of timing out


# --- scope-spanning teardown sweep -------------------------------------------

def test_sweep_backups_clears_both_scopes(scoped_dirs):
    user, machine = scoped_dirs
    # A marker + a snapshot in each scope's data dir.
    managed._write_marker({"kind": "registry"}, "user")
    managed._write_marker({"kind": "registry"}, "machine")
    for base in (user, machine):
        bdir = base / "backups"
        bdir.mkdir(parents=True, exist_ok=True)
        (bdir / "cowork.HKLM_x.20260101T000000.regbak.json").write_text("{}", "utf-8")
        (bdir / "cowork.settings.20260101T000000.bak").write_text("{}", "utf-8")
        # A foreign file must be left untouched (ownership-scoped to cowork. prefix).
        (bdir / "claude-code.settings.20260101T000000.bak").write_text("{}", "utf-8")

    removed = managed.sweep_backups()

    assert managed._marker_path("user").is_file() is False
    assert managed._marker_path("machine").is_file() is False
    # Both scopes' cowork.* snapshots removed; the foreign file kept.
    assert all("claude-code" not in str(p) for p in removed)
    for base in (user, machine):
        kept = list((base / "backups").glob("claude-code.*"))
        assert len(kept) == 1
        assert list((base / "backups").glob("cowork.*")) == []


# --- remove_config bookkeeping (no winreg) -----------------------------------

def test_remove_config_no_marker_is_noop(scoped_dirs):
    result = managed.remove_config()
    assert result.ok is True
    assert result.changed is False
    assert "nothing to remove" in result.detail


def test_remove_config_machine_registry_missing_backup(scoped_dirs):
    # A machine-scope registry marker whose backup file is gone: remove_config must
    # take the machine branch (under the lock), report failure, and name HKLM.
    managed._write_marker(
        {"kind": "registry", "regbackup_path": "/nonexistent/x.regbak.json",
         "root": "HKLM"},
        "machine",
    )
    result = managed.remove_config()
    assert result.ok is False
    assert "HKLM" in result.detail


# --- R2-1: machine-rollback trust boundary -----------------------------------

class _FakeSnap:
    """Minimal stand-in for RegistryBackup for the pure validation checks."""

    def __init__(self, root, subkey, values):
        self.root = root
        self.subkey = subkey
        self.values = values


def test_validate_snapshot_accepts_tool_owned():
    snap = _FakeSnap("HKLM", managed._WIN_SUBKEY,
                     {"inferenceProvider": {}, "inferenceModels": {}})
    assert managed._validate_registry_snapshot(snap, expected_root="HKLM") is None


def test_validate_snapshot_rejects_root_mismatch():
    snap = _FakeSnap("HKCU", managed._WIN_SUBKEY, {"inferenceProvider": {}})
    reason = managed._validate_registry_snapshot(snap, expected_root="HKLM")
    assert reason and "root" in reason


def test_validate_snapshot_rejects_foreign_subkey():
    snap = _FakeSnap("HKLM", r"SOFTWARE\Microsoft\Windows", {"inferenceProvider": {}})
    reason = managed._validate_registry_snapshot(snap, expected_root="HKLM")
    assert reason and "managed policy key" in reason


def test_validate_snapshot_rejects_foreign_value_names():
    snap = _FakeSnap("HKLM", managed._WIN_SUBKEY,
                     {"inferenceProvider": {}, "EnableLUA": {}})
    reason = managed._validate_registry_snapshot(snap, expected_root="HKLM")
    assert reason and "EnableLUA" in reason


def test_backup_within_scope_dir(scoped_dirs):
    _, machine = scoped_dirs
    inside = machine / "backups" / "cowork.HKLM.regbak.json"
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.write_text("{}", "utf-8")
    assert managed._backup_within_scope_dir(str(inside), "machine") is True
    outside = machine.parent / "evil.regbak.json"
    outside.write_text("{}", "utf-8")
    assert managed._backup_within_scope_dir(str(outside), "machine") is False


def test_remove_config_refuses_backup_outside_scope_dir(scoped_dirs, tmp_path):
    # A marker pointing at a real file OUTSIDE the machine state dir must be refused
    # before any elevated restore — a redirected marker cannot pick the backup file.
    evil = tmp_path / "planted.regbak.json"
    evil.write_text("{}", "utf-8")
    managed._write_marker(
        {"kind": "registry", "regbackup_path": str(evil), "root": "HKLM"}, "machine",
    )
    result = managed.remove_config()
    assert result.ok is False
    assert "outside the machine-scope state directory" in result.detail


def test_remove_config_refuses_foreign_snapshot(scoped_dirs):
    # A backup INSIDE our state dir but targeting a foreign key is refused as
    # not-tool-owned (a hand-edited snapshot cannot retarget the restore).
    _, machine = scoped_dirs
    bdir = machine / "backups"
    bdir.mkdir(parents=True, exist_ok=True)
    snap_path = bdir / "cowork.HKLM_evil.regbak.json"
    snap_path.write_text(json.dumps({
        "root": "HKLM",
        "subkey": r"SOFTWARE\Microsoft\Windows",
        "key_existed": True,
        "values": {"inferenceProvider": {"present": False}},
        "tool_name": "cowork",
        "created_at": "2026-01-01T00:00:00+00:00",
    }), "utf-8")
    managed._write_marker(
        {"kind": "registry", "regbackup_path": str(snap_path), "root": "HKLM"}, "machine",
    )
    result = managed.remove_config()
    assert result.ok is False
    assert "not tool-owned" in result.detail


def test_harden_machine_dir_noop_off_windows(scoped_dirs):
    # Off Windows the ACL step is skipped but the dir is still created (fail-open is
    # fine here — there is no cross-user hive to protect on macOS/CI).
    ok, detail = managed._harden_machine_dir()
    assert ok is True
    assert "skipped" in detail
    _, machine = scoped_dirs
    assert machine.is_dir()


# --- R2-1: release-locked env overrides --------------------------------------

def test_release_locked_env_ignored_when_frozen(monkeypatch):
    monkeypatch.setenv("GATEWAY_CLI_MACHINE_DATA_DIR", "/tmp/attacker-controlled")
    monkeypatch.setattr(paths.sys, "frozen", True, raising=False)
    assert paths._release_locked_env("GATEWAY_CLI_MACHINE_DATA_DIR") is None
    # machine_data_dir falls back to the platform site dir, NOT the override.
    assert str(paths.machine_data_dir()) != "/tmp/attacker-controlled"


def test_release_locked_env_honored_when_not_frozen(monkeypatch):
    monkeypatch.setenv("GATEWAY_CLI_BACKUP_DIR", "/tmp/test-sink")
    monkeypatch.delattr(paths.sys, "frozen", raising=False)
    assert paths._release_locked_env("GATEWAY_CLI_BACKUP_DIR") == "/tmp/test-sink"


# --- R2-3: scope switch consults the live opposite hive ----------------------

def test_hive_has_gateway_policy_false_off_windows():
    # The live-hive read is a Windows-only signal; off Windows it is inert (the real
    # cross-hive refusal is exercised on the box).
    assert managed._hive_has_gateway_policy("machine") is False
    assert managed._hive_has_gateway_policy("user") is False


# --- Windows write path (fake winreg) driven off-box -------------------------

class _FakeRegKey:
    def __init__(self):
        self.values: dict = {}


class _FakeWinreg:
    """Just enough of the winreg module for :func:`managed._write_windows_impl`."""

    HKEY_CURRENT_USER = "HKCU"
    HKEY_LOCAL_MACHINE = "HKLM"
    KEY_ALL_ACCESS = 0xF003F
    KEY_READ = 0x20019
    REG_SZ = 1

    def __init__(self):
        self.store: dict = {}

    def CreateKeyEx(self, hive, sub, reserved, access):
        key = self.store.get((hive, sub)) or _FakeRegKey()
        self.store[(hive, sub)] = key
        return key

    def SetValueEx(self, key, name, reserved, typ, value):
        key.values[name] = (typ, value)

    def DeleteValue(self, key, name):
        if name in key.values:
            del key.values[name]
        else:
            raise FileNotFoundError(name)

    def CloseKey(self, key):
        pass

    def OpenKey(self, hive, sub, reserved=0, access=0):
        key = self.store.get((hive, sub))
        if key is None:
            raise FileNotFoundError(sub)
        return key

    def QueryInfoKey(self, key):
        return (0, len(key.values), 0)

    def EnumValue(self, key, index):
        name = list(key.values)[index]
        typ, value = key.values[name]
        return (name, value, typ)

    def DeleteKey(self, hive, sub):
        self.store.pop((hive, sub), None)


def test_write_windows_impl_user_scope_writes_gateway_values(scoped_dirs, monkeypatch):
    # Regression for the base_dir/backup_dir kwarg mismatch: managed called
    # snapshot_registry_values(base_dir=...) whose parameter is backup_dir=, so EVERY
    # first-time Windows setup raised TypeError. The macOS suite never routes here
    # (write_config -> _write_macos), so drive _write_windows_impl directly with a fake
    # winreg while using the REAL backup_util — a wrong kwarg re-breaks this test.
    fake = _FakeWinreg()
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(managed.sys, "platform", "win32")
    monkeypatch.setattr(managed, "hklm_conflict", lambda: False)
    monkeypatch.setattr(managed, "_hive_has_gateway_policy", lambda scope: False)

    cfg = managed.build_config(
        base_url="https://cowork-inference.example.com/", models=["claude-x"],
        credential_kind="helper-script",
        helper_path=r"C:\Gateway-CLI-Cowork\api-key-helper.exe",
        registry_scope="user",
    )
    res = managed._write_windows_impl(cfg)

    assert res.ok and res.changed, res.detail
    key = fake.store[(fake.HKEY_CURRENT_USER, managed._WIN_SUBKEY)]
    assert key.values["inferenceProvider"][1] == "gateway"
    assert key.values["inferenceProvider"][0] == fake.REG_SZ
    # First setup captured a durable revert marker (this is the code path that
    # threaded backup_dir= into the real snapshotter).
    marker = managed._read_marker("user")
    assert marker is not None and marker["kind"] == "registry"
    assert marker["root"] == "HKCU"
