# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Drift guard: reconcile.CONFLICTS must stay in sync with manifest BYPASS.

The manifest (``cli.manifest``) is the declarative catalog of which config keys
are gateway-bypassing (``Status.BYPASS``). ``cli.reconcile`` owns the operational
policy for stripping/reporting them. These two lists live in different modules
by design (the manifest is metadata-free; reconcile carries localized reasons,
fatal/alarm rules and sweep layers), so this test enforces the contract that
they can never silently diverge.
"""

from __future__ import annotations

from cli import manifest, reconcile


def test_manifest_has_bypass_keys() -> None:
    """Guard against the cross-check passing vacuously if the catalog empties."""
    assert set(manifest.bypass_keys()), "manifest declares no BYPASS keys"


def test_reconcile_covers_every_manifest_bypass_key() -> None:
    """Every gateway-bypassing key in the manifest has a reconcile Conflict."""
    missing, _ = reconcile.manifest_coverage_gaps()
    assert missing == [], (
        "manifest BYPASS keys with no reconcile Conflict (setup would fail to "
        f"strip them): {missing}"
    )


def test_reconcile_has_no_unexpected_conflicts() -> None:
    """No reconcile Conflict is a non-bypass key unless it's a documented strip-only."""
    _, unexpected = reconcile.manifest_coverage_gaps()
    assert unexpected == [], (
        "reconcile Conflicts that are neither manifest BYPASS nor listed in "
        f"MANIFEST_STRIP_ONLY_KEYS: {unexpected}"
    )


def test_strip_only_keys_are_owned_not_bypass() -> None:
    """The documented strip-only exceptions must be genuinely OWNED in the manifest.

    Protects the exemption from being abused to hide a real bypass key: a
    strip-only key must exist in the catalog and must NOT be a BYPASS key.
    """
    bypass = set(manifest.bypass_keys())
    for key in reconcile.MANIFEST_STRIP_ONLY_KEYS:
        field = manifest.by_key(key)
        assert field is not None, f"strip-only key {key!r} is not in the manifest catalog"
        assert key not in bypass, f"strip-only key {key!r} is also a manifest BYPASS key"
