# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Tests for cli.teardown — the `clear` engine (settings revert, OS-env restore,
backup sweep, post-teardown checks).

Windows-registry paths are exercised only for their POSIX-reachable logic
(snapshot selection, sweep, settings revert); the winreg calls themselves are
covered by the on-box E2E (plan T9).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli import teardown
from cli.teardown import (
    owned_user_settings_keys,
    post_teardown_checks,
    revert_user_settings,
    sweep_backups,
)


@pytest.fixture()
def backup_dir(tmp_path, monkeypatch):
    d = tmp_path / "backups"
    d.mkdir()
    monkeypatch.setenv("GATEWAY_CLI_BACKUP_DIR", str(d))
    return d


# ---------------------------------------------------------------------------
# owned-key derivation
# ---------------------------------------------------------------------------

def test_owned_keys_match_manifest_pins():
    """The eraser's key set mirrors what setup emits at the user tier."""
    top, env = owned_user_settings_keys()
    assert {"apiKeyHelper", "model", "availableModels"} <= top
    assert {
        "ANTHROPIC_BASE_URL",
        "ADMIN_API_URL",
        "OIDC_ISSUER_URL",
        "OIDC_CLIENT_ID",
        "GATEWAY_CLI_GATEWAY_URL",
    } <= env
    # Never touch keys we don't own.
    assert "ANTHROPIC_API_KEY" not in env
    assert "CLAUDE_CODE_USE_BEDROCK" not in env


# ---------------------------------------------------------------------------
# settings.json revert
# ---------------------------------------------------------------------------

