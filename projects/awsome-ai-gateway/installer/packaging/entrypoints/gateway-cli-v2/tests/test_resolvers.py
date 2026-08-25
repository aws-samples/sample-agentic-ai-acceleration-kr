# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Unit tests for the named derivers in ``cli.resolvers`` (T4.1).

Each deriver is exercised in isolation: ``otel_from_host`` against a synthetic
context, ``user_id_from_identity`` with the identity lookups monkeypatched so no
network/OIDC/STS call happens.
"""

from __future__ import annotations

import pytest

from cli import resolvers
from cli.manifest import by_key


@pytest.mark.parametrize(
    ("key", "expected"),
    (
        ("OTEL_EXPORTER_OTLP_ENDPOINT", "http://col.example:4318"),
        ("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", "http://col.example:4318/v1/logs"),
        ("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "http://col.example:4318/v1/metrics"),
        ("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://col.example:4318/v1/traces"),
    ),
)
def test_otel_from_host_per_signal(key: str, expected: str) -> None:
    """Each signal endpoint is derived from the base, trailing slash stripped."""
    ctx = resolvers.ResolveContext(otel_endpoint="http://col.example:4318/")
    assert resolvers.otel_from_host(by_key(key), ctx) == expected


def test_otel_from_host_no_endpoint_is_none() -> None:
    """No base endpoint ⇒ nothing derived (the OTEL block is omitted entirely)."""
    ctx = resolvers.ResolveContext(otel_endpoint=None)
    assert resolvers.otel_from_host(by_key("OTEL_EXPORTER_OTLP_ENDPOINT"), ctx) is None


def test_user_id_pre_resolved_passthrough() -> None:
    """A pre-resolved identity on the context is returned verbatim (no lookup)."""
    field = by_key("OTEL_RESOURCE_ATTRIBUTES")
    ctx = resolvers.ResolveContext(user_id="alice@corp")
    assert resolvers.user_id_from_identity(field, ctx) == "alice@corp"


def test_user_id_prefers_oidc_over_sts(monkeypatch: pytest.MonkeyPatch) -> None:
    """R1: the logged-in OIDC email wins over the STS fallback."""
    monkeypatch.setattr(resolvers, "resolve_login_user_id", lambda: "oidc@corp")
    field = by_key("OTEL_RESOURCE_ATTRIBUTES")
    assert resolvers.user_id_from_identity(field, resolvers.ResolveContext()) == "oidc@corp"


def test_user_id_falls_back_to_sts(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no OIDC identity, the AWS STS caller id is used."""
    monkeypatch.setattr(resolvers, "resolve_login_user_id", lambda: None)
    import cli.setup as setup_mod

    monkeypatch.setattr(setup_mod, "_resolve_aws_user_id", lambda: "arn-user")
    field = by_key("OTEL_RESOURCE_ATTRIBUTES")
    assert resolvers.user_id_from_identity(field, resolvers.ResolveContext()) == "arn-user"


def test_user_id_none_when_no_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither source yields an id ⇒ None (attribute omitted downstream)."""
    monkeypatch.setattr(resolvers, "resolve_login_user_id", lambda: None)
    import cli.setup as setup_mod

    monkeypatch.setattr(setup_mod, "_resolve_aws_user_id", lambda: None)
    field = by_key("OTEL_RESOURCE_ATTRIBUTES")
    assert resolvers.user_id_from_identity(field, resolvers.ResolveContext()) is None
