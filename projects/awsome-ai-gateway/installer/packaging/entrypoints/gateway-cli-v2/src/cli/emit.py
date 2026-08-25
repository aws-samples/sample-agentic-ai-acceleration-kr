# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""The single emission path: encode a resolved value for each of its targets.

:func:`resolve` (and :func:`resolve_merge`) produce a field's *typed* value; this
module turns that value into concrete settings/env fragments — one per declared
:class:`~cli.manifest.Output`. The encoder is chosen by the field's
``value_kind`` and the target's ``placement`` (``codecs.codec_for(kind).encode``),
so a single emitter writes scalars, JSON arrays (``availableModels``), JSON
objects (``permissions``), command records (``statusLine``) and comma-joined
attribute maps (``OTEL_RESOURCE_ATTRIBUTES``) alike — a string-only writer could
not (finding R2). Pairing this with the catalog's ``outputs`` closes C1: the
managed env, the user block and OS-env persistence all become *"resolve + emit
every OWNED field targeting that tier"* rather than three hand-written blocks.

Conditional targets (``Output.condition`` set) are **not** emitted by default. The
only one today is ``OTEL_RESOURCE_ATTRIBUTES``'s user-tier copy
(``user_tier_seeded``): setup writes it ONLY when site-extra already seeded the
key at the user tier (setup.py:559-565, delivered Fix 1). Making the default skip
conditional targets means a consumer can never emit that key at the user tier
unconditionally (AR-F2) — a caller must opt in explicitly via ``targets``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from cli import codecs
from cli.manifest import ConfigField, Output, Placement, Tier


@dataclass(frozen=True)
class Emitted:
    """One encoded value ready to be written to a single output target.

    ``key`` is the field key (the settings key or env-var name); ``output`` is the
    ``(placement, tier, condition)`` target; ``value`` is encoded per the field's
    ``value_kind`` × ``output.placement`` (a ``str`` for env placements, a ``list``
    / ``dict`` / command-record for structured settings placements).
    """

    key: str
    output: Output
    value: Any


def emit(
    field: ConfigField,
    typed: Any,
    targets: Callable[[Output], bool] | None = None,
) -> list[Emitted]:
    """Encode ``typed`` for each of ``field``'s outputs selected by ``targets``.

    ``typed is None`` (the field resolved to nothing) ⇒ nothing is emitted, so an
    unresolved field is simply absent from the output rather than written blank.

    ``targets`` is a predicate over the field's :class:`~cli.manifest.Output`
    entries. The default emits every **unconditional** output (``condition is
    None``); a conditional target must be opted in explicitly (see the module
    docstring on AR-F2). Compose a predicate to also filter by tier/placement,
    e.g. ``lambda o: o.tier in (Tier.MANAGED, Tier.EITHER)``.
    """
    if typed is None:
        return []
    select = targets if targets is not None else _unconditional
    codec = codecs.codec_for(field.value_kind)
    return [
        Emitted(key=field.key, output=out, value=codec.encode(typed, out.placement))
        for out in field.outputs
        if select(out)
    ]


def _unconditional(output: Output) -> bool:
    """Default target filter: only outputs with no gating condition."""
    return output.condition is None


# --- Convenience target predicates -----------------------------------------
#
# Callers (T5.2 build_gateway_env, T5.3 user-settings block, OS-env persistence)
# select the slice of a field's outputs that lands in the tier/placement they own.
# ``satisfied`` names the runtime conditions the caller has established (e.g.
# ``{"user_tier_seeded"}`` when site-extra seeded the key at the user tier), so a
# conditional target is emitted only when its condition holds.


def for_settings_tier(
    tier: Tier, *, satisfied: frozenset[str] = frozenset()
) -> Callable[[Output], bool]:
    """Select settings outputs (``SETTINGS_ENV`` / ``SETTINGS_TOP``) for ``tier``.

    ``EITHER``-tier outputs match both the managed and user selections. A
    conditional target is included only when its condition is in ``satisfied``.
    """
    settings_placements = (Placement.SETTINGS_ENV, Placement.SETTINGS_TOP)

    def select(output: Output) -> bool:
        if output.placement not in settings_placements:
            return False
        if output.tier not in (tier, Tier.EITHER):
            return False
        if output.condition is not None and output.condition not in satisfied:
            return False
        return True

    return select


def for_os_env(output: Output) -> bool:
    """Select the OS-env-persistence target (``Placement.OS_ENV``, unconditional)."""
    return output.placement is Placement.OS_ENV and output.condition is None