def _write_settings(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def test_revert_removes_owned_keys_preserves_others(tmp_path, backup_dir):
    settings = tmp_path / "settings.json"
    _write_settings(settings, {
        "apiKeyHelper": "C:/x/api-key-helper.exe",
        "model": "claude-sonnet-4-6",
        "userOwnedKey": "keep-me",
        "env": {
            "OIDC_ISSUER_URL": "https://idp",
            "ANTHROPIC_BASE_URL": "http://gw",
            "USER_OWNED_ENV": "keep-me-too",
        },
    })

    result = revert_user_settings(settings)

    assert result.changed
    data = json.loads(settings.read_text())
    assert "apiKeyHelper" not in data
    assert "model" not in data
    assert data["userOwnedKey"] == "keep-me"
    assert data["env"] == {"USER_OWNED_ENV": "keep-me-too"}
    assert "env.OIDC_ISSUER_URL" in result.removed


def test_revert_restores_pre_setup_value_from_earliest_snapshot(tmp_path, backup_dir):
    """A key the user had BEFORE setup comes back with its old value."""
    settings = tmp_path / "settings.json"
    _write_settings(settings, {"model": "claude-sonnet-4-6", "env": {"ANTHROPIC_BASE_URL": "http://gw"}})
    # Earliest snapshot = pre-setup state; a later one holds gateway's own values
    # and must NOT win.
    (backup_dir / "claude-code.settings.json.20260101T000000.bak").write_text(
        json.dumps({"model": "user-model", "env": {}}), encoding="utf-8"
    )
    (backup_dir / "claude-code.settings.json.20260601T000000.bak").write_text(
        json.dumps({"model": "claude-sonnet-4-6", "env": {"ANTHROPIC_BASE_URL": "http://gw"}}),
        encoding="utf-8",
    )

    result = revert_user_settings(settings)

    data = json.loads(settings.read_text())
    assert data["model"] == "user-model"          # restored, not removed
    assert "model" in result.restored
    assert "env" not in data                       # our env key removed, block emptied
    assert "env.ANTHROPIC_BASE_URL" in result.removed


def test_revert_empty_env_block_dropped(tmp_path, backup_dir):
    settings = tmp_path / "settings.json"
    _write_settings(settings, {"env": {"OIDC_CLIENT_ID": "cid"}})
    revert_user_settings(settings)
    assert "env" not in json.loads(settings.read_text())


def test_revert_missing_file_is_noop(tmp_path, backup_dir):
    result = revert_user_settings(tmp_path / "absent.json")
    assert not result.changed
    assert result.skipped_reason


def test_revert_unparseable_file_left_alone(tmp_path, backup_dir):
    settings = tmp_path / "settings.json"
    settings.write_text("{not json", encoding="utf-8")
    result = revert_user_settings(settings)
    assert not result.changed
    assert settings.read_text() == "{not json"  # refused to guess


def test_revert_idempotent(tmp_path, backup_dir):
    settings = tmp_path / "settings.json"
    _write_settings(settings, {"model": "claude-sonnet-4-6"})
    assert revert_user_settings(settings).changed
    second = revert_user_settings(settings)
    assert not second.changed  # Q3: idempotency = absence of owned keys


# ---------------------------------------------------------------------------
# POSIX rc-line removal
# ---------------------------------------------------------------------------

def test_strip_posix_rc_removes_only_our_block_lines(tmp_path):
    rc = tmp_path / ".zshrc"
    rc.write_text(
        "export PATH=$PATH:/usr/local/bin\n"
        "\n"
        "# LLM Gateway — added by gateway-cli env --persist\n"
        'export OIDC_ISSUER_URL="https://idp"\n'
        'export ANTHROPIC_BASE_URL="http://gw"\n'
        'export MY_OWN_VAR="untouched"\n',
        encoding="utf-8",
    )
    changed = teardown._strip_posix_rc(rc)
    assert changed
    text = rc.read_text()
    assert "OIDC_ISSUER_URL" not in text
    assert "ANTHROPIC_BASE_URL" not in text
    assert "MY_OWN_VAR" in text                    # not ours — kept
    assert "gateway-cli env --persist" not in text  # marker gone
    assert "export PATH=$PATH:/usr/local/bin" in text


def test_strip_posix_rc_noop_when_absent(tmp_path):
    rc = tmp_path / ".zshrc"
    rc.write_text("export FOO=bar\n", encoding="utf-8")
    assert not teardown._strip_posix_rc(rc)
    assert rc.read_text() == "export FOO=bar\n"


def test_restore_os_env_posix_cleans_both_rc_files(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".zshrc").write_text(
        '# LLM Gateway — added by gateway-cli env --persist\nexport OIDC_CLIENT_ID="x"\n',
        encoding="utf-8",
    )
    result = teardown.restore_os_env()
    assert result.changed
    assert str(tmp_path / ".zshrc") in result.rc_files


# ---------------------------------------------------------------------------
# backup sweep
# ---------------------------------------------------------------------------

def test_sweep_removes_only_owned_prefixes(backup_dir):
    ours = [
        backup_dir / "claude-code.settings.json.20260101T000000.bak",
        backup_dir / "claude-code-managed.managed-settings.json.20260101T000000.bak",
        backup_dir / "gateway-cli-hkcu-env.Environment.20260101T000000.json.bak",
    ]
    theirs = [
        backup_dir / "cowork.config.20260101T000000.bak",   # sibling tool's snapshot
        backup_dir / "random-notes.txt",
    ]
    for p in ours + theirs:
        p.write_text("{}", encoding="utf-8")

    removed = sweep_backups()

    assert sorted(removed) == sorted(ours)
    for p in ours:
        assert not p.exists()
    for p in theirs:
        assert p.exists()  # never touch another tenant's files


def test_sweep_refuses_symlink_escape(backup_dir, tmp_path):
    outside = tmp_path / "outside.bak"
    outside.write_text("precious", encoding="utf-8")
    link = backup_dir / "claude-code.evil.20260101T000000.bak"
    link.symlink_to(outside)

    sweep_backups()

    assert outside.exists()  # the escape guard kept the target


def test_sweep_missing_dir_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_CLI_BACKUP_DIR", str(tmp_path / "nope"))
    assert sweep_backups() == []


# ---------------------------------------------------------------------------
# post-teardown checks
# ---------------------------------------------------------------------------

def test_post_teardown_clean_home(tmp_path, monkeypatch, backup_dir):
    monkeypatch.setenv("HOME", str(tmp_path))
    # Point every stateful surface at the empty tmp home.
    monkeypatch.setattr(teardown, "_user_settings_path", lambda: tmp_path / ".claude" / "settings.json")
    monkeypatch.setattr("cli.managed.is_gateway_enabled", lambda: False)
    monkeypatch.setattr("cli.login.load_tokens", lambda: None)
    monkeypatch.setattr("cli.login.load_vk_cache", lambda: None)
    monkeypatch.setattr(teardown, "oidc_tokens_path", lambda: tmp_path / "t.json")
    monkeypatch.setattr(teardown, "vk_cache_path", lambda: tmp_path / "v.json")

    checks = post_teardown_checks()

    assert set(checks) == {"managed-settings", "user-settings", "os-env", "tokens", "backups"}
    assert all(v == "ok" for v in checks.values()), checks


def test_post_teardown_flags_residue(tmp_path, monkeypatch, backup_dir):
    monkeypatch.setenv("HOME", str(tmp_path))
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text(json.dumps({"apiKeyHelper": "x"}), encoding="utf-8")
    (backup_dir / "claude-code.settings.json.20260101T000000.bak").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(teardown, "_user_settings_path", lambda: claude_dir / "settings.json")
    monkeypatch.setattr("cli.managed.is_gateway_enabled", lambda: True)
    monkeypatch.setattr("cli.login.load_tokens", lambda: None)
    monkeypatch.setattr("cli.login.load_vk_cache", lambda: None)
    monkeypatch.setattr(teardown, "oidc_tokens_path", lambda: tmp_path / "t.json")
    monkeypatch.setattr(teardown, "vk_cache_path", lambda: tmp_path / "v.json")

    checks = post_teardown_checks()

    assert checks["managed-settings"] == "residue"
    assert checks["user-settings"] == "residue"
    assert checks["backups"] == "residue"
    assert checks["tokens"] == "ok"
