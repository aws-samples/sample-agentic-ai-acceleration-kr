# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Value validators for catalogued config fields.

A validator answers one question: *is this parsed value acceptable for this
field?* It runs after a codec has parsed the raw value (``codecs.parse``) and
before resolution/emission commits it, so a bad value fails loudly at
``setup``/``build`` time rather than landing silently in a user's Claude Code
settings.

Two dispatch keys, checked in order by :func:`validate`:

1. a field's explicit ``validate`` name (``"url"`` / ``"path"`` / ``"model_alias"``
   in the manifest) → the matching entry in :data:`VALIDATORS`; then
2. the field's :class:`~cli.manifest.ValueKind` → :data:`_BY_KIND` (currently
   only ``ENUM``, which checks the field's ``choices``).

A field with neither an applicable name nor a kind rule is accepted as-is.

The load-bearing rule is **F2**: a ``MODEL_ALIAS`` must be a gateway alias
(e.g. ``claude-sonnet-4-6``), never a ``us.anthropic.*`` Bedrock
inference-profile id. gateway-cli routes Claude Code through the gateway on the
Anthropic-native path (C3), so a Bedrock profile id would point Claude Code at
the wrong roster. Rejecting it here keeps a copy-pasted Bedrock id from a
console out of ``--model`` / ``ANTHROPIC_DEFAULT_*_MODEL``.
"""

from __future__ import annotations

from urllib.parse import urlparse

from cli.manifest import ConfigField, ValueKind


class ValidationError(ValueError):
    """A field value failed validation; the message is user-facing."""


# Region prefixes that mark a Bedrock cross-region inference-profile id
# (e.g. ``us.anthropic.claude-...``). Any ``<region>.anthropic.`` shape is a
# profile id, so we detect the substring rather than enumerate every region.
_BEDROCK_PROFILE_MARKER = ".anthropic."


def validate_model_alias(value: str, field: ConfigField | None = None) -> None:
    """F2: accept a gateway alias; reject a Bedrock inference-profile id."""
    alias = value.strip()
    if not alias:
        raise ValidationError("model alias is empty")
    if _BEDROCK_PROFILE_MARKER in alias.lower() or alias.lower().startswith("anthropic."):
        raise ValidationError(
            f"{value!r} looks like a Bedrock inference-profile id; use a gateway model "
            "alias instead (e.g. claude-sonnet-4-6). gateway-cli routes on the "
            "Anthropic-native path, not Bedrock."
        )


def validate_enum(value: str, field: ConfigField | None = None) -> None:
    """ENUM: value must be one of the field's declared ``choices``."""
    choices = field.choices if field is not None else ()
    if choices and value not in choices:
        allowed = ", ".join(choices)
        raise ValidationError(f"{value!r} is not a valid choice; expected one of: {allowed}")


def validate_url(value: str, field: ConfigField | None = None) -> None:
    """URL sanity: an http(s) scheme and a host must be present."""
    text = value.strip()
    if not text:
        raise ValidationError("URL is empty")
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https"):
        raise ValidationError(f"{value!r} must be an http(s) URL")
    if not parsed.netloc:
        raise ValidationError(f"{value!r} is missing a host")


def validate_path(value: str, field: ConfigField | None = None) -> None:
    """PATH sanity: non-blank (target existence is machine-specific, not checked here)."""
    if not value.strip():
        raise ValidationError("path is empty")


#: Named validators, keyed by ``ConfigField.validate``.
VALIDATORS: dict[str, object] = {
    "model_alias": validate_model_alias,
    "url": validate_url,
    "path": validate_path,
}

#: Kind-driven fallbacks applied when a field has no named validator.
_BY_KIND: dict[ValueKind, object] = {
    ValueKind.ENUM: validate_enum,
}


def validate(field: ConfigField, value: str) -> None:
    """Validate ``value`` for ``field``; raise :class:`ValidationError` if invalid.

    Named validator (``field.validate``) wins; otherwise a kind rule applies; a
    field with neither is accepted.
    """
    fn = VALIDATORS.get(field.validate) if field.validate else None
    if fn is None:
        fn = _BY_KIND.get(field.value_kind)
    if fn is not None:
        fn(value, field)  # type: ignore[operator]
