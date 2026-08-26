# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""T6.3: detect managed-settings.d drop-ins that sort AFTER 50-gateway.json.

A drop-in whose filename sorts after ``50-gateway.json`` has its ``env`` merged
over ours and can override the gateway/OTEL/proxy keys — silently breaking
routing. gateway-cli never renames a foreign fragment; it reports them so an
operator can reorder. :func:`cli.managed._shadowing_dropins` is the detector
behind that warning (and the PROXY_PRECEDENCE.md verify hint).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cli import managed


@pytest.fixture
def dropin_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the managed root at a tmp dir and return its managed-settings.d."""
    root = tmp_path / "ClaudeCode"
    monkeypatch.setattr(managed, "_managed_root", lambda: root)
    d = root / managed.MANAGED_SETTINGS_DROPIN_DIRNAME
    d.mkdir(parents=True)
    return d


def _touch(d: Path, *names: str) -> None:
    for name in names:
        (d / name).write_text("{}", encoding="utf-8")


def test_flags_later_sorting_dropins(dropin_dir: Path) -> None:
    """Higher-prefix and same-prefix-but-later fragments are both flagged."""
    _touch(
        dropin_dir,
        managed.GATEWAY_DROPIN_FILENAME,  # ours — excluded
        "99-org.json",                    # higher prefix → sorts after
        "50-otel.json",                   # 'o' > 'g' → sorts after 50-gateway
        "40-early.json",                  # sorts before → not flagged
        "50-gateway.txt",                 # not .json → ignored
    )
    assert managed._shadowing_dropins() == ["50-otel.json", "99-org.json"]


def test_excludes_our_own_and_legacy_fragments(dropin_dir: Path) -> None:
    """Our fragment and the legacy 99-gateway.json we retire are never flagged."""
    _touch(
        dropin_dir,
        managed.GATEWAY_DROPIN_FILENAME,         # 50-gateway.json
        managed.LEGACY_GATEWAY_DROPIN_FILENAME,  # 99-gateway.json — ours, retired
    )
    assert managed._shadowing_dropins() == []


def test_no_dropin_dir_is_safe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A missing managed-settings.d yields no findings rather than raising."""
    monkeypatch.setattr(managed, "_managed_root", lambda: tmp_path / "absent")
    assert managed._shadowing_dropins() == []
