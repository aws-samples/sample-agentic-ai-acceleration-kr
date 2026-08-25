# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Characterization: pin the exact managed env dict ``build_gateway_env`` emits.

This is a **golden** snapshot taken BEFORE the Phase 3-5 resolve/emit rewrite
(TC.1, gate for P3-P5). ``managed.build_gateway_env`` is the authoritative writer
for gateway-cli's managed-tier env block; the later rewrite (T5.2) must reproduce
this dict byte-for-byte. Each case locks a specific behaviour the refactor could
silently change:

* the full env with every optional input supplied — exact keys, values, ORDER;
* the conditional gating — OTEL/CA/auth-token/user.id keys appear only when their
  input is supplied, and ``user.id`` rides in the OTEL block (never emitted
  without an endpoint); and
* per-signal endpoint derivation, including trailing-slash normalisation.

``NO_PROXY`` is a baked, environment-injected constant (build.ps1 → _site_config,
guarded separately by test_catalog_buildps1_drift). We source it from
``managed.NO_PROXY_VALUE`` here rather than hardcoding a literal, so this golden
pins the *derivation and shape* without coupling to a particular site's bypass
list.
"""

from __future__ import annotations

from cli import managed

_GW = "https://gw.example.com"
_ADMIN = "https://admin.example.com"
_OTEL = "http://collector.example.com:4318"
_TOKEN = "secret-token"
_USER = "alice@corp"
_CA = "/etc/ssl/corp.pem"


def test_full_managed_env_is_pinned() -> None:
    """Every optional input supplied — exact dict AND insertion order are frozen."""
    env = managed.build_gateway_env(
        gateway_url=_GW,
        admin_api_url=_ADMIN,
        otel_endpoint=_OTEL,
        otel_auth_token=_TOKEN,
        user_id=_USER,
        ca_bundle=_CA,
    )

    expected = {
        "ANTHROPIC_BASE_URL": _GW,
        "GATEWAY_CLI_GATEWAY_URL": _ADMIN,
        "NODE_EXTRA_CA_CERTS": _CA,
        "REQUESTS_CA_BUNDLE": _CA,
        "AWS_CA_BUNDLE": _CA,
        "SSL_CERT_FILE": _CA,
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_TRACES_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_EXPORTER_OTLP_ENDPOINT": _OTEL,
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": f"{_OTEL}/v1/logs",
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT": f"{_OTEL}/v1/metrics",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": f"{_OTEL}/v1/traces",
        "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE": "cumulative",
        "OTEL_LOG_USER_PROMPTS": "1",
        "OTEL_LOG_TOOL_DETAILS": "1",
        "OTEL_LOG_TOOL_CONTENT": "1",
        "OTEL_METRICS_INCLUDE_VERSION": "true",
        "OTEL_METRICS_INCLUDE_ENTRYPOINT": "true",
        "OTEL_RESOURCE_ATTRIBUTES": f"user.id={_USER}",
        "OTEL_EXPORTER_OTLP_HEADERS": f"Authorization=Bearer {_TOKEN}",
    }
    # NO_PROXY is emitted last, but only when the site build baked a bypass list.
    # A bare build leaves NO_PROXY_VALUE blank ⇒ put() drops the empty CSV and the
    # key is absent (see managed.build_gateway_env / NO_PROXY_VALUE). Guarding on
    # the baked value keeps this golden valid for both bare and site builds.
    if managed.NO_PROXY_VALUE.strip():
        expected["NO_PROXY"] = managed.NO_PROXY_VALUE

    assert env == expected
    # Order is part of the contract — the emitted JSON must be byte-stable.
    assert list(env.keys()) == list(expected.keys())


def test_minimal_managed_env_is_pinned() -> None:
    """No optional inputs — only the two routing keys (plus NO_PROXY if baked)."""
    env = managed.build_gateway_env(
        gateway_url=_GW,
        admin_api_url=_ADMIN,
        otel_endpoint=None,
        otel_auth_token=None,
        user_id=None,
        ca_bundle=None,
    )
    expected = {
        "ANTHROPIC_BASE_URL": _GW,
        "GATEWAY_CLI_GATEWAY_URL": _ADMIN,
    }
    # NO_PROXY rides along only when the site build baked a bypass list; a bare
    # build (blank NO_PROXY_VALUE) emits just the two routing keys.
    if managed.NO_PROXY_VALUE.strip():
        expected["NO_PROXY"] = managed.NO_PROXY_VALUE
    assert env == expected
    assert list(env.keys()) == list(expected.keys())


def test_user_id_without_endpoint_is_not_emitted() -> None:
    """``user.id`` rides inside the OTEL block: no endpoint ⇒ no resource attrs."""
    env = managed.build_gateway_env(
        gateway_url=_GW,
        admin_api_url=_ADMIN,
        otel_endpoint=None,
        otel_auth_token=_TOKEN,
        user_id=_USER,
        ca_bundle=None,
    )
    assert "OTEL_RESOURCE_ATTRIBUTES" not in env
    assert "OTEL_EXPORTER_OTLP_HEADERS" not in env  # auth token also gated on endpoint
    assert not any(k.startswith("OTEL_") for k in env)


def test_otel_without_token_or_user_omits_those_keys() -> None:
    """Endpoint present but no token/user_id ⇒ headers + resource attrs absent."""
    env = managed.build_gateway_env(
        gateway_url=_GW,
        admin_api_url=_ADMIN,
        otel_endpoint=_OTEL,
        otel_auth_token=None,
        user_id=None,
        ca_bundle=None,
    )
    assert "OTEL_EXPORTER_OTLP_HEADERS" not in env
    assert "OTEL_RESOURCE_ATTRIBUTES" not in env
    # The unconditional OTEL keys still land.
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == _OTEL
    assert env["OTEL_METRICS_EXPORTER"] == "otlp"


def test_endpoint_trailing_slash_is_normalised() -> None:
    """A trailing slash on the base is stripped before per-signal derivation."""
    env = managed.build_gateway_env(
        gateway_url=_GW,
        admin_api_url=_ADMIN,
        otel_endpoint=_OTEL + "/",
        otel_auth_token=None,
        user_id=None,
        ca_bundle=None,
    )
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == _OTEL
    assert env["OTEL_EXPORTER_OTLP_LOGS_ENDPOINT"] == f"{_OTEL}/v1/logs"
    assert env["OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"] == f"{_OTEL}/v1/metrics"
    assert env["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] == f"{_OTEL}/v1/traces"
