# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""``config --explain``: show each catalogued field's resolved value + winning source.

A support/diagnostic view over the single resolve path. For every field gateway-cli
writes or passes through (OWNED / PASSTHROUGH), it walks the SAME
:func:`cli.resolve.resolve` precedence ``setup`` uses and reports:

* the **winning source** — flag / env-override / site-extra / derived / default /
  merge, or ``unset`` when nothing resolved; and
* the resolved **value**.

Credential-bearing fields (:func:`cli.manifest.sensitive_keys`, finding R4) print
their source but **mask the value** — a token planted in
``OTEL_EXPORTER_OTLP_HEADERS`` (or ``ANTHROPIC_CUSTOM_HEADERS``) must never appear
in the output or logs (verified by the T5.5 redaction golden). The mask string is a
fixed placeholder that shares no substring with any real secret.

This module is pure: :func:`explain` takes a :class:`~cli.resolve.Sources` and
returns structured rows; :func:`render` formats them. The command layer
(``cli.main``) builds the ``Sources`` from the live environment/site-extra.
"""

from __future__ import annotations

from dataclasses import dataclass

from cli import manifest
from cli.manifest import Compose, ConfigField, Status
from cli.resolve import Resolved, Sources, resolve, resolve_merge

#: What a sensitive field's value is replaced with. Deliberately contains no part
#: of any real secret so a grep of the output for a planted token finds nothing.
REDACTED = "<redacted>"

#: Shown when no source supplied a value for a field.
UNSET = "unset"


@dataclass(frozen=True)
class Explained:
    """One field's resolution outcome, ready to render (value already masked)."""

    key: str
    category: str
    status: str
    source: str  # winning Source.value, or UNSET
    display: str  # resolved value, REDACTED for sensitive fields, "-" when unset
    sensitive: bool


def _resolve_one(field: ConfigField, sources: Sources) -> Resolved:
    """Resolve a field via the right path for its compose kind.

    REPLACE fields go through :func:`resolve`; MERGE_ATTRS / MERGE_OBJECT through
    :func:`resolve_merge`. The explain view uses managed-tier semantics
    (``create_if_absent=True``) so a mergeable field (OTEL_RESOURCE_ATTRIBUTES,
    permissions) reports the value it would take at the tier gateway-cli owns.
    """
    if field.compose is Compose.REPLACE:
        return resolve(field, sources)
    return resolve_merge(field, sources, create_if_absent=True)


def explain(sources: Sources, fields: tuple[ConfigField, ...] | None = None) -> list[Explained]:
    """Resolve each field in ``fields`` (default: OWNED + PASSTHROUGH) via ``sources``.

    Sensitive fields keep their winning source but have the value replaced with
    :data:`REDACTED`. A field that raises during resolution (e.g. a malformed
    value fails validation) is reported with source ``"error"`` rather than
    aborting the whole view.
    """
    catalog = fields if fields is not None else (
        manifest.by_status(Status.OWNED) + manifest.by_status(Status.PASSTHROUGH)
    )
    rows: list[Explained] = []
    for field in catalog:
        try:
            resolved = _resolve_one(field, sources)
        except Exception as exc:  # noqa: BLE001 — a bad value must not blank the report
            rows.append(_row(field, "error", REDACTED if field.sensitive else str(exc)))
            continue
        if resolved.source is None:
            rows.append(_row(field, UNSET, "-"))
        else:
            display = REDACTED if field.sensitive else str(resolved.raw)
            rows.append(_row(field, resolved.source.value, display))
    return rows


def _row(field: ConfigField, source: str, display: str) -> Explained:
    return Explained(
        key=field.key,
        category=field.category.value,
        status=field.status.value,
        source=source,
        display=display,
        sensitive=field.sensitive,
    )


def render(rows: list[Explained]) -> str:
    """Format explain rows as an aligned ``KEY  SOURCE  VALUE`` table."""
    if not rows:
        return "(no catalogued fields)"
    key_w = max(len(r.key) for r in rows)
    src_w = max(len(r.source) for r in rows)
    header = f"{'KEY'.ljust(key_w)}  {'SOURCE'.ljust(src_w)}  VALUE"
    lines = [header, "-" * len(header)]
    for r in rows:
        lines.append(f"{r.key.ljust(key_w)}  {r.source.ljust(src_w)}  {r.display}")
    return "\n".join(lines)
