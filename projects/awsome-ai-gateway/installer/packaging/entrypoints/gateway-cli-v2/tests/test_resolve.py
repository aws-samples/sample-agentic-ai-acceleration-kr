# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Tests for the unified REPLACE resolver ``cli.resolve.resolve`` (T4.2).

Pins the declared source precedence and shows that, fed inputs equivalent to
``build_gateway_env``'s args, the resolver reproduces TC.1's scalar values
(``managed.build_gateway_env`` golden). MERGE fields are deferred to T4.3 and
must be rejected by the REPLACE path.
"""

from __future__ import annotations

import pytest

from cli import managed, resolvers
from cli.manifest import by_key
from cli.resolve import Source, Sources, resolve


def _r(key: str, **kw: object) -> object:
    return resolve(by_key(key), Sources(**kw))  # type: ignore[arg-type]


def test_flag_beats_everything() -> None:
    res = _r(
        "ANTHROPIC_BASE_URL",
        flags={"ANTHROPIC_BASE_URL": "https://flag.example.com"},
        env={"": ""},
        site_extra={"ANTHROPIC_BASE_URL": "https://site.example.com"},
    )
    assert res.value == "https://flag.example.com"
    assert res.source is Source.FLAG


def test_env_override_beats_default() -> None:
    """CA field: GATEWAY_CLI_CA_BUNDLE env override wins over the baked default."""
    res = _r("NODE_EXTRA_CA_CERTS", env={"GATEWAY_CLI_CA_BUNDLE": "/env/ca.pem"})
    assert res.value == "/env/ca.pem"
    assert res.source is Source.ENV_OVERRIDE


def test_literal_default_is_last_for_owned() -> None:
    """An owned toggle with no higher source resolves to its literal default."""
    res = _r("CLAUDE_CODE_ENABLE_TELEMETRY")
    assert res.value == "1"
    assert res.source is Source.DEFAULT


def test_protocol_default_matches_tc1() -> None:
    res = _r("OTEL_EXPORTER_OTLP_PROTOCOL")
    assert res.value == "http/protobuf"  # TC.1 golden


def test_no_proxy_baked_default_matches_tc1() -> None:
    """NO_PROXY resolves to the baked managed.NO_PROXY_VALUE (TC.1 golden).

    A site build bakes a bypass list and resolves it via the CSV codec; a bare
    build leaves NO_PROXY_VALUE blank, so the default is empty and the field
    resolves to nothing (not emitted).
    """
    res = _r("NO_PROXY")
    if managed.NO_PROXY_VALUE.strip():
        assert res.value == managed.NO_PROXY_VALUE.split(",")  # CSV codec → list
        assert res.source is Source.DEFAULT
    else:
        assert res.value is None and res.source is None


def test_otel_endpoint_derived_matches_tc1() -> None:
    """OTEL endpoints derive from the base via otel_from_host (TC.1 shape)."""
    ctx = resolvers.ResolveContext(otel_endpoint="http://collector.example.com:4318/")
    base = _r("OTEL_EXPORTER_OTLP_ENDPOINT", derive_ctx=ctx)
    logs = _r("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", derive_ctx=ctx)
    assert base.value == "http://collector.example.com:4318"
    assert base.source is Source.DERIVE
    assert logs.value == "http://collector.example.com:4318/v1/logs"


def test_site_extra_overridable_beats_derive() -> None:
    """OTEL_* is the exception: a site-extra pin beats the gateway-cli value."""
    fld = by_key("OTEL_EXPORTER_OTLP_ENDPOINT")
    assert fld.site_extra_overridable
    ctx = resolvers.ResolveContext(otel_endpoint="http://auto:4318")
    res = resolve(
        fld,
        Sources(site_extra={"OTEL_EXPORTER_OTLP_ENDPOINT": "http://pinned:4318"}, derive_ctx=ctx),
    )
    assert res.value == "http://pinned:4318"
    assert res.source is Source.SITE_EXTRA


def test_owned_key_site_extra_never_beats_gateway_cli() -> None:
    """NO_PROXY is owned: a site-extra value cannot beat gateway-cli's baked value.

    This invariant only bites when gateway-cli actually bakes a value. In a bare
    build (blank NO_PROXY_VALUE) gateway-cli sets nothing, so — by design — the
    site-extra value fills the key we never set (resolve._precedence puts
    site-extra last for owned keys: "only fills what we don't set").
    """
    res = _r("NO_PROXY", site_extra={"NO_PROXY": "evil.example.com"})
    if managed.NO_PROXY_VALUE.strip():
        assert res.value == managed.NO_PROXY_VALUE.split(",")
        assert res.source is Source.DEFAULT
    else:
        assert res.value == ["evil.example.com"]
        assert res.source is Source.SITE_EXTRA


def test_unresolved_field_returns_none() -> None:
    """A user-tier flag field with no source resolves to nothing (not emitted)."""
    res = _r("ANTHROPIC_DEFAULT_OPUS_MODEL")
    assert res.value is None and res.source is None


def test_merge_field_rejected_by_replace_path() -> None:
    """OTEL_RESOURCE_ATTRIBUTES (MERGE_ATTRS) must not resolve via the REPLACE path."""
    fld = by_key("OTEL_RESOURCE_ATTRIBUTES")
    with pytest.raises(ValueError, match="merge path"):
        resolve(fld, Sources())


def test_bad_model_alias_fails_in_resolve() -> None:
    """F2: a Bedrock inference-profile id supplied via flag fails at resolve time."""
    from cli import validators

    with pytest.raises(validators.ValidationError):
        _r("ANTHROPIC_DEFAULT_OPUS_MODEL", flags={"ANTHROPIC_DEFAULT_OPUS_MODEL": "us.anthropic.claude"})
