# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Named derivers that compute a field's value at execution time.

Most catalogued fields resolve from a static source (a CLI flag, site-extra, a
literal default). A handful must be *computed* at ``setup``/``build`` time from
runtime state — the collector base URL, the logged-in identity. Those fields
carry a ``derive`` name in the manifest; this module maps each name to the
function that computes it. :func:`resolve.resolve` calls
:func:`resolver_for` when a field has a ``derive`` and no higher-precedence
source won.

Two derivers today (the only ``derive`` names in the catalog):

* ``otel_from_host`` — the four ``OTEL_EXPORTER_OTLP_*_ENDPOINT`` fields derive
  from a single base endpoint (``ctx.otel_endpoint``): the base itself for
  ``OTEL_EXPORTER_OTLP_ENDPOINT``, and ``<base>/v1/{logs,metrics,traces}`` for
  the per-signal fields, matching ``managed.build_gateway_env``. A trailing
  slash on the base is stripped before derivation.
* ``user_id_from_identity`` — the telemetry ``user.id`` (R1 / Fix 1): the
  logged-in OIDC email, falling back to the AWS STS caller id, matching
  ``setup._resolve_user_id``. This is the authenticated login email the gateway
  authorizes (gateway-cli-issues.md:255-257) — never a static/stale value.

Derivers are pure given their :class:`ResolveContext`. The context carries the
inputs (base endpoint, an optionally pre-resolved identity) so the live
network/identity lookup can be injected for tests and only happens when the
context does not already carry the value.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from cli.login import resolve_login_user_id
from cli.manifest import ConfigField

#: Per-signal path suffix appended to the base OTEL endpoint, keyed by field.
#: The base endpoint field itself maps to "" (no suffix).
_OTEL_SUFFIX_BY_KEY: dict[str, str] = {
    "OTEL_EXPORTER_OTLP_ENDPOINT": "",
    "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "/v1/logs",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT": "/v1/metrics",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "/v1/traces",
}


@dataclass(frozen=True)
class ResolveContext:
    """Runtime inputs a deriver may read.

    ``otel_endpoint`` is the base collector URL (pre-normalisation). ``user_id``
    is an optional pre-resolved identity: when set, ``user_id_from_identity``
    returns it verbatim (the login step already resolved it); when None, the
    deriver performs the live OIDC→STS lookup.
    """

    otel_endpoint: str | None = None
    user_id: str | None = None


def resolve_identity() -> str | None:
    """The authenticated ``user.id``: logged-in OIDC email, else AWS STS caller id.

    Mirrors ``setup._resolve_user_id`` (setup always runs after login, so the
    OIDC email is normally present; STS is the standalone-setup fallback).
    Returns None when neither source yields an id, so the attribute is omitted.
    """
    oidc_id = resolve_login_user_id()
    if oidc_id:
        return oidc_id
    # Deferred import: the STS fallback lives in ``setup``; importing it lazily
    # keeps this module free of a resolve→setup import cycle (setup imports
    # resolve in Phase 4/5).
    from cli.setup import _resolve_aws_user_id

    return _resolve_aws_user_id()


def otel_from_host(field: ConfigField, ctx: ResolveContext) -> str | None:
    """Derive an ``OTEL_EXPORTER_OTLP_*_ENDPOINT`` from ``ctx.otel_endpoint``."""
    if not ctx.otel_endpoint:
        return None
    base = ctx.otel_endpoint.rstrip("/")
    suffix = _OTEL_SUFFIX_BY_KEY.get(field.key, "")
    return f"{base}{suffix}"


def user_id_from_identity(field: ConfigField, ctx: ResolveContext) -> str | None:
    """Derive the telemetry ``user.id`` (R1): pre-resolved identity, else live lookup."""
    if ctx.user_id:
        return ctx.user_id
    return resolve_identity()


Resolver = Callable[[ConfigField, ResolveContext], "str | None"]

#: Named derivers, keyed by ``ConfigField.derive``.
RESOLVERS: dict[str, Resolver] = {
    "otel_from_host": otel_from_host,
    "user_id_from_identity": user_id_from_identity,
}


def resolver_for(name: str) -> Resolver | None:
    """Return the deriver for ``name`` (None if the name is not registered)."""
    return RESOLVERS.get(name)
