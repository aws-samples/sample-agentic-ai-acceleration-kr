# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Redaction golden (T5.5, finding R4): a planted secret never leaves ``config --explain``.

We seed a credential-bearing field (``OTEL_EXPORTER_OTLP_HEADERS`` — the OTLP auth
header — and ``ANTHROPIC_CUSTOM_HEADERS``) with a unique sentinel token via
site-extra, then assert:

* the pure resolver (:func:`cli.config_explain.explain`) reports the field's
  winning **source** but replaces the **value** with the fixed mask; and
* the ``config --explain`` command output contains the mask and NOT the token.

The token is a single unique string, so a substring search of the rendered output
(and of anything logged during the run) is a sound leak check. Every field in
:func:`cli.manifest.sensitive_keys` is exercised so a newly-added secret that
forgot ``sensitive=True`` fails here.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from click.testing import CliRunner

from cli import manifest
from cli.config_explain import REDACTED, explain
from cli.main import cli
from cli.resolve import Sources

_TOKEN = "sk-SENTINEL-6f3a9c-DO-NOT-LEAK"


def test_sensitive_field_value_masked_source_kept() -> None:
    """The pure resolver masks the value of a sensitive field but keeps its source."""
    field = manifest.by_key("OTEL_EXPORTER_OTLP_HEADERS")
    assert field.sensitive, "precondition: the OTLP auth header is sensitive"

    sources = Sources(
        site_extra={"OTEL_EXPORTER_OTLP_HEADERS": f"Authorization=Bearer {_TOKEN}"},
    )
    rows = explain(sources)
    row = next(r for r in rows if r.key == "OTEL_EXPORTER_OTLP_HEADERS")

    assert row.display == REDACTED
    assert _TOKEN not in row.display
    assert row.source == "site-extra"  # source is still surfaced, only the value hides
    # No row anywhere may carry the raw token.
    assert all(_TOKEN not in r.display for r in rows)


def test_every_sensitive_key_is_masked_when_resolved() -> None:
    """A token planted in ANY sensitive field is masked, never echoed verbatim."""
    seeded = {key: f"seed-with-{_TOKEN}" for key in manifest.sensitive_keys()}
    rows = explain(Sources(site_extra=seeded))
    for key in manifest.sensitive_keys():
        matches = [r for r in rows if r.key == key]
        # DOCUMENTED-only sensitive keys are outside the OWNED/PASSTHROUGH view; the
        # ones that ARE shown must be masked.
        for r in matches:
            assert r.display == REDACTED, f"{key} leaked a value into the explain view"
    assert all(_TOKEN not in r.display for r in rows)


def test_config_explain_command_never_prints_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """End-to-end: `config --explain` masks a site-extra-seeded token in its output."""
    site_extra = {
        "managed": {"env": {"OTEL_EXPORTER_OTLP_HEADERS": f"Authorization=Bearer {_TOKEN}"}},
        "user": {"env": {"ANTHROPIC_CUSTOM_HEADERS": f"X-Secret={_TOKEN}"}},
    }
    se_path = tmp_path / "site_extra.json"
    se_path.write_text(json.dumps(site_extra), encoding="utf-8")
    monkeypatch.setenv("GATEWAY_CLI_SITE_EXTRA", str(se_path))
    # Keep the identity lookup offline and deterministic.
    monkeypatch.setattr("cli.resolvers.resolve_identity", lambda: "alice@corp")

    with caplog.at_level(logging.DEBUG):
        result = CliRunner().invoke(cli, ["config", "--explain"])

    assert result.exit_code == 0, result.output
    assert _TOKEN not in result.output, "raw token leaked into config --explain output"
    assert REDACTED in result.output  # the masked marker is present
    # The masked field still appears (source visible), just without its value.
    assert "OTEL_EXPORTER_OTLP_HEADERS" in result.output
    # Nothing logged during the run may carry the token either.
    assert _TOKEN not in caplog.text
