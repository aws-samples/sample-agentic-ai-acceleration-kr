"""
Tests for the environment definition.

The installer deploys one Terraform environment, infra/envs/standalone. These
tests assert the directory actually exists, so a renamed env directory fails
here rather than at `terraform init` time.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from installer.__main__ import ENV  # noqa: E402
from installer.core import env as envmod  # noqa: E402


def test_the_cli_env_has_a_definition():
    assert tuple(e.name for e in envmod.ALL) == (ENV,)


def test_standalone_uses_the_bap_prefix():
    assert envmod.get("standalone").project == "bap"


def test_tf_dirs_exist_on_disk():
    for e in envmod.ALL:
        assert (e.tf_dir / "variables.tf").is_file(), f"{e.name}: {e.tf_dir}"


def test_repo_root_is_the_repository():
    assert (envmod.REPO_ROOT / "infra" / "README.md").is_file()
    assert (envmod.REPO_ROOT / "run.sh").is_file()


def test_unknown_env_raises():
    with pytest.raises(KeyError):
        envmod.get("prod")


# ── which region the installer addresses ────────────────────────────────────
#
# This was a `REGION = "us-east-1"` constant in three modules while `region` was
# editable in the settings form. Set it to anything else and terraform built the
# stack there while every `aws` call and the ECR hostname stayed in us-east-1.


def test_tfvars_region_beats_the_shell(tmp_path, monkeypatch):
    """tfvars is what terraform reads, so it decides where the resources are."""
    path = tmp_path / "terraform.tfvars"
    path.write_text('region = "ap-northeast-2"\n')
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    region, detail = envmod.resolve_region(envmod.get("standalone"), path)
    assert region == "ap-northeast-2"
    assert "ap-northeast-2" in detail


def test_the_shell_region_never_decides_a_stacks_region(tmp_path, monkeypatch):
    """Terraform's provider region is var.region, which the shell cannot reach.
    Following AWS_REGION here made the dashboard probe an existing us-east-1
    stack and mark its steps 완료 while the deployment went to Tokyo."""
    path = tmp_path / "terraform.tfvars"
    path.write_text('project = "bap"\n')
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    region, detail = envmod.resolve_region(envmod.get("standalone"), path)
    assert region == "ap-northeast-1"
    assert detail == ""


def test_no_tfvars_at_all_still_means_the_variables_tf_default(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    missing = tmp_path / "terraform.tfvars"
    assert envmod.resolve_region(envmod.get("standalone"), missing)[0] == "ap-northeast-1"


def test_aws_region_wins_over_aws_default_region(monkeypatch):
    """Same precedence the AWS CLI itself applies."""
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    assert envmod.resolve_region()[0] == "eu-west-1"


def test_region_default_matches_what_variables_tf_declares(monkeypatch):
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    assert envmod.resolve_region()[0] == "ap-northeast-1"
