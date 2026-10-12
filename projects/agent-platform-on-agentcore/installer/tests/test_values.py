"""
Tests for the settings model.

The installer owns every value, so a user never has to open an editor — which
means the form must cover every Terraform variable in variables.tf and validate
them before `terraform apply` does. Validating here matters because apply
failures arrive minutes later, after resources have been created.

The tab split is derived from variables.tf rather than listed by hand, so a new
variable cannot vanish from the UI. Sensitive values are masked on display but
still written verbatim.
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from installer.core import env as envmod  # noqa: E402
from installer.core import hcl  # noqa: E402
from installer.core import values  # noqa: E402

ENV = envmod.get("standalone")
VARS = {v.name: v for v in hcl.read_variables(ENV.variables_path)}
# Counted from the file directly, not from VARS: comparing the form against the
# same parser that built it proves only that the parser agrees with itself.
DECLARED = re.findall(
    r'^variable\s+"([a-z_0-9]+)"\s*\{', ENV.variables_path.read_text(), re.M
)


def test_every_variable_lands_in_exactly_one_tab():
    fields = values.load_fields(ENV)
    assert len(fields) == len(DECLARED)
    assert {f.variable.name for f in fields} == set(DECLARED) == set(VARS)
    assert {f.tab for f in fields} <= {"required", "advanced"}


def test_required_tab_matches_the_rule():
    fields = values.load_fields(ENV)
    required = {f.variable.name for f in fields if f.tab == "required"}
    assert required == {
        "admin_email", "admin_password", "user_email", "user_password",
        "bedrock_model_id",
    }


# ── validation ──────────────────────────────────────────────────────────────

def test_email_must_look_like_an_email():
    assert values.validate(VARS["admin_email"], "not-an-email") is not None
    assert values.validate(VARS["admin_email"], "a@b.co") is None


def test_password_must_satisfy_the_cognito_policy():
    """Cognito rejects weak passwords at apply time, minutes into a run."""
    assert values.validate(VARS["admin_password"], "short") is not None
    assert values.validate(VARS["admin_password"], "alllowercase123") is not None
    assert values.validate(VARS["admin_password"], "ChangeMe123!") is None
    # modules/cognito sets require_symbols = false: a symbol must not be demanded.
    assert values.validate(VARS["admin_password"], "ChangeMe123") is None


def test_required_value_cannot_be_blank():
    assert values.validate(VARS["admin_email"], "") is not None


def test_optional_value_may_be_blank():
    assert values.validate(VARS["agent_runtime_arn"], "") is None


def test_number_must_parse():
    assert values.validate(VARS["desired_count"], "many") is not None
    assert values.validate(VARS["desired_count"], "2") is None


def test_bool_accepts_only_true_or_false():
    assert values.validate(VARS["registry_auto_approval"], "yes") is not None
    assert values.validate(VARS["registry_auto_approval"], "true") is None


def test_azs_must_have_two_entries():
    """ALB requires two AZs; variables.tf says so and apply enforces it late."""
    assert values.validate(VARS["azs"], "us-east-1a") is not None
    assert values.validate(VARS["azs"], "us-east-1a, us-east-1b") is None


# ── saving ──────────────────────────────────────────────────────────────────

def test_save_writes_literals_and_preserves_the_rest(tmp_path, monkeypatch):
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("*.tfvars\n")
    tfvars = tmp_path / "terraform.tfvars"
    tfvars.write_text('# keep me\nadmin_email    = "old@x.com"\n')
    monkeypatch.setattr(values, "_tfvars_path", lambda e: tfvars)

    values.save(ENV, {
        "admin_email": "new@x.com",
        "registry_auto_approval": "false",
        "desired_count": "2",
        "azs": "us-east-1a, us-east-1b",
    })
    text = tfvars.read_text()
    assert "# keep me" in text
    assert 'admin_email    = "new@x.com"' in text
    assert "registry_auto_approval = false" in text      # bool: unquoted
    assert "desired_count = 2" in text                    # number: unquoted
    assert 'azs = ["us-east-1a", "us-east-1b"]' in text   # list literal


def test_save_refuses_when_tfvars_is_not_ignored(tmp_path, monkeypatch):
    import subprocess

    from installer.core import files

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("nothing\n")
    tfvars = tmp_path / "terraform.tfvars"
    monkeypatch.setattr(values, "_tfvars_path", lambda e: tfvars)

    with pytest.raises(files.WriteRefused):
        values.save(ENV, {"admin_password": "ChangeMe123!"})


def test_map_input_must_be_hcl_or_pairs():
    from installer.core import hcl as hclmod

    var = hclmod.TfVariable(
        name="runtime_mcp_servers", type="map(string)", description="",
        sensitive=False, has_default=True, default_literal="{}",
    )
    assert values.validate(var, "{}") is None
    assert values.validate(var, "a = b, c = d") is None
    assert values.validate(var, "just-a-string") is not None
