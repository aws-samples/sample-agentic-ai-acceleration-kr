# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""The single value-resolution path for catalogued config fields.

Today each family (gateway/OIDC in ``site_defaults``, the CA bundle, the model
roster, the OTEL block in ``setup``) resolves its own values with its own ad-hoc
precedence walk (finding: *fragmentation*). This module replaces those with one
:func:`resolve` that walks a field's sources in a single, declared precedence and
returns the winning value plus *which* source won (for ``config --explain``).

Precedence (highest first)::

    explicit CLI flag                       # field.flag, user-set
    GATEWAY_CLI_* env override              # field.env_override
    site-extra   — only if site_extra_overridable   (the OTEL_* exception)
    derived value                           # field.derive → resolvers.RESOLVERS
    literal / baked default                 # field.literal_default / field.baked_from
    site-extra   — if NOT site_extra_overridable    (owned keys: gateway-cli wins;
                                              site-extra only fills what we don't set)

``site_extra_overridable`` moves site-extra above the gateway-cli-computed value:
for ``OTEL_*`` a site may pin its own collector, so its site-extra value beats our
derived/default one; for owned keys (e.g. ``NO_PROXY``) gateway-cli wins and
site-extra can only fill a key we never set (e.g. the forward-proxy passthrough).

This module handles ``Compose.REPLACE`` (first source wins outright). The
``MERGE_ATTRS`` / ``MERGE_OBJECT`` composition — notably the ``user.id``
authoritative overwrite (R1) — is layered on in :mod:`resolve` T4.3.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

from cli import codecs, resolvers, validators
from cli.manifest import Compose, ConfigField


class Source(enum.Enum):
    """Which precedence layer supplied a resolved value."""

    FLAG = "flag"
    ENV_OVERRIDE = "env-override"
    SITE_EXTRA = "site-extra"
    DERIVE = "derived"
    DEFAULT = "default"
    MERGE = "merge"  # composite: site base + authoritative-key overwrite / deep-merge


@dataclass(frozen=True)
class Sources:
    """The raw source layers :func:`resolve` reads, each keyed by field ``key``.

    ``flags`` are user-set CLI values (the orchestration layer maps Click options
    to field keys); ``env`` is the process environment (read for
    ``field.env_override``); ``site_extra`` is the merged site-extra value for the
    field's tier; ``derive_ctx`` carries the deriver inputs (base OTEL endpoint,
    pre-resolved identity).
    """

    flags: Mapping[str, str] = dc_field(default_factory=dict)
    env: Mapping[str, str] = dc_field(default_factory=dict)
    # site-extra values are strings for scalar/ATTR_MAP fields and already-typed
    # dicts for JSON_OBJECT (MERGE_OBJECT) fields — hence Any.
    site_extra: Mapping[str, Any] = dc_field(default_factory=dict)
    derive_ctx: resolvers.ResolveContext = dc_field(default_factory=resolvers.ResolveContext)


@dataclass(frozen=True)
class Resolved:
    """The outcome of resolving one field."""

    field: ConfigField
    value: Any  # typed value (post-codec parse); None when nothing resolved
    raw: str | None  # the winning raw string, pre-parse; None when unresolved
    source: Source | None  # which layer won; None when unresolved


def _nonblank(value: Any) -> str | None:
    """Return a str value stripped of blanks, or None if empty/non-str.

    The REPLACE path only ever reads string sources; a non-str site-extra value
    (a JSON_OBJECT dict) belongs to the merge path, so it is not a REPLACE source.
    """
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _default_raw(fld: ConfigField) -> str | None:
    """The field's literal default, or its build-time-baked constant value.

    ``baked_from`` names the build-injected attribute (see ``site_defaults._baked``).
    Those constants live in two modules with two naming shapes: ``managed`` exposes
    the bare name (``NO_PROXY_VALUE``); ``site_defaults`` exposes it ``DEFAULT_``-
    prefixed (``GATEWAY_URL`` → ``DEFAULT_GATEWAY_URL``, ``CA_BUNDLE`` →
    ``DEFAULT_CA_BUNDLE``). We check both so resolve() is self-sufficient for baked
    defaults. Imported lazily to keep this module free of a hard writer dependency.
    """
    if fld.literal_default is not None:
        return fld.literal_default
    if fld.baked_from:
        from cli import managed, site_defaults

        baked = getattr(managed, fld.baked_from, None) or getattr(
            site_defaults, f"DEFAULT_{fld.baked_from}", None
        )
        if baked:
            return str(baked)
    return None


def _raw_by_source(fld: ConfigField, src: Source, sources: Sources) -> str | None:
    """Fetch the raw string a single source offers for ``fld`` (None if it has none)."""
    if src is Source.FLAG:
        return _nonblank(sources.flags.get(fld.key)) if fld.flag else None
    if src is Source.ENV_OVERRIDE:
        return _nonblank(sources.env.get(fld.env_override)) if fld.env_override else None
    if src is Source.SITE_EXTRA:
        return _nonblank(sources.site_extra.get(fld.key))
    if src is Source.DERIVE:
        if not fld.derive:
            return None
        deriver = resolvers.resolver_for(fld.derive)
        if deriver is None:
            return None
        return _nonblank(deriver(fld, sources.derive_ctx))
    if src is Source.DEFAULT:
        return _nonblank(_default_raw(fld))
    return None


def _precedence(fld: ConfigField) -> tuple[Source, ...]:
    """The ordered source list for a field (site-extra position depends on ownership)."""
    order: list[Source] = [Source.FLAG, Source.ENV_OVERRIDE]
    if fld.site_extra_overridable:
        order.append(Source.SITE_EXTRA)  # OTEL_* exception: site pin beats our value
    order += [Source.DERIVE, Source.DEFAULT]
    if not fld.site_extra_overridable:
        order.append(Source.SITE_EXTRA)  # owned: only fills a key we never set
    return tuple(order)


def resolve(fld: ConfigField, sources: Sources) -> Resolved:
    """Resolve ``fld`` by walking its sources in precedence; first non-blank wins.

    The winning raw string is parsed by the field's codec and validated. Returns
    a :class:`Resolved` with ``value``/``raw``/``source`` all None when no source
    supplied a value (the field is simply not emitted).
    """
    if fld.compose is not Compose.REPLACE:
        # MERGE_ATTRS / MERGE_OBJECT are handled by resolve_merge (T4.3); guard so
        # a merge field never silently resolves as a plain REPLACE.
        raise ValueError(
            f"{fld.key}: compose={fld.compose.name} needs the merge path, not resolve()"
        )

    codec = codecs.codec_for(fld.value_kind)
    for src in _precedence(fld):
        raw = _raw_by_source(fld, src, sources)
        if raw is None:
            continue
        validators.validate(fld, raw)
        return Resolved(field=fld, value=codec.parse(raw), raw=raw, source=src)
    return Resolved(field=fld, value=None, raw=None, source=None)


def _site_base(fld: ConfigField, sources: Sources) -> tuple[Any, bool]:
    """The site-extra seed for a merge field, parsed, plus whether it was present.

    A string seed (ATTR_MAP) is parsed via the codec; an already-typed dict
    (JSON_OBJECT) is used verbatim. Returns (base, present).
    """
    seed = sources.site_extra.get(fld.key)
    if seed is None or (isinstance(seed, str) and not seed.strip()):
        return {}, False
    if isinstance(seed, str):
        return codecs.codec_for(fld.value_kind).parse(seed), True
    return dict(seed), True


def resolve_merge(fld: ConfigField, sources: Sources, *, create_if_absent: bool = True) -> Resolved:
    """Resolve a composed (MERGE_ATTRS / MERGE_OBJECT) field.

    ``MERGE_ATTRS`` (``OTEL_RESOURCE_ATTRIBUTES``): start from the site-extra
    attribute map (static attrs like ``service.name`` a site pins), then overwrite
    every ``authoritative_keys`` entry (``user.id``) with the value from the
    field's deriver — the authenticated identity (R1 / Fix 1). This preserves the
    site's attrs while guaranteeing ``user.id`` is never a stale/baked value.

    ``create_if_absent`` encodes the tier distinction (AR-F2):

    * managed tier (``True``) — the authoritative key is injected even when the
      site did not seed the map, matching ``build_gateway_env``
      (``set_resource_user_id(None, user_id)``); and
    * user tier (``False``) — the key is overwritten ONLY when the site already
      seeded ``OTEL_RESOURCE_ATTRIBUTES`` at that tier, and the field is not
      emitted at all when unseeded (setup.py:559-565: "don't inject telemetry
      attrs the site did not opt into at the user tier").

    ``MERGE_OBJECT`` (``permissions``): deep-merge the site object over the base
    default (no authoritative keys). Returns value=None when nothing composed, so
    the field is simply not emitted.
    """
    codec = codecs.codec_for(fld.value_kind)
    base, present = _site_base(fld, sources)

    if fld.compose is Compose.MERGE_ATTRS:
        deriver = resolvers.resolver_for(fld.derive) if fld.derive else None
        for key in fld.authoritative_keys:
            derived = deriver(fld, sources.derive_ctx) if deriver else None
            if derived and (present or create_if_absent):
                base[key] = derived  # replace in place (order kept) / append if new
        # User tier, unseeded ⇒ never injected. Empty map ⇒ nothing to emit.
        if (not create_if_absent and not present) or not base:
            return Resolved(field=fld, value=None, raw=None, source=None)
        return Resolved(
            field=fld, value=base, raw=codec.encode(base, fld.placement), source=Source.MERGE
        )

    if fld.compose is Compose.MERGE_OBJECT:
        default = _default_raw(fld)
        merged = codec.parse(default) if default else {}
        if present:
            from cli.site_extra import deep_merge

            merged = deep_merge(merged, base)
        if not merged:
            return Resolved(field=fld, value=None, raw=None, source=None)
        return Resolved(
            field=fld, value=merged, raw=codec.encode(merged, fld.placement), source=Source.MERGE
        )

    raise ValueError(f"{fld.key}: compose={fld.compose.name} is not a merge kind")
