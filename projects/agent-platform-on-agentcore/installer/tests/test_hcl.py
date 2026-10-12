"""
Tests for the variables.tf parser and tfvars line editor.

Two decisions are load-bearing here.

First, the form is generated from variables.tf rather than hardcoded, so a new
Terraform variable cannot silently go missing from the UI. The parser therefore
runs against the REAL infra/envs/standalone/variables.tf — a hand-written
fixture would only prove the parser matches itself, not the file it must read.

Second, writes replace a single line instead of regenerating the file. The live
tfvars carries aligned `=`, blank-line grouping, comments, and commented-out
examples (`# server_image = ...`). Regenerating would destroy all of it, and
matching too loosely would edit the commented-out example instead of the real
key.

The expected variable list is derived from the file by a second, independent
scan rather than frozen as a literal. A frozen copy was tried first and did the
opposite of its job: three variables were added to variables.tf and the only
thing that happened was that this file went red and stayed red, so it stopped
being read at all. A line scan still catches the failure that matters — the
brace-counting parser silently dropping a block — without needing a hand edit
every time infra grows a variable.
"""
import os
import re
import sys


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from installer.core import env as envmod  # noqa: E402
from installer.core import hcl  # noqa: E402

STANDALONE_VARS = envmod.get("standalone").variables_path.read_text()
EXAMPLE_TFVARS = (envmod.get("standalone").tf_dir / "terraform.tfvars.example").read_text()

# Independent of hcl.parse_variables: only top-level `variable "x" {` openings,
# found line by line instead of by counting braces.
DECLARED = re.findall(r'^variable\s+"([a-z_0-9]+)"\s*\{', STANDALONE_VARS, re.M)


# ── parsing the real variables.tf ────────────────────────────────────────────

def test_parses_every_variable_in_the_real_file():
    names = [v.name for v in hcl.parse_variables(STANDALONE_VARS)]
    assert names == DECLARED
    # Sanity check on the scan itself, so an empty DECLARED cannot pass by
    # matching an empty parse.
    assert "project" in names and "admin_password" in names


def _var(name):
    return {v.name: v for v in hcl.parse_variables(STANDALONE_VARS)}[name]


def test_types_are_classified():
    assert _var("project").type == "string"
    assert _var("azs").type == "list(string)"
    assert _var("registry_auto_approval").type == "bool"
    assert _var("desired_count").type == "number"


def test_sensitive_is_detected():
    for name in ("admin_password", "user_password"):
        assert _var(name).sensitive is True, name
    assert _var("admin_email").sensitive is False


def test_defaults_are_detected():
    assert _var("admin_email").has_default is False
    assert _var("project").has_default is True
    assert _var("project").default_literal == '"bap"'
    assert _var("azs").has_default is True


def test_heredoc_description_is_dedented():
    """project's description is parsed correctly."""
    d = _var("project").description
    assert d.startswith("Name prefix for every resource in this environment.")
    assert not d.startswith(" ")


def test_quoted_description_is_read():
    assert _var("desired_count").description == (
        "Tasks per service. 0 parks the environment without destroying it."
    )


def test_variable_without_description_is_empty_not_none():
    assert _var("region").description == ""


# ── the tab-placement rule ──────────────────────────────────────────────────

def test_required_is_no_default_plus_one_explicit_exception():
    required = {v.name for v in hcl.parse_variables(STANDALONE_VARS) if v.required}
    assert required == {
        "admin_email", "admin_password",
        "user_email", "user_password",
        "bedrock_model_id",     # model choice must not be silently defaulted
    }


def test_required_and_advanced_cover_every_variable():
    """A new variable must land in one tab or the other, never nowhere."""
    all_vars = hcl.parse_variables(STANDALONE_VARS)
    required = [v for v in all_vars if v.required]
    advanced = [v for v in all_vars if not v.required]
    assert len(required) + len(advanced) == len(all_vars) == len(DECLARED)


# ── rendering HCL literals ──────────────────────────────────────────────────

def test_render_string_is_quoted():
    assert hcl.render_value("hello", "string") == '"hello"'


def test_render_string_escapes_quotes_and_backslashes():
    assert hcl.render_value('pa"ss\\word', "string") == '"pa\\"ss\\\\word"'


def test_render_bool_is_lowercase_unquoted():
    assert hcl.render_value(True, "bool") == "true"
    assert hcl.render_value(False, "bool") == "false"


def test_render_number_is_unquoted():
    assert hcl.render_value(3, "number") == "3"


