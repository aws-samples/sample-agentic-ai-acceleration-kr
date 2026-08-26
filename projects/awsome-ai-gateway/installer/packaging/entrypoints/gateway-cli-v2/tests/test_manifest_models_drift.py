# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Drift guard: the models roster and the manifest's MODEL fields stay consistent.

``cli.models`` is the single source of truth for the model aliases the CLI
advertises (``FALLBACK_MODELS`` + the ``--available-models`` override). The
manifest catalogues the model-selection keys (``model``,
``ANTHROPIC_DEFAULT_*_MODEL``) as :class:`ValueKind.MODEL_ALIAS`. These tests
enforce the F2 invariant — every advertised alias is a **gateway alias**, never a
``us.anthropic.*`` Bedrock inference-profile id (which would not route) — and
that the roster stays internally resolvable (no stale/duplicated entries, default
in-roster) and consistent with how the manifest types those fields.
"""

from __future__ import annotations

import pytest

from cli import manifest, models, validators
from cli.manifest import Status, ValueKind


def test_fallback_models_are_gateway_aliases() -> None:
    """F2: every roster alias is a bare gateway alias, not a Bedrock profile id."""
    assert models.FALLBACK_MODELS, "FALLBACK_MODELS must not be empty"
    for alias in models.FALLBACK_MODELS:
        assert alias.startswith("claude-"), f"{alias!r} is not a gateway alias"
        # us.anthropic.* / eu.anthropic.* / anthropic.* are Bedrock inference-profile
        # ids — F2 forbids advertising them; the gateway resolves plain aliases.
        assert "anthropic." not in alias, f"{alias!r} looks like a Bedrock profile id"


def test_no_duplicate_roster_entries() -> None:
    """A stale/duplicated FALLBACK_MODELS entry is flagged."""
    assert len(models.FALLBACK_MODELS) == len(set(models.FALLBACK_MODELS))


def test_default_model_is_in_roster() -> None:
    """The default must be advertised and accepted by the roster gate."""
    assert models.DEFAULT_MODEL in models.FALLBACK_MODELS
    assert models.is_allowed_model(models.DEFAULT_MODEL)


def test_every_roster_alias_resolves() -> None:
    """Each roster alias passes the effective-roster membership gate."""
    for alias in models.FALLBACK_MODELS:
        assert models.is_allowed_model(alias), f"{alias!r} unresolvable in fallback roster"
    # Default roster resolution returns the fallback list verbatim.
    assert models.resolve_model_roster(None) == list(models.FALLBACK_MODELS)


def test_roster_honours_supplied_available_models() -> None:
    """A supplied roster is returned verbatim (routed through resolve/STR_LIST)."""
    supplied = ["claude-sonnet-4-6", "claude-haiku-4-5"]
    assert models.resolve_model_roster(supplied) == supplied


def test_roster_rejects_bedrock_profile_id() -> None:
    """F2: a us.anthropic.* id in --available-models fails loudly, not silently."""
    with pytest.raises(validators.ValidationError):
        models.resolve_model_roster(["us.anthropic.claude-sonnet-4-6-v1:0"])


def test_manifest_model_alias_fields_are_validated() -> None:
    """Every OWNED MODEL_ALIAS field routes through the model_alias validator.

    Keeps the manifest's typing consistent with the roster gate: a MODEL_ALIAS
    field must declare ``validate="model_alias"`` so the (T3.4) validator can
    reject a us.anthropic.* id at setup/build time.
    """
    alias_fields = [
        f for f in manifest.FIELDS
        if f.value_kind is ValueKind.MODEL_ALIAS and f.status is Status.OWNED
    ]
    assert alias_fields, "expected OWNED MODEL_ALIAS fields (model, ANTHROPIC_DEFAULT_*_MODEL)"
    for f in alias_fields:
        assert f.validate == "model_alias", (
            f"{f.key!r} is MODEL_ALIAS but validate={f.validate!r}, expected 'model_alias'"
        )
