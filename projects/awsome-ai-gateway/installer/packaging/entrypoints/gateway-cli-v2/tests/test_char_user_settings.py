# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Characterization: pin the ``~/.claude/settings.json`` (user tier) setup writes.

Golden snapshot taken BEFORE the Phase 3-5 resolve/emit rewrite (TC.2, gate for
P3-P5). The user-settings block lives inline in :func:`cli.setup.run_setup`
(lines ~498-567); the T5.3 rewrite must reproduce it. We drive the REAL code path
and stub only the boundaries that need privilege or network:

* ``write_gateway_settings`` — the managed-tier write (needs admin/sudo);
* ``_resolve_user_id``       — the OIDC/STS identity lookup (network);
* ``configured_ca_bundle``   — the baked corporate CA path; and
* ``backup_config``          — the timestamped on-disk backup.

Site-extra (``user`` section) and the user-settings file are steered through
their real env-var overrides (``GATEWAY_CLI_SITE_EXTRA`` / ``GATEWAY_CLI_SETTINGS_PATH``),
so the merge/reconcile logic is exercised, not mocked.

AR-F2 is the load-bearing case: the user-tier ``OTEL_RESOURCE_ATTRIBUTES``
``user.id`` reconcile (setup.py:559-565) must fire ONLY when site-extra already
seeded the key at the user tier, and must NEVER inject the key when absent. Both
directions are snapshotted here (seeded → user.id replaced, other attrs kept;
absent → key not present) so the T5.3 rewrite cannot silently drop delivered
Fix 1 (gateway-cli-issues.md:255-257).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli import setup as setup_mod
from cli.manifest import ResolvedConfig

_GW = "https://gw.example.com"
_ADMIN = "https://admin.example.com"
_ISSUER = "https://issuer.example.com"
_CLIENT = "client-abc"
_HELPER = "/opt/gateway/api-key-helper"
_MODEL = "claude-sonnet-4-6"
_ROSTER = ["claude-sonnet-4-6", "claude-haiku-4-5"]
_USER_ID = "alice@corp"


def _run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    site_extra: dict | None = None,
    existing: dict | None = None,
    user_id: str | None = _USER_ID,
    ca_bundle: str | None = None,
    **run_kwargs: object,
) -> tuple[setup_mod.SetupResult, dict, str]:
    """Run ``run_setup`` with boundaries stubbed; return (result, on-disk dict, raw text)."""
    settings_path = tmp_path / "settings.json"
    if existing is not None:
        settings_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    monkeypatch.setenv("GATEWAY_CLI_SETTINGS_PATH", str(settings_path))

    if site_extra is not None:
        se_path = tmp_path / "site_extra.json"
        se_path.write_text(json.dumps(site_extra), encoding="utf-8")
        monkeypatch.setenv("GATEWAY_CLI_SITE_EXTRA", str(se_path))
    else:
        monkeypatch.delenv("GATEWAY_CLI_SITE_EXTRA", raising=False)

    # Stub the privileged/networked boundaries; the user-settings logic is real.
    monkeypatch.setattr(setup_mod, "write_gateway_settings", lambda **kw: tmp_path / "managed.json")
    monkeypatch.setattr(setup_mod, "_resolve_user_id", lambda: user_id)
    monkeypatch.setattr(setup_mod, "configured_ca_bundle", lambda: ca_bundle)
    monkeypatch.setattr(setup_mod, "backup_config", lambda *a, **k: None)

    cfg = ResolvedConfig(
        gateway_url=_GW, admin_api_url=_ADMIN,
        oidc_issuer_url=_ISSUER, oidc_client_id=_CLIENT,
    )
    result = setup_mod.run_setup(
        cfg,
        api_key_helper=_HELPER,
        statusline="statusline",
        model=_MODEL,
        persist_env=False,
        **run_kwargs,
    )
    text = settings_path.read_text(encoding="utf-8")
    return result, json.loads(text), text


