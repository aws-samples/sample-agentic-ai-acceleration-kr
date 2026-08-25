# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""F2 at the command boundary: ``setup`` rejects Bedrock inference-profile ids.

The emit path (:func:`cli.emit.emit`) *encodes* a value but never validates it, and
``setup``'s roster-membership check only runs when ``--available-models`` is supplied.
So without a command-boundary validation a ``us.anthropic.*`` id passed to ``--model``
or ``--default-*-model`` would land verbatim in Claude Code settings and point it at a
roster the Anthropic-native gateway path cannot route (F2). These tests pin that
``setup`` now rejects such ids up front — while still accepting an arbitrary *gateway*
alias not in the baked fallback roster (a future ``claude-sonnet-5``), which setup
documents as allowed when no ``--available-models`` is given.
"""

from __future__ import annotations

from unittest import mock

import pytest
from click.testing import CliRunner

from cli.main import _validate_model_flag, cli
from cli.validators import ValidationError


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ---- the helper in isolation ------------------------------------------------


def test_validate_model_flag_rejects_bedrock_profile_id() -> None:
    import click

    with pytest.raises(click.ClickException) as ei:
        _validate_model_flag("--model", "model", "us.anthropic.claude-opus-4-8")
    assert "--model:" in str(ei.value)
    assert "Bedrock" in str(ei.value)


def test_validate_model_flag_accepts_gateway_alias_not_in_fallback() -> None:
    # A future gateway alias must pass — this is not a membership check.
    _validate_model_flag("--model", "model", "claude-sonnet-5")


@pytest.mark.parametrize("value", [None, "", "   "])
def test_validate_model_flag_noop_on_unset(value: str | None) -> None:
    # An unset optional flag (None/blank) validates to nothing.
    _validate_model_flag("--default-opus-model", "ANTHROPIC_DEFAULT_OPUS_MODEL", value)


def test_validate_model_flag_routes_through_catalog_validator() -> None:
    # Proves it uses the field's catalogued "model_alias" validator, not an ad-hoc
    # check — so the manifest stays the single source of truth for the rule.
    with mock.patch("cli.validators.validate", side_effect=ValidationError("boom")) as v:
        import click

        with pytest.raises(click.ClickException):
            _validate_model_flag("--model", "model", "whatever")
    assert v.called


# ---- through the setup command ---------------------------------------------


@pytest.fixture
def _no_admin_preflight():
    # setup runs an elevation preflight before validation; neutralise it so the
    # test exercises the model-flag gate rather than the OS-privilege check.
    with mock.patch("cli.main.ensure_admin_for_setup", return_value=None):
        yield


@pytest.mark.usefixtures("_no_admin_preflight")
def test_setup_rejects_bedrock_model(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["setup", "--model", "us.anthropic.claude-opus-4-8"])
    assert result.exit_code == 1
    assert "--model:" in result.output
    assert "Bedrock" in result.output


@pytest.mark.usefixtures("_no_admin_preflight")
@pytest.mark.parametrize(
    "flag",
    ["--default-sonnet-model", "--default-opus-model", "--default-haiku-model"],
)
def test_setup_rejects_bedrock_default_model(runner: CliRunner, flag: str) -> None:
    result = runner.invoke(
        cli, ["setup", "--model", "claude-sonnet-4-6", flag, "eu.anthropic.claude-opus-4-8"]
    )
    assert result.exit_code == 1
    assert flag in result.output
    assert "Bedrock" in result.output


@pytest.mark.usefixtures("_no_admin_preflight")
def test_setup_accepts_future_gateway_alias_past_f2_gate(runner: CliRunner) -> None:
    # A gateway alias not in the baked fallback must clear the F2 gate. It may still
    # fail later (config resolution / write) in a bare test env; we only assert it is
    # not rejected as a Bedrock id.
    result = runner.invoke(cli, ["setup", "--model", "claude-sonnet-5"])
    assert "Bedrock" not in result.output
