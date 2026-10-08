"""
Tests for destructive-operation confirmation.

destroy and park require typing the environment name rather than pressing y.
A single keystroke is too cheap for an action that takes a stack down: park
leaves the resources but stops the service, and the downtime in between is not
undone by re-applying.

Matching is exact after trimming. Case-insensitive matching would accept "BAP"
for "bap", and these names are lowercase identifiers.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from installer.core import env as envmod  # noqa: E402
from installer.core import steps as stepsmod  # noqa: E402
from installer.screens.confirm import matches  # noqa: E402


def test_exact_name_confirms():
    assert matches("bap", "bap") is True


def test_surrounding_whitespace_is_trimmed():
    assert matches("  bap \n", "bap") is True


def test_wrong_name_does_not_confirm():
    assert matches("agent-platform", "bap") is False


def test_yes_does_not_confirm():
    """The whole point is that y is not enough."""
    for typed in ("y", "yes", "Y", "확인"):
        assert matches(typed, "bap") is False


def test_empty_does_not_confirm():
    assert matches("", "bap") is False


def test_case_must_match():
    assert matches("BAP", "bap") is False


def test_destructive_operations_are_the_expected_two():
    ops = stepsmod.operations(envmod.get("standalone"))
    destructive = {o.id for o in ops if o.destructive}
    assert destructive == {"destroy", "park"}


def test_every_operation_has_a_title_and_action():
    for op in stepsmod.operations(envmod.get("standalone")):
        assert op.title
        assert op.action is not None



def test_destroy_note_warns_unless_allow_destroy_is_applied(tmp_path):
    from dataclasses import replace

    from installer.core import env as envmod
    from installer.screens.operations import destroy_protection_note

    env = replace(envmod.get("standalone"), name="note-test")
    tf_dir = tmp_path / "infra" / "envs" / "note-test"
    tf_dir.mkdir(parents=True)
    saved = envmod.REPO_ROOT
    envmod.REPO_ROOT = tmp_path
    try:
        assert "allow_destroy" in destroy_protection_note(env)          # no tfvars
        (tf_dir / "terraform.tfvars").write_text("allow_destroy = false\n")
        assert "allow_destroy" in destroy_protection_note(env)
        (tf_dir / "terraform.tfvars").write_text("allow_destroy = true\n")
        assert destroy_protection_note(env) == ""
    finally:
        envmod.REPO_ROOT = saved
