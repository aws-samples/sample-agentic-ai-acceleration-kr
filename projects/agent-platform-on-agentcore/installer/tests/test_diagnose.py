"""
Tests for error interpretation.

The mapping turns AWS and Terraform failure text into the next action, using
traps this repository has actually hit (recorded in infra/README.md). Two
properties matter more than any single entry: an unrecognised error must be
passed through verbatim rather than swallowed, and a recognised one must name
what to do next.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from installer.core import diagnose  # noqa: E402


def test_name_collision_points_at_the_project_prefix_and_bucket_suffix():
    """Two stacks in one account share the bap-* prefix, so the fix is a
    different prefix (or bucket suffix)."""
    out = diagnose.explain(
        "Error creating S3 bucket: BucketAlreadyExists: The requested bucket name is not available"
    )
    assert out is not None
    assert "project" in out
    assert "bucket_suffix" in out


def test_state_lock_extracts_the_id_for_force_unlock():
    out = diagnose.explain(
        "Error acquiring the state lock\n"
        "  ID:        b7a9c1e2-3f4d-5a6b-7c8d-9e0f1a2b3c4d\n"
        "  Path:      bap-tfstate/standalone/terraform.tfstate"
    )
    assert "force-unlock" in out
    assert "b7a9c1e2-3f4d-5a6b-7c8d-9e0f1a2b3c4d" in out


def test_state_lock_without_an_id_still_explains():
    out = diagnose.explain("Error acquiring the state lock: ConditionalCheckFailedException")
    assert "force-unlock" in out


def test_missing_credentials_suggests_signing_in():
    out = diagnose.explain("Error: No valid credential sources found")
    assert "aws sso login" in out or "자격증명" in out


def test_gateway_delete_race_suggests_retrying():
    out = diagnose.explain(
        "ConflictException: Cannot create gateway while another operation is in progress"
    )
    assert "재시도" in out


def test_unrecognised_error_returns_none_so_the_original_is_shown():
    assert diagnose.explain("Error: something nobody has seen before") is None


def test_empty_output_returns_none():
    assert diagnose.explain("") is None


def test_uninitialised_directory_points_at_terraform_init():
    out = diagnose.explain(
        "Error: Inconsistent dependency lock file\n"
        "  - provider registry.terraform.io/hashicorp/aws: required by this "
        "configuration but no version is selected"
    )
    assert "terraform init" in out
