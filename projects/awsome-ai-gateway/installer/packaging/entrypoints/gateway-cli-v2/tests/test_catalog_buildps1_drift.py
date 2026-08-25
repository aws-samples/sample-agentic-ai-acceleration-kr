# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Drift guard: manifest ``baked_from`` ↔ build.ps1 baked inputs stay in sync.

Environment-specific corporate values are injected at build time: ``build.ps1``
writes ``cli/_site_config.py`` via a series of ``Add-SiteValue "<NAME>" $Param``
calls, and the code reads them back through ``site_defaults._baked("<NAME>", …)``.
The manifest records which fields are build-injected via ``baked_from``.

These three checks enforce that the chain never develops a hole:

* every manifest ``baked_from`` name is emitted by build.ps1 (else a catalogued
  field claims a build input that does not exist), and
* every ``_baked(...)`` name the *code* reads is emitted by build.ps1 (else the
  generated ``_site_config.py`` cannot cover it and the value silently falls back
  to its literal default on every build), and
* every build.ps1 baked key is actually consumed by the code (else a dead build
  knob that bakes a value nothing reads).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from cli import manifest

_PKG_ROOT = Path(__file__).resolve().parents[1]  # …/entrypoints/gateway-cli-v2
_CLI_DIR = _PKG_ROOT / "src" / "cli"


def _find_build_ps1() -> Path | None:
    """Locate packaging/build.ps1 by walking up from the package root."""
    for parent in _PKG_ROOT.parents:
        candidate = parent / "build.ps1"
        if candidate.is_file():
            return candidate
    return None


_BUILD_PS1 = _find_build_ps1()


def _buildps1_baked_keys() -> set[str]:
    """The _site_config.py keys build.ps1 emits (Add-SiteValue "<NAME>" …)."""
    assert _BUILD_PS1 is not None
    text = _BUILD_PS1.read_text(encoding="utf-8")
    return set(re.findall(r'Add-SiteValue\s+"([A-Z_][A-Z0-9_]*)"', text))


def _code_baked_keys() -> set[str]:
    """Every name the code reads via site_defaults._baked("<NAME>", …)."""
    keys: set[str] = set()
    for name in ("site_defaults.py", "managed.py"):
        text = (_CLI_DIR / name).read_text(encoding="utf-8")
        keys.update(re.findall(r'_baked\(\s*"([A-Z_][A-Z0-9_]*)"', text))
    return keys


@pytest.mark.skipif(_BUILD_PS1 is None, reason="build.ps1 not present in this checkout")
def test_manifest_baked_from_has_buildps1_input() -> None:
    """Every field with baked_from maps to a build.ps1 Add-SiteValue key."""
    baked = _buildps1_baked_keys()
    assert baked, "parsed no Add-SiteValue keys from build.ps1 — regex/format drift?"
    manifest_baked = {f.baked_from for f in manifest.FIELDS if f.baked_from}
    missing = manifest_baked - baked
    assert not missing, (
        f"manifest baked_from names with no build.ps1 input: {sorted(missing)} "
        f"(build.ps1 bakes {sorted(baked)})"
    )


@pytest.mark.skipif(_BUILD_PS1 is None, reason="build.ps1 not present in this checkout")
def test_code_baked_keys_have_buildps1_input() -> None:
    """Every _baked() name the code reads is emitted into _site_config.py."""
    baked = _buildps1_baked_keys()
    code = _code_baked_keys()
    assert code, "parsed no _baked() names from source — regex/format drift?"
    missing = code - baked
    assert not missing, (
        f"code reads _baked names build.ps1 never emits: {sorted(missing)} — "
        f"_site_config.py cannot cover them"
    )


@pytest.mark.skipif(_BUILD_PS1 is None, reason="build.ps1 not present in this checkout")
def test_buildps1_keys_are_consumed_by_code() -> None:
    """Every build.ps1 baked key is read somewhere (no dead build knob)."""
    baked = _buildps1_baked_keys()
    code = _code_baked_keys()
    unused = baked - code
    assert not unused, (
        f"build.ps1 bakes keys nothing reads via _baked(): {sorted(unused)}"
    )