def test_render_list_of_strings():
    assert hcl.render_value(["a", "b"], "list(string)") == '["a", "b"]'
    assert hcl.render_value([], "list(string)") == "[]"


# ── single-line replacement ─────────────────────────────────────────────────

def test_replacing_a_key_preserves_alignment_and_every_other_byte():
    out = hcl.set_var(EXAMPLE_TFVARS, "admin_email", '"me@example.com"')
    before, after = EXAMPLE_TFVARS.splitlines(), out.splitlines()
    assert len(before) == len(after)
    changed = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
    assert len(changed) == 1
    assert after[changed[0]] == 'admin_email    = "me@example.com"'


def test_missing_key_is_appended():
    out = hcl.set_var(EXAMPLE_TFVARS, "desired_count", "0")
    assert out.splitlines()[-1] == "desired_count = 0"
    assert out.startswith(EXAMPLE_TFVARS.rstrip("\n"))


def test_commented_out_example_is_not_edited():
    """terraform.tfvars.example carries `# server_image = "..."` as a comment.
    Matching it would write the value into a comment and leave Terraform with
    the placeholder image."""
    assert "# server_image" in EXAMPLE_TFVARS
    out = hcl.set_var(EXAMPLE_TFVARS, "server_image", '"acct.dkr.ecr/x/server:v1"')
    assert '# server_image = "<account>' in out          # comment untouched
    assert out.splitlines()[-1] == 'server_image = "acct.dkr.ecr/x/server:v1"'


def test_replacement_is_idempotent():
    once = hcl.set_var(EXAMPLE_TFVARS, "admin_email", '"a@b.c"')
    twice = hcl.set_var(once, "admin_email", '"a@b.c"')
    assert once == twice


def test_appending_to_a_file_without_trailing_newline():
    out = hcl.set_var('admin_email = "a@b.c"', "desired_count", "1")
    assert out == 'admin_email = "a@b.c"\ndesired_count = 1\n'


def test_read_values_reads_uncommented_keys_only():
    values = hcl.read_values(EXAMPLE_TFVARS)
    assert values["admin_email"] == '"admin@example.com"'
    assert "server_image" not in values          # only present as a comment


def test_render_map_literal_is_written_verbatim():
    """`"{}"` is a string to terraform; the real first apply failed on it."""
    assert hcl.render_value("{}", "map(string)") == "{}"
    assert hcl.render_value("", "map(string)") == "{}"
    literal = '{ "platform-status" = "arn:aws:bedrock-agentcore:ap-northeast-1:1:runtime/x" }'
    assert hcl.render_value(literal, "map(string)") == literal


def test_render_map_from_key_value_pairs():
    out = hcl.render_value("a = 1, b=two", "map(string)")
    assert out == '{ "a" = "1", "b" = "two" }'
    assert hcl.render_value({"k": "v"}, "map(string)") == '{ "k" = "v" }'


def test_every_real_default_round_trips_as_its_own_type(tmp_path, monkeypatch):
    """Saving the form untouched must reproduce literals terraform accepts.

    Goes through the same load → render path Settings uses, over the real
    variables.tf, so a new non-scalar type cannot regress silently again.
    """
    from installer.core import values
    from installer.core.env import get

    monkeypatch.setattr(values, "_tfvars_path", lambda e: tmp_path / "none.tfvars")
    for field in values.load_fields(get("standalone")):
        v = field.variable
        if not v.has_default or v.default_literal is None:
            continue
        rendered = hcl.render_value(
            field.value if v.type not in ("bool", "number")
            else (field.value == "true" if v.type == "bool" else int(field.value)),
            v.type,
        )
        if v.type.startswith(("list(", "map(")):
            assert rendered.replace(" ", "") == v.default_literal.replace(" ", ""), v.name
        else:
            assert rendered == v.default_literal, v.name


def test_multi_line_list_default_is_read_whole():
    text = (
        'variable "models" {\n'
        '  type    = list(string)\n'
        '  default = [\n'
        '    "a",\n'
        '    "b",\n'
        '  ]\n'
        '}\n'
        'variable "after" {\n  type = string\n  default = "x"\n}\n'
    )
    vars_ = {v.name: v for v in hcl.parse_variables(text)}
    assert vars_["models"].default_literal == '["a", "b"]'
    assert vars_["after"].default_literal == '"x"'


def test_real_allowed_models_default_keeps_its_items():
    from installer.core.env import get

    var = {v.name: v for v in hcl.read_variables(get("standalone").variables_path)}
    assert var["allowed_models"].default_literal.count('"') >= 4
