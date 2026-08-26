# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Unit tests for the Windows registry-scope choice (HKCU vs HKLM).

The installer records the operator's wizard choice in a ``registry-scope.conf``
file next to the exe; ``setup`` resolves the effective scope as
``--scope flag > installer file > 'user'`` and threads it into the config so the
Windows writer targets the chosen hive. These tests cover that resolution and the
``build_config`` plumbing without needing a live registry (the ``winreg`` write is
Windows-only and exercised on the box).
"""

from __future__ import annotations

import pytest

from cli import cowork_config, managed

# --- build_config: scope plumbing + validation ------------------------------

def test_build_config_defaults_to_user_scope():
    cfg = cowork_config.build_config(
        base_url="https://x.cloudfront.net",
        models=["m"],
        credential_kind="static",
        api_key="vk",
    )
    assert cfg.registry_scope == "user"


def test_build_config_accepts_machine_scope_with_helper():
    # Machine scope is allowed with the helper-script credential (each user mints
    # their own key), which is the only safe multi-user model.
    cfg = cowork_config.build_config(
        base_url="https://x.cloudfront.net",
        models=["m"],
        credential_kind="helper-script",
        helper_path="/opt/api-key-helper",
        registry_scope="machine",
    )
    assert cfg.registry_scope == "machine"


def test_build_config_rejects_static_under_machine_scope():
    # A static VK under machine-wide HKLM would be readable by every local user,
    # leaking the configuring user's gateway identity — must be refused (Fix 1).
    with pytest.raises(ValueError, match="machine"):
        cowork_config.build_config(
            base_url="https://x.cloudfront.net",
            models=["m"],
            credential_kind="static",
            api_key="vk",
            registry_scope="machine",
        )


def test_build_config_allows_static_under_user_scope():
    cfg = cowork_config.build_config(
        base_url="https://x.cloudfront.net",
        models=["m"],
        credential_kind="static",
        api_key="vk",
        registry_scope="user",
    )
    assert cfg.registry_scope == "user"
    assert cfg.credential_kind == "static"


def test_build_config_rejects_machine_helper_under_user_profile(monkeypatch, tmp_path):
    # A machine (HKLM) policy names ONE helper path for every user, so a helper under
    # the installing user's profile is unreadable to others — refuse it (R2-2).
    profile = tmp_path / "userprofile"
    helper = profile / "AppData" / "Local" / "app" / "api-key-helper"
    helper.parent.mkdir(parents=True, exist_ok=True)
    helper.write_text("", encoding="utf-8")
    monkeypatch.setenv("USERPROFILE", str(profile))
    with pytest.raises(ValueError, match="machine-readable"):
        cowork_config.build_config(
            base_url="https://x.cloudfront.net",
            models=["m"],
            credential_kind="helper-script",
            helper_path=str(helper),
            registry_scope="machine",
        )


def test_build_config_allows_machine_helper_outside_profile(monkeypatch, tmp_path):
    # A helper in a shared/machine location (not under the user profile) is fine.
    profile = tmp_path / "userprofile"
    profile.mkdir()
    monkeypatch.setenv("USERPROFILE", str(profile))
    cfg = cowork_config.build_config(
        base_url="https://x.cloudfront.net",
        models=["m"],
        credential_kind="helper-script",
        helper_path=str(tmp_path / "ProgramFiles" / "GatewayCLI" / "api-key-helper"),
        registry_scope="machine",
    )
    assert cfg.registry_scope == "machine"


def test_build_config_allows_user_helper_under_profile(monkeypatch, tmp_path):
    # The profile-location rule is machine-scope only: a per-user policy legitimately
    # points at a helper under that user's own profile.
    profile = tmp_path / "userprofile"
    helper = profile / "AppData" / "Local" / "app" / "api-key-helper"
    helper.parent.mkdir(parents=True, exist_ok=True)
    helper.write_text("", encoding="utf-8")
    monkeypatch.setenv("USERPROFILE", str(profile))
    cfg = cowork_config.build_config(
        base_url="https://x.cloudfront.net",
        models=["m"],
        credential_kind="helper-script",
        helper_path=str(helper),
        registry_scope="user",
    )
    assert cfg.registry_scope == "user"


def test_build_config_rejects_unknown_scope():
    with pytest.raises(ValueError, match="registry_scope"):
        cowork_config.build_config(
            base_url="https://x.cloudfront.net",
            models=["m"],
            credential_kind="static",
            api_key="vk",
            registry_scope="everywhere",
        )


# --- resolve_registry_scope: flag > installer file > default -----------------

def test_explicit_flag_wins(monkeypatch):
    # Even with an installer file saying 'user', an explicit --scope machine wins.
    monkeypatch.setattr(managed, "installer_registry_scope", lambda: "user")
    assert cowork_config.resolve_registry_scope("machine") == "machine"


def test_installer_file_used_when_flag_omitted(monkeypatch):
    monkeypatch.setattr(managed, "installer_registry_scope", lambda: "machine")
    assert cowork_config.resolve_registry_scope(None) == "machine"


def test_defaults_to_user_when_nothing_set(monkeypatch):
    monkeypatch.setattr(managed, "installer_registry_scope", lambda: None)
    assert cowork_config.resolve_registry_scope(None) == "user"


def test_bogus_flag_falls_through_to_default(monkeypatch):
    monkeypatch.setattr(managed, "installer_registry_scope", lambda: None)
    assert cowork_config.resolve_registry_scope("nonsense") == "user"


# --- installer_registry_scope: reads the file next to the exe ----------------

def test_installer_scope_reads_pref_file(monkeypatch, tmp_path):
    exe = tmp_path / "gateway-cli-cowork.exe"
    exe.write_text("", encoding="utf-8")
    (tmp_path / "registry-scope.conf").write_text("machine\n", encoding="utf-8")
    monkeypatch.setattr(managed.sys, "executable", str(exe))
    assert managed.installer_registry_scope() == "machine"


def test_installer_scope_none_when_absent(monkeypatch, tmp_path):
    exe = tmp_path / "gateway-cli-cowork.exe"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(managed.sys, "executable", str(exe))
    assert managed.installer_registry_scope() is None


def test_installer_scope_none_on_garbage(monkeypatch, tmp_path):
    exe = tmp_path / "gateway-cli-cowork.exe"
    exe.write_text("", encoding="utf-8")
    (tmp_path / "registry-scope.conf").write_text("HKLM-ish", encoding="utf-8")
    monkeypatch.setattr(managed.sys, "executable", str(exe))
    assert managed.installer_registry_scope() is None


def test_root_alias_mapping():
    assert managed._win_root_for_scope("machine") == "HKLM"
    assert managed._win_root_for_scope("user") == "HKCU"
