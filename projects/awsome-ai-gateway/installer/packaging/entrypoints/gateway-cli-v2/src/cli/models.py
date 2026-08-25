# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Canonical model aliases accepted by ``gateway-cli setup --model``.

This is the single source of truth for which model aliases the CLI advertises.
The UI-generated copy-paste setup command supplies exactly one of these via
``--model``; anything else is rejected so a typo never lands silently in
Claude Code settings.

Keep this in sync with the statusline display map
(``statusline/formatter.py::_MODEL_SHORT``) — the same aliases appear there.
"""

from __future__ import annotations

# Ordered best→smallest. This is only the FALLBACK roster used when the caller
# does not supply --available-models: the model list is meant to be chosen at
# `setup` time (guideline 1-2) so adding/retiring models needs no CLI rebuild.
# The first entry is the default when a caller omits one.
FALLBACK_MODELS: tuple[str, ...] = (
    "claude-sonnet-4-6",
    "claude-opus-4-6",
    "claude-haiku-4-5",
)

# Backwards-compat alias — earlier code referred to ALLOWED_MODELS.
ALLOWED_MODELS = FALLBACK_MODELS

DEFAULT_MODEL = FALLBACK_MODELS[0]


def parse_available_models(raw: str | None) -> list[str]:
    """Parse a comma-separated --available-models value into an ordered list.

    Whitespace around each alias is trimmed and blanks dropped; order is
    preserved and duplicates removed (first occurrence wins) so the picker shows
    a clean, stable roster. Returns [] when nothing usable was supplied.
    """
    if not raw:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for part in raw.split(","):
        alias = part.strip()
        if alias and alias not in seen:
            seen.add(alias)
            out.append(alias)
    return out


def resolve_model_roster(available: list[str] | None) -> list[str]:
    """The effective list of models the picker should expose.

    Routed through the single resolver (:func:`cli.resolve.resolve`) so the
    picker roster shares the ``availableModels`` field's codec (``STR_LIST``) and
    precedence with settings emission rather than walking its own path (finding:
    *fragmentation*). A supplied ``--available-models`` roster is honoured
    verbatim (dynamic, no rebuild); with none supplied the field resolves to
    nothing and we fall back to the baked ``FALLBACK_MODELS`` so the CLI still
    advertises a sensible default set.

    F2: every effective alias is validated as a gateway alias — a
    ``us.anthropic.*`` Bedrock inference-profile id raises
    :class:`~cli.validators.ValidationError` rather than landing in the picker
    (it would not route on the Anthropic-native path). ``FALLBACK_MODELS`` are
    all gateway aliases (drift test), so the default roster never raises.

    Imports are deferred to keep this leaf module free of a load-time dependency
    on the resolver stack (mirrors ``site_defaults``).
    """
    from cli import validators
    from cli.manifest import by_key
    from cli.resolve import Sources, resolve

    raw = ",".join(available) if available else ""
    resolved = resolve(by_key("availableModels"), Sources(flags={"availableModels": raw}))
    roster = list(resolved.value) if resolved.value else list(FALLBACK_MODELS)
    for alias in roster:
        validators.validate_model_alias(alias)
    return roster


def is_allowed_model(model: str, available: list[str] | None = None) -> bool:
    """Return True if ``model`` is valid for the effective roster.

    With an explicit --available-models roster, membership in THAT list is the
    only gate (no hard-coded allowlist blocks a newly-introduced model such as a
    future claude-sonnet-5). Without one, the baked FALLBACK_MODELS applies.
    """
    return model in resolve_model_roster(available)
