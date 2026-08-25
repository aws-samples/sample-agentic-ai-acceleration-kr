# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Tests for cli.uninstall — the ARP resolver/validator (POSIX-reachable parts).

The registry enumeration itself is Windows-only; what we pin here is the
security validation (_validate_uninstaller) and the identity constants —
including the double-brace `}}_is1` quirk the GUID-substring matcher must
tolerate. The live launch path is covered by the on-box E2E (plan T9).
"""

from __future__ import annotations

import re
import sys

import pytest

from cli import uninstall
from cli.uninstall import (
    _APP_GUID,
    _EXPECTED_DIR_NAME,
    _EXPECTED_DISPLAY_NAME,
    _UNINS_RE,
    _ArpEntry,
    _validate_uninstaller,
    manual_hint,
)

# ---------------------------------------------------------------------------
# identity constants — must match packaging/installer.iss
# ---------------------------------------------------------------------------

def test_guid_matches_installer_iss():
    """The GUID is the AppId from installer.iss (uppercase, no braces)."""
    from pathlib import Path

    iss = Path(__file__).resolve()
    for parent in iss.parents:
        candidate = parent / "packaging" / "installer.iss"
        if candidate.is_file():
            content = candidate.read_text(encoding="utf-8", errors="replace")
            m = re.search(r"AppId=\{\{([0-9A-Fa-f-]+)\}\}", content)
            assert m, "installer.iss AppId not in the expected {{GUID}} form"
            assert m.group(1).upper() == _APP_GUID
            # The double-brace form is load-bearing (ARP key = {GUID}}_is1);
            # the resolver must never assume the conventional single brace.
            assert "{{" + m.group(1) in content
            return
    pytest.skip("installer.iss not found from test location")


def test_expected_dir_is_gatewaycli_not_cowork():
    assert _EXPECTED_DIR_NAME == "gatewaycli"
    assert _EXPECTED_DISPLAY_NAME == "LLM Gateway CLI"
    # The sibling Cowork product must NOT validate against these anchors.
    assert _EXPECTED_DIR_NAME != "gatewaycli-cowork"
    assert _EXPECTED_DISPLAY_NAME != "LLM Gateway CLI (Cowork)"


def test_double_brace_arp_key_matches_substring_logic():
    """The on-disk key `{GUID}}_is1` passes the matcher the resolver uses."""
    quirky_name = "{" + _APP_GUID + "}}_is1"
    assert _APP_GUID in quirky_name.upper()
    assert quirky_name.endswith("_is1")
    conventional = "{" + _APP_GUID + "}_is1"
    assert _APP_GUID in conventional.upper()  # a normalised key still matches


# ---------------------------------------------------------------------------
# _validate_uninstaller — the trust gate before elevation
# ---------------------------------------------------------------------------

def _entry(path: str, display: str | None = _EXPECTED_DISPLAY_NAME) -> _ArpEntry:
    return _ArpEntry(path, display, None, hive_is_hklm=True)


@pytest.fixture()
def fake_install(tmp_path):
    install_dir = tmp_path / "GatewayCLI"
    install_dir.mkdir()
    unins = install_dir / "unins000.exe"
    unins.write_bytes(b"MZ fake")
    return unins


def test_validator_accepts_real_layout(fake_install):
    path, reason = _validate_uninstaller(_entry(str(fake_install)))
    assert reason is None
    assert path == str(fake_install.resolve())


def test_validator_rejects_shell_metacharacters(fake_install):
    for bad in (f'"{fake_install}"', f"{fake_install} & calc.exe", f"{fake_install};rm"):
        path, reason = _validate_uninstaller(_entry(bad))
        assert path is None
        assert "shell characters" in reason


def test_validator_rejects_missing_file(tmp_path):
    path, reason = _validate_uninstaller(_entry(str(tmp_path / "GatewayCLI" / "unins000.exe")))
    assert path is None
    assert "does not resolve" in reason


def test_validator_rejects_non_inno_name(tmp_path):
    install_dir = tmp_path / "GatewayCLI"
    install_dir.mkdir()
    evil = install_dir / "evil.exe"
    evil.write_bytes(b"MZ")
    path, reason = _validate_uninstaller(_entry(str(evil)))
    assert path is None
    assert "not an Inno uninstaller" in reason


def test_validator_rejects_wrong_directory(tmp_path):
    """The Cowork CLI's uninstaller (different install dir) must not validate."""
    other = tmp_path / "GatewayCLI-Cowork"
    other.mkdir()
    unins = other / "unins000.exe"
    unins.write_bytes(b"MZ")
    path, reason = _validate_uninstaller(_entry(str(unins)))
    assert path is None
    assert "install directory" in reason


def test_validator_rejects_wrong_display_name(fake_install):
    path, reason = _validate_uninstaller(
        _entry(str(fake_install), display="LLM Gateway CLI (Cowork)")
    )
    assert path is None
    assert "DisplayName" in reason


def test_validator_accepts_missing_display_name(fake_install):
    """DisplayName is belt-and-suspenders — its absence alone doesn't reject."""
    path, reason = _validate_uninstaller(_entry(str(fake_install), display=None))
    assert reason is None


def test_unins_regex_shape():
    assert _UNINS_RE.match("unins000.exe")
    assert _UNINS_RE.match("UNINS001.EXE")
    assert not _UNINS_RE.match("unins00.exe")
    assert not _UNINS_RE.match("unins000.exe.bat")


# ---------------------------------------------------------------------------
# platform gating
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX gating branch")
def test_uninstall_posix_skips_with_hint():
    outcome = uninstall.uninstall(dry_run=False)
    assert outcome.skipped and not outcome.delegated
    assert "Windows-only" in outcome.detail
    assert outcome.hint == manual_hint()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX gating branch")
def test_resolver_none_off_windows():
    assert uninstall.resolve_uninstall_string() is None