def test_user_settings_golden_no_site_extra(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Representative user settings (with availableModels) are pinned byte-for-byte."""
    result, settings, text = _run(
        monkeypatch, tmp_path, available_models=_ROSTER,
    )
    expected = {
        "apiKeyHelper": _HELPER,
        "model": _MODEL,
        "availableModels": _ROSTER,
        "env": {
            "ANTHROPIC_BASE_URL": _GW,
            "ADMIN_API_URL": _ADMIN,
            "GATEWAY_CLI_GATEWAY_URL": _ADMIN,
            "OIDC_ISSUER_URL": _ISSUER,
            "OIDC_CLIENT_ID": _CLIENT,
        },
    }
    assert settings == expected
    # Byte-for-byte: the writer emits json.dumps(indent=2, ensure_ascii=False)+"\n".
    assert text == json.dumps(expected, indent=2, ensure_ascii=False) + "\n"
    assert result.cleaned_settings == []
    # AR-F2 absent case: the user-tier key is NEVER injected when unseeded.
    assert "OTEL_RESOURCE_ATTRIBUTES" not in settings["env"]


def test_optional_default_models_written_only_when_supplied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ANTHROPIC_DEFAULT_*_MODEL land in user env only for the flags supplied."""
    _, settings, _ = _run(
        monkeypatch, tmp_path,
        default_opus_model="claude-opus-4-6",
        default_haiku_model="claude-haiku-4-5",
    )
    env = settings["env"]
    assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "claude-opus-4-6"
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "claude-haiku-4-5"
    assert "ANTHROPIC_DEFAULT_SONNET_MODEL" not in env  # flag not supplied
    assert "availableModels" not in settings  # roster not supplied


def test_permissions_from_site_extra_are_merged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A structured ``permissions`` object from site-extra survives into user settings."""
    _, settings, _ = _run(
        monkeypatch, tmp_path,
        site_extra={"user": {"permissions": {"allow": ["Bash(git*)"]}}},
    )
    assert settings["permissions"] == {"allow": ["Bash(git*)"]}


def test_ar_f2_user_tier_otel_reconciled_when_seeded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AR-F2 seeded case: site-extra user.env seeds OTEL_RESOURCE_ATTRIBUTES ⇒
    user.id is replaced with the resolved identity, other attributes preserved."""
    _, settings, _ = _run(
        monkeypatch, tmp_path,
        site_extra={
            "user": {
                "env": {
                    "OTEL_RESOURCE_ATTRIBUTES": "service.name=claude-code,user.id=stale@old"
                }
            }
        },
    )
    attrs = settings["env"]["OTEL_RESOURCE_ATTRIBUTES"]
    assert attrs == "service.name=claude-code,user.id=alice@corp"
    assert "stale@old" not in attrs           # stale baked id overwritten
    assert "service.name=claude-code" in attrs  # static site attr preserved


def test_ar_f2_user_tier_otel_not_injected_when_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AR-F2 absent case: with no seed AND a resolved user_id, the key stays absent."""
    _, settings, _ = _run(monkeypatch, tmp_path, user_id=_USER_ID)
    assert "OTEL_RESOURCE_ATTRIBUTES" not in settings["env"]


def test_reconcile_strips_stale_user_tier_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pre-existing bypass/strip-only keys are removed and reported by setup."""
    result, settings, _ = _run(
        monkeypatch, tmp_path,
        existing={
            "statusLine": {"type": "command", "command": "/old/statusline"},
            "modelOverrides": {"foo": "bar"},
            "env": {"ANTHROPIC_API_KEY": "sk-stale", "CLAUDE_CODE_USE_BEDROCK": "1"},
        },
    )
    # Stripped from user settings…
    assert "statusLine" not in settings           # managed tier owns it now
    assert "modelOverrides" not in settings
    assert "ANTHROPIC_API_KEY" not in settings["env"]
    assert "CLAUDE_CODE_USE_BEDROCK" not in settings["env"]
    # …and reported on the result.
    assert "settings.json (top-level): statusLine" in result.cleaned_settings
    assert "settings.json (top-level): modelOverrides" in result.cleaned_settings
    assert "settings.json env: ANTHROPIC_API_KEY" in result.cleaned_settings
    assert "settings.json env: CLAUDE_CODE_USE_BEDROCK" in result.cleaned_settings
