# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Unit tests for the single emission path ``cli.emit.emit`` (T5.1).

Exercised against real catalog fields covering every ValueKind shape that must
round-trip through emission: a scalar with multiple targets, a JSON-array
(``availableModels``), a command record (``statusLine``), a deep-merge object
(``permissions``) and the composed attr-map (``OTEL_RESOURCE_ATTRIBUTES``). The
load-bearing case is AR-F2: the conditional user-tier ``OTEL_RESOURCE_ATTRIBUTES``
target must NOT be emitted unless the caller opts into its condition.
"""

from __future__ import annotations

from cli.emit import Emitted, emit, for_os_env, for_settings_tier
from cli.manifest import Placement, Tier, by_key


def test_none_typed_emits_nothing() -> None:
    """An unresolved field (typed=None) produces no fragments, not a blank write."""
    assert emit(by_key("ANTHROPIC_BASE_URL"), None) == []


def test_scalar_emits_every_unconditional_target() -> None:
    """ANTHROPIC_BASE_URL has managed-env + user-env + OS-env targets; all emit."""
    out = emit(by_key("ANTHROPIC_BASE_URL"), "https://gw.example")
    placements = [(e.output.placement, e.output.tier) for e in out]
    assert placements == [
        (Placement.SETTINGS_ENV, Tier.MANAGED),
        (Placement.SETTINGS_ENV, Tier.USER),
        (Placement.OS_ENV, Tier.NONE),
    ]
    # Scalar encode is placement-invariant: same string at every target.
    assert {e.value for e in out} == {"https://gw.example"}
    assert all(e.key == "ANTHROPIC_BASE_URL" for e in out)


def test_str_list_encodes_json_array_at_settings_top() -> None:
    """availableModels (STR_LIST @ SETTINGS_TOP) emits a native list, not CSV."""
    roster = ["claude-sonnet-4-6", "claude-haiku-4-5"]
    out = emit(by_key("availableModels"), roster)
    assert len(out) == 1
    assert out[0].value == roster  # JSON array shape for a settings file
    assert isinstance(out[0].value, list)


def test_command_encodes_record_at_settings_top() -> None:
    """statusLine (COMMAND @ SETTINGS_TOP) emits the {type, command} record."""
    out = emit(by_key("statusLine"), {"type": "command", "command": "statusline"})
    assert out[0].value == {"type": "command", "command": "statusline"}


def test_json_object_encodes_native_dict_at_settings_top() -> None:
    """permissions (JSON_OBJECT @ SETTINGS_TOP) emits a native dict."""
    perms = {"allow": ["Bash(ls:*)"], "deny": []}
    out = emit(by_key("permissions"), perms)
    assert out[0].value == perms
    assert isinstance(out[0].value, dict)


def test_attr_map_default_emits_only_unconditional_managed_target() -> None:
    """AR-F2: the default filter skips the conditional user-tier OTEL target."""
    typed = {"service.name": "claude-code", "user.id": "alice@corp"}
    out = emit(by_key("OTEL_RESOURCE_ATTRIBUTES"), typed)
    assert len(out) == 1
    assert out[0].output.tier is Tier.MANAGED
    assert out[0].output.condition is None
    assert out[0].value == "service.name=claude-code,user.id=alice@corp"


def test_attr_map_user_target_requires_satisfied_condition() -> None:
    """The user-tier OTEL copy emits ONLY when its condition is satisfied (AR-F2)."""
    field = by_key("OTEL_RESOURCE_ATTRIBUTES")
    typed = {"service.name": "claude-code", "user.id": "alice@corp"}

    # Not seeded ⇒ conditional user target is withheld.
    assert emit(field, typed, for_settings_tier(Tier.USER)) == []

    # Seeded ⇒ the user-tier copy is emitted.
    out = emit(field, typed, for_settings_tier(Tier.USER, satisfied=frozenset({"user_tier_seeded"})))
    assert len(out) == 1
    assert out[0].output.tier is Tier.USER
    assert out[0].output.condition == "user_tier_seeded"
    assert out[0].value == "service.name=claude-code,user.id=alice@corp"


def test_for_settings_tier_managed_selects_only_managed_settings() -> None:
    """The managed selector drops the user-env and OS-env targets."""
    out = emit(by_key("ANTHROPIC_BASE_URL"), "https://gw.example", for_settings_tier(Tier.MANAGED))
    assert [(e.output.placement, e.output.tier) for e in out] == [
        (Placement.SETTINGS_ENV, Tier.MANAGED),
    ]


def test_for_settings_tier_either_matches_both_tiers() -> None:
    """An EITHER-tier output (permissions) matches both managed and user selection."""
    perms = {"allow": ["Bash(ls:*)"]}
    assert len(emit(by_key("permissions"), perms, for_settings_tier(Tier.MANAGED))) == 1
    assert len(emit(by_key("permissions"), perms, for_settings_tier(Tier.USER))) == 1


def test_for_os_env_selects_only_os_target() -> None:
    """The OS-env selector picks the persisted-env target and nothing else."""
    out = emit(by_key("ANTHROPIC_BASE_URL"), "https://gw.example", for_os_env)
    assert len(out) == 1
    assert out[0].output.placement is Placement.OS_ENV
    assert isinstance(out[0], Emitted)
