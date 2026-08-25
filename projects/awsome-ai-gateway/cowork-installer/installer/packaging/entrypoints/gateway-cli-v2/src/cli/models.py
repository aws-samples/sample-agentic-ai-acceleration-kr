# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Canonical model aliases accepted by ``gateway-cli setup --model``.

This is the single source of truth for which model aliases the CLI advertises.
The UI-generated copy-paste setup command supplies exactly one of these via
``--model``; anything else is rejected so a typo never lands silently in the
Cowork managed configuration (``inferenceModels``).

These are the **Cowork** gateway aliases (e.g. ``cowork-opus``), registered on
the deployed gateway's ``model.model_aliases`` table — NOT the Claude Code
``claude-*`` aliases. A ``claude-*`` value here would be routed to an
unregistered alias and fail with model-not-found. See
docs/cowork-enable-guide.md §A-4 for the alias contract.
"""

from __future__ import annotations

# FALLBACK roster used only when the caller does not supply --available-models
# (the model list is chosen at `setup` time so adding/retiring models needs no
# CLI rebuild). The first entry is the default when a caller omits one.
#
# Cowork traffic is force-routed to Opus 4.8 by User-Agent on the gateway, so the
# baked alias is cosmetic for routing — it is the label the Desktop model picker
# shows. The baked alias must be registered + ACTIVE in the gateway's
# ``model.model_aliases`` so it never hits model-not-found.
FALLBACK_MODELS: tuple[str, ...] = (
    "global.anthropic.claude-opus-4-8",
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

    When the caller supplied --available-models we honour it verbatim (dynamic,
    no rebuild). Otherwise we fall back to the baked FALLBACK_MODELS so the CLI
    still advertises a sensible default set.
    """
    return list(available) if available else list(FALLBACK_MODELS)


def is_allowed_model(model: str, available: list[str] | None = None) -> bool:
    """Return True if ``model`` is valid for the effective roster.

    With an explicit --available-models roster, membership in THAT list is the
    only gate (no hard-coded allowlist blocks a newly-introduced model such as a
    future claude-sonnet-5). Without one, the baked FALLBACK_MODELS applies.
    """
    return model in resolve_model_roster(available)
