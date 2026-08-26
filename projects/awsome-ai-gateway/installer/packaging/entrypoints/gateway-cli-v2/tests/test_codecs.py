# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Round-trip + shape tests for the codec registry and validators (T3.5).

Covers every :class:`cli.manifest.ValueKind`: scalars round-trip identically,
structured kinds emit the correct JSON shape per placement, and the F2 /
ENUM / URL / PATH validators reject bad values with a clear message.
"""

from __future__ import annotations

import json

import pytest

from cli import codecs, validators
from cli.manifest import Category, ConfigField, Placement, Status, ValueKind

_SCALAR_KINDS = (
    ValueKind.STR,
    ValueKind.URL,
    ValueKind.PATH,
    ValueKind.ENUM,
    ValueKind.MODEL_ALIAS,
)

_ENV_PLACEMENTS = (Placement.SETTINGS_ENV, Placement.PROCESS_ENV, Placement.OS_ENV)


def test_every_value_kind_has_a_codec() -> None:
    """Registry is exhaustive — no catalogued kind can KeyError at emit time."""
    assert set(codecs.CODECS) == set(ValueKind)


@pytest.mark.parametrize("kind", _SCALAR_KINDS)
@pytest.mark.parametrize("placement", (*_ENV_PLACEMENTS, Placement.SETTINGS_TOP))
def test_scalar_round_trip_identity(kind: ValueKind, placement: Placement) -> None:
    """encode(parse(x)) == x for every scalar kind, at every placement."""
    codec = codecs.codec_for(kind)
    for raw in ("claude-sonnet-4-6", "https://gw.example.com", "/opt/helper"):
        assert codec.encode(codec.parse(raw), placement) == raw


def test_csv_round_trip() -> None:
    codec = codecs.codec_for(ValueKind.CSV)
    assert codec.parse("a, b ,c") == ["a", "b", "c"]
    assert codec.encode(["a", "b", "c"], Placement.SETTINGS_ENV) == "a,b,c"
    # Whole round-trip on a normalised value is identity.
    assert codec.encode(codec.parse("a,b,c"), Placement.PROCESS_ENV) == "a,b,c"


def test_str_list_json_array_at_top_csv_in_env() -> None:
    codec = codecs.codec_for(ValueKind.STR_LIST)
    parsed = codec.parse('["claude-sonnet-4-6", "claude-haiku-4-5"]')
    assert parsed == ["claude-sonnet-4-6", "claude-haiku-4-5"]
    # SETTINGS_TOP → native JSON array (json-serialisable list).
    top = codec.encode(parsed, Placement.SETTINGS_TOP)
    assert top == parsed
    assert json.loads(json.dumps(top)) == parsed
    # Env placement → comma-joined string.
    assert codec.encode(parsed, Placement.SETTINGS_ENV) == "claude-sonnet-4-6,claude-haiku-4-5"
    # A bare CSV also parses (site-extra scalar).
    assert codec.parse("a,b") == ["a", "b"]


def test_json_object_dict_at_top_compact_in_env() -> None:
    codec = codecs.codec_for(ValueKind.JSON_OBJECT)
    parsed = codec.parse('{"allow": ["Bash(git*)"]}')
    assert parsed == {"allow": ["Bash(git*)"]}
    assert codec.encode(parsed, Placement.SETTINGS_TOP) == parsed
    env = codec.encode(parsed, Placement.SETTINGS_ENV)
    assert env == '{"allow":["Bash(git*)"]}'
    assert json.loads(env) == parsed  # compact env form is valid JSON


def test_json_object_rejects_non_object() -> None:
    codec = codecs.codec_for(ValueKind.JSON_OBJECT)
    with pytest.raises(ValueError):
        codec.parse("[1, 2, 3]")


def test_command_record_shape() -> None:
    codec = codecs.codec_for(ValueKind.COMMAND)
    # Bare command string → record.
    assert codec.parse("/opt/statusline") == {"type": "command", "command": "/opt/statusline"}
    # Already-shaped record parses back to the same record.
    rec = {"type": "command", "command": "/opt/statusline"}
    assert codec.parse(json.dumps(rec)) == rec
    assert codec.encode(rec, Placement.SETTINGS_TOP) == rec
    # Env placement → bare command string.
    assert codec.encode(rec, Placement.SETTINGS_ENV) == "/opt/statusline"


def test_attr_map_round_trip_preserves_order() -> None:
    codec = codecs.codec_for(ValueKind.ATTR_MAP)
    raw = "service.name=claude-code,user.id=alice@corp"
    parsed = codec.parse(raw)
    assert parsed == {"service.name": "claude-code", "user.id": "alice@corp"}
    assert list(parsed) == ["service.name", "user.id"]
    assert codec.encode(parsed, Placement.SETTINGS_ENV) == raw


# --- validators -----------------------------------------------------------


def _field(**kw: object) -> ConfigField:
    """A minimal catalog field for validator tests (placement/tier irrelevant here)."""
    return ConfigField(
        "X", Category.GATEWAY, Placement.SETTINGS_ENV, Status.OWNED, **kw  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    "bad",
    ("us.anthropic.claude-sonnet-4-6", "eu.anthropic.claude-opus-4-6", "anthropic.claude-x"),
)
def test_model_alias_rejects_bedrock_profile(bad: str) -> None:
    """F2: a Bedrock inference-profile id is rejected with a clear message."""
    field = _field(value_kind=ValueKind.MODEL_ALIAS, validate="model_alias")
    with pytest.raises(validators.ValidationError, match="Bedrock inference-profile"):
        validators.validate(field, bad)


def test_model_alias_accepts_gateway_alias() -> None:
    field = _field(value_kind=ValueKind.MODEL_ALIAS, validate="model_alias")
    validators.validate(field, "claude-sonnet-4-6")  # no raise


def test_enum_checks_choices() -> None:
    field = _field(value_kind=ValueKind.ENUM, choices=("us", "eu", "apac"))
    validators.validate(field, "us")  # no raise
    with pytest.raises(validators.ValidationError, match="valid choice"):
        validators.validate(field, "moon")


def test_url_sanity() -> None:
    field = _field(value_kind=ValueKind.URL, validate="url")
    validators.validate(field, "https://gw.example.com")  # no raise
    with pytest.raises(validators.ValidationError):
        validators.validate(field, "not-a-url")
    with pytest.raises(validators.ValidationError):
        validators.validate(field, "ftp://host/x")


def test_path_sanity() -> None:
    field = _field(value_kind=ValueKind.PATH, validate="path")
    validators.validate(field, "/opt/helper")  # no raise
    with pytest.raises(validators.ValidationError):
        validators.validate(field, "   ")


def test_field_without_rule_is_accepted() -> None:
    """A plain STR field with no validator name passes anything."""
    validators.validate(_field(value_kind=ValueKind.STR), "whatever")
