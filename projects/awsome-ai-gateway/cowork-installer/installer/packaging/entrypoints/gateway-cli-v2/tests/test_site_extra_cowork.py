# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Unit tests for the Cowork site-extra flat-map merge (Layer B).

Covers the contract the guide + code guide describe:
  * only real 3P managed-config keys are accepted (allowlist);
  * gateway-cli-owned routing/credential keys can never be overridden;
  * accepted values serialize per the 3P write-format spec on Windows
    (strings) and stay native on macOS;
  * the snapshot key set widens to include accepted keys so a later
    ``disable`` / rollback reverts them instead of orphaning them;
  * core gateway-cli keys always win over a colliding site-extra value.
"""

from __future__ import annotations

import json

import pytest

from cli import cowork_config


@pytest.fixture
def write_site_extra(tmp_path, monkeypatch):
    """Point GATEWAY_CLI_SITE_EXTRA at a temp file holding the given document."""

    def _write(doc) -> None:
        path = tmp_path / "site_extra.json"
        path.write_text(
            doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8"
        )
        monkeypatch.setenv("GATEWAY_CLI_SITE_EXTRA", str(path))

    return _write


# --- resolve_site_extra: allowlist acceptance / rejection --------------------

def test_no_site_extra_is_noop(monkeypatch):
    monkeypatch.delenv("GATEWAY_CLI_SITE_EXTRA", raising=False)
    accepted, warnings = cowork_config.resolve_site_extra()
    assert accepted == {}
    assert warnings == []


def test_allowlisted_keys_accepted(write_site_extra):
    write_site_extra({
        "inferenceCustomHeaders": {"X-Tenant-Id": "acme"},
        "disableAutoUpdates": True,
    })
    accepted, warnings = cowork_config.resolve_site_extra()
    assert accepted == {
        "inferenceCustomHeaders": {"X-Tenant-Id": "acme"},
        "disableAutoUpdates": True,
    }
    assert warnings == []


def test_core_owned_key_rejected_with_warning(write_site_extra):
    # A build must not be able to hijack gateway routing via site-extra.
    write_site_extra({"inferenceGatewayBaseUrl": "https://evil.example.com"})
    accepted, warnings = cowork_config.resolve_site_extra()
    assert accepted == {}
    assert len(warnings) == 1
    assert "inferenceGatewayBaseUrl" in warnings[0]
    assert "owned by gateway-cli" in warnings[0]


def test_org_uuid_is_core_owned(write_site_extra):
    # deploymentOrganizationUuid is computed from the baked ORG_UUID, not injected.
    write_site_extra({"deploymentOrganizationUuid": "11111111-2222-3333-4444-555555555555"})
    accepted, warnings = cowork_config.resolve_site_extra()
    assert accepted == {}
    assert warnings and "deploymentOrganizationUuid" in warnings[0]


def test_claude_code_only_keys_rejected(write_site_extra):
    # The legacy managed/user/env/permissions schema is meaningless to the app.
    write_site_extra({
        "env": {"HTTPS_PROXY": "http://proxy.example.com:8080"},
        "permissions": {"allow": ["Bash(git*)"]},
        "managed": {"env": {}},
    })
    accepted, warnings = cowork_config.resolve_site_extra()
    assert accepted == {}
    assert len(warnings) == 3
    assert all("not a recognized" in w for w in warnings)


def test_mixed_accept_and_reject(write_site_extra):
    write_site_extra({
        "otlpEndpoint": "https://otel.example.com",   # accepted
        "inferenceProvider": "something",             # core-owned -> rejected
        "bogusKey": 1,                                 # unknown -> rejected
    })
    accepted, warnings = cowork_config.resolve_site_extra()
    assert accepted == {"otlpEndpoint": "https://otel.example.com"}
    assert len(warnings) == 2


# --- value serialization -----------------------------------------------------

def test_windows_serialization_is_strings():
    assert cowork_config._extra_value_for_store(True, as_string=True) == "true"
    assert cowork_config._extra_value_for_store(False, as_string=True) == "false"
    assert cowork_config._extra_value_for_store(3600, as_string=True) == "3600"
    assert (
        cowork_config._extra_value_for_store({"a": 1}, as_string=True) == '{"a":1}'
    )
    assert (
        cowork_config._extra_value_for_store(["x", "y"], as_string=True) == '["x","y"]'
    )


def test_macos_serialization_is_native():
    assert cowork_config._extra_value_for_store(True, as_string=False) is True
    assert cowork_config._extra_value_for_store(3600, as_string=False) == 3600
    assert cowork_config._extra_value_for_store({"a": 1}, as_string=False) == {"a": 1}


# --- snapshot key set widening ----------------------------------------------

def test_all_managed_key_names_includes_accepted_extra(write_site_extra):
    write_site_extra({"otlpEndpoint": "https://otel.example.com"})
    names = cowork_config._all_managed_key_names()
    assert "otlpEndpoint" in names
    # core keys still present
    assert "inferenceGatewayBaseUrl" in names
    # no duplicates
    assert len(names) == len(set(names))


def test_all_managed_key_names_excludes_rejected(write_site_extra):
    write_site_extra({"bogusKey": 1, "inferenceProvider": "x"})
    names = cowork_config._all_managed_key_names()
    assert "bogusKey" not in names
    # inferenceProvider is core anyway, but must not appear twice
    assert names.count("inferenceProvider") == 1


# --- core keys win over site-extra in the written policy ---------------------

def _base_config():
    return cowork_config.CoworkConfig(
        base_url="https://gw.example.cloudfront.net",
        models=["cowork-opus"],
        credential_kind="helper-script",
        helper_path=r"C:\helper.exe",
    )


def test_policy_values_core_wins_over_site_extra(write_site_extra):
    # site-extra tries to set a core key; resolve strips it, and even if it
    # slipped through the update() ordering guarantees the core value wins.
    write_site_extra({"otlpEndpoint": "https://otel.example.com"})
    values = cowork_config._policy_values(_base_config(), models_as_json_string=True)
    assert values["inferenceProvider"] == "gateway"
    assert values["inferenceGatewayBaseUrl"] == "https://gw.example.cloudfront.net"
    # accepted extra key is present alongside the core keys
    assert values["otlpEndpoint"] == "https://otel.example.com"


def test_policy_values_extra_serialized_for_windows(write_site_extra):
    write_site_extra({
        "disableAutoUpdates": True,
        "inferenceCustomHeaders": {"X-Tenant-Id": "acme"},
    })
    values = cowork_config._policy_values(_base_config(), models_as_json_string=True)
    assert values["disableAutoUpdates"] == "true"
    assert values["inferenceCustomHeaders"] == '{"X-Tenant-Id":"acme"}'


def test_policy_values_extra_native_for_macos(write_site_extra):
    write_site_extra({
        "disableAutoUpdates": True,
        "inferenceCustomHeaders": {"X-Tenant-Id": "acme"},
    })
    values = cowork_config._policy_values(_base_config(), models_as_json_string=False)
    assert values["disableAutoUpdates"] is True
    assert values["inferenceCustomHeaders"] == {"X-Tenant-Id": "acme"}
