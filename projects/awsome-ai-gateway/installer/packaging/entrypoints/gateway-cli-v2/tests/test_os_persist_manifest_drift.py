# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Drift guard: the OS-env-persist var set is single-sourced from the manifest.

``setup`` (and ``gateway-cli env --persist``) register a fixed set of operator
vars as permanent User-scope OS environment variables (shell rc / HKCU). WHICH
keys those are is declared once in the manifest (``os_persist=True``); both
consumers derive their list from :func:`manifest.os_persisted_keys`. These tests
enforce that neither consumer drifts from the manifest and that the manifest set
stays sane (non-empty, OWNED, never a gateway-bypassing key).
"""

from __future__ import annotations

from cli import env, manifest, setup


def test_manifest_has_os_persisted_keys() -> None:
    """Guard against the cross-checks passing vacuously if the set empties."""
    assert set(manifest.os_persisted_keys()), "manifest declares no os_persist keys"


def test_env_operator_vars_match_manifest() -> None:
    """cli.env._OPERATOR_VARS is exactly the manifest's os_persist set."""
    assert set(env._OPERATOR_VARS) == set(manifest.os_persisted_keys())


def test_setup_persists_exactly_the_manifest_set() -> None:
    """setup._os_env_to_persist covers every manifest key (given non-blank values)."""
    persisted = setup._os_env_to_persist(
        gateway_url="https://gw.example",
        admin_api_url="https://admin.example",
        issuer_url="https://oidc.example",
        client_id="client-abc",
    )
    assert set(persisted) == set(manifest.os_persisted_keys())


def test_setup_drops_blank_values() -> None:
    """A blank/whitespace value for a manifest key is not persisted."""
    persisted = setup._os_env_to_persist(
        gateway_url="https://gw.example",
        admin_api_url="   ",
        issuer_url="",
        client_id="client-abc",
    )
    assert "ADMIN_API_URL" not in persisted
    assert "OIDC_ISSUER_URL" not in persisted
    assert persisted["ANTHROPIC_BASE_URL"] == "https://gw.example"
    assert persisted["OIDC_CLIENT_ID"] == "client-abc"


def test_os_persisted_keys_are_owned_not_bypass() -> None:
    """Every persisted key exists in the catalog and is not gateway-bypassing.

    Persisting a bypass key as a permanent OS env var would defeat the gateway,
    so the manifest must never mark one ``os_persist=True``.
    """
    bypass = set(manifest.bypass_keys())
    for key in manifest.os_persisted_keys():
        field = manifest.by_key(key)
        assert field is not None, f"os_persist key {key!r} is not in the manifest catalog"
        assert key not in bypass, f"os_persist key {key!r} is also a manifest BYPASS key"
