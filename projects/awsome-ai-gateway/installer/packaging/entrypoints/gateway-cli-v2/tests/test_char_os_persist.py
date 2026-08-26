# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Characterization: pin the OS-persisted operator-var set and its written values.

Golden snapshot taken BEFORE the Phase 3-5 rewrite (TC.3, gate for P3-P5).
``setup`` and ``env --persist`` register a fixed set of operator vars as permanent
User-scope OS environment variables (Windows HKCU / shell rc). WHICH keys is
single-sourced from the manifest (``os_persisted_keys()``); the VALUES come from
the resolved config via ``setup._os_env_to_persist``. This snapshot freezes both
— the exact key set + order, that ``cli.env`` reads the same set, and the
value mapping (including blank-drop) — so the rewrite cannot silently change what
lands in a user's shell profile / registry.
"""

from __future__ import annotations

from cli import env as env_mod
from cli import manifest
from cli import setup as setup_mod

#: The frozen operator-var set, in catalog declaration order.
_EXPECTED_KEYS: tuple[str, ...] = (
    "ANTHROPIC_BASE_URL",
    "ADMIN_API_URL",
    "OIDC_ISSUER_URL",
    "OIDC_CLIENT_ID",
)


def test_os_persisted_keys_are_pinned() -> None:
    """The manifest's OS-persist set and its order are frozen."""
    assert manifest.os_persisted_keys() == _EXPECTED_KEYS


def test_env_operator_vars_match_manifest() -> None:
    """``cli.env`` sources the identical set (no drift between reader and writer)."""
    assert tuple(env_mod._OPERATOR_VARS) == _EXPECTED_KEYS


def test_persisted_values_for_representative_config() -> None:
    """The value mapping is pinned — exact dict AND insertion order."""
    persisted = setup_mod._os_env_to_persist(
        gateway_url="https://gw.example.com",
        admin_api_url="https://admin.example.com",
        issuer_url="https://issuer.example.com",
        client_id="client-abc",
    )
    expected = {
        "ANTHROPIC_BASE_URL": "https://gw.example.com",
        "ADMIN_API_URL": "https://admin.example.com",
        "OIDC_ISSUER_URL": "https://issuer.example.com",
        "OIDC_CLIENT_ID": "client-abc",
    }
    assert persisted == expected
    assert list(persisted.keys()) == list(_EXPECTED_KEYS)


def test_blank_values_are_dropped() -> None:
    """A blank/whitespace value is omitted rather than persisted as empty."""
    persisted = setup_mod._os_env_to_persist(
        gateway_url="https://gw.example.com",
        admin_api_url="   ",   # whitespace ⇒ dropped
        issuer_url="",         # blank ⇒ dropped
        client_id="client-abc",
    )
    assert persisted == {
        "ANTHROPIC_BASE_URL": "https://gw.example.com",
        "OIDC_CLIENT_ID": "client-abc",
    }
