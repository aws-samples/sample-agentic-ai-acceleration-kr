"""
Tests for the step graph.

Steps are data, not code paths: the UI renders whatever build_plan returns, so
adding a step must not require touching a screen. These tests pin the graph's
integrity (no dangling requires, no cycles, ordering respects dependencies) and
the two places where the manual procedure in infra/README.md is easy to get
wrong:

- Images must build for linux/amd64. The ECS task definitions set no
  runtimePlatform, so Fargate runs x86_64; an arm64 image from an Apple Silicon
  laptop crash-loops with nothing but a CloudWatch trail to explain it.
- create-memory must pass --event-expiry-duration 365. AWS defaults to 30 days,
  silently expiring conversations that were meant to be kept.
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from installer.core import env as envmod  # noqa: E402
from installer.core import steps as stepsmod  # noqa: E402
from installer.core.probe import State  # noqa: E402
from installer.core.runner import Command  # noqa: E402

FULL = envmod.get("standalone")


# ── graph integrity ─────────────────────────────────────────────────────────

def test_every_requires_names_a_real_step():
    for env in envmod.ALL:
        plan = stepsmod.build_plan(env)
        ids = {s.id for s in plan.steps}
        for step in plan.steps:
            for dep in step.requires:
                assert dep in ids, f"{env.name}/{step.id} requires unknown {dep}"


def test_steps_are_listed_after_their_dependencies():
    for env in envmod.ALL:
        seen = set()
        for step in stepsmod.build_plan(env).steps:
            for dep in step.requires:
                assert dep in seen, f"{env.name}/{step.id} precedes its dep {dep}"
            seen.add(step.id)


def test_step_ids_are_unique():
    for env in envmod.ALL:
        ids = [s.id for s in stepsmod.build_plan(env).steps]
        assert len(ids) == len(set(ids))


def test_every_step_has_a_probe():
    for env in envmod.ALL:
        for step in stepsmod.build_plan(env).steps:
            assert callable(step.probe), f"{env.name}/{step.id}"


# ── which steps each environment gets ───────────────────────────────────────

def test_full_stack_plan_is_the_whole_procedure_in_order():
    ids = [s.id for s in stepsmod.build_plan(FULL).steps]
    assert ids == [
        "preflight", "bootstrap", "backend", "transaction_search", "tfvars",
        "init", "first_apply", "images", "second_apply", "memory", "local_env",
    ]


# ── blocking ────────────────────────────────────────────────────────────────

def test_step_is_blocked_by_unfinished_dependencies():
    plan = stepsmod.build_plan(FULL)
    states = {"preflight": State.DONE, "bootstrap": State.PENDING}
    assert plan.blocked_by("backend", states) == ("bootstrap",)


def test_step_is_unblocked_when_dependencies_are_done():
    plan = stepsmod.build_plan(FULL)
    states = {"preflight": State.DONE, "bootstrap": State.DONE}
    assert plan.blocked_by("backend", states) == ()


def test_unknown_dependency_state_blocks():
    """UNKNOWN is not permission to proceed."""
    plan = stepsmod.build_plan(FULL)
    states = {"preflight": State.DONE, "bootstrap": State.UNKNOWN}
    assert plan.blocked_by("backend", states) == ("bootstrap",)


# ── actions ─────────────────────────────────────────────────────────────────

def test_file_write_steps_are_not_commands():
    plan = stepsmod.build_plan(FULL)
    for step_id in ("backend", "tfvars", "local_env"):
        assert isinstance(plan.by_id(step_id).action, stepsmod.FileWrite)


def test_preflight_has_no_action():
    assert stepsmod.build_plan(FULL).by_id("preflight").action is None


def test_command_steps_carry_a_command():
    plan = stepsmod.build_plan(FULL)
    for step_id in ("bootstrap", "init", "first_apply", "second_apply", "memory"):
        assert isinstance(plan.by_id(step_id).action, Command), step_id


def test_backend_config_follows_the_backend_hcl():
    """standalone gets the flag only once the `backend` step has written a
    backend.hcl. The file is generated and git-ignored, so it may or may not
    exist in the tree the tests run in — hence a biconditional."""
    expected = (FULL.tf_dir / "backend.hcl").is_file()
    init = stepsmod.build_plan(FULL).by_id("init").action
    passed = any(a.startswith("-backend-config=") for a in init.argv)
    assert passed is expected


# ── the two easy-to-get-wrong flags ─────────────────────────────────────────

def test_image_build_always_targets_linux_amd64():
    cmds = stepsmod.build_commands(FULL, "images", {"account_id": "123456789012"})
    builds = [c for c in cmds if "build" in c.argv]
    assert len(builds) == 2
    for cmd in builds:
        assert "--platform" in cmd.argv
        assert cmd.argv[cmd.argv.index("--platform") + 1] == "linux/amd64"


def test_memory_creation_pins_expiry_to_365_days():
    cmd = stepsmod.build_plan(FULL).by_id("memory").action
    assert "--event-expiry-duration" in cmd.argv
    assert cmd.argv[cmd.argv.index("--event-expiry-duration") + 1] == "365"


def test_memory_step_includes_the_session_summary_strategy():
    """LONG_TERM_RECALL on the default agent has nothing to recall without it."""
    cmd = stepsmod.build_plan(FULL).by_id("memory").action
    assert "--memory-strategies" in cmd.argv
    strategy_json = cmd.argv[cmd.argv.index("--memory-strategies") + 1]
    assert "summaryMemoryStrategy" in strategy_json
    assert "session_summary" in strategy_json
    assert "/summaries/{actorId}/{sessionId}" in strategy_json


def test_first_apply_parks_the_services():
    """ECR repos do not exist yet on the first apply, so no image can run."""
    cmd = stepsmod.build_plan(FULL).by_id("first_apply").action
    assert "-var" in cmd.argv
    assert "desired_count=0" in cmd.argv


def test_no_command_carries_a_secret_as_an_argument():
    """Command lines are visible to other processes via ps."""
    for env in envmod.ALL:
        for step in stepsmod.build_plan(env).steps:
            if isinstance(step.action, Command):
                joined = " ".join(step.action.argv)
                for secret in ("password", "api_key", "secret"):
                    assert secret not in joined.lower(), f"{env.name}/{step.id}"


# ── destructive operations ──────────────────────────────────────────────────

def test_destroy_and_park_are_destructive():
    ops = {o.id: o for o in stepsmod.operations(FULL)}
    assert ops["destroy"].destructive is True
    assert ops["park"].destructive is True


def test_redeploy_is_not_destructive():
    ops = {o.id: o for o in stepsmod.operations(FULL)}
    assert ops["redeploy"].destructive is False


def test_no_creation_step_is_destructive():
    for env in envmod.ALL:
        for step in stepsmod.build_plan(env).steps:
            assert step.destructive is False, step.id


# ── the resolved project reaches the COMMANDS, not just the probes ───────────
#
# A round of fixes taught the project this the hard way: `resolve_project` was
# applied to the probes but not to the commands, so with an edited `project` the
# probes watched the new prefix while docker pushed to the old one — a successful
# push then read PENDING forever. These assert on argv, where the divergence is
# actually visible.

@pytest.fixture
def tfvars_with_project(tmp_path, monkeypatch):
    """Point the project resolution at a temp tfvars, and return a setter."""
    path = tmp_path / "terraform.tfvars"

    def write(body: str) -> None:
        path.write_text(body)

    monkeypatch.setattr(envmod, "tfvars_path", lambda e: path)
    monkeypatch.setattr(stepsmod.probe, "_tfvars_path", lambda e: path)
    return write


def test_bootstrap_var_follows_the_tfvars_project(tfvars_with_project):
    tfvars_with_project('project = "ap-tui-test"\n')
    cmd = stepsmod.build_plan(FULL).by_id("bootstrap").action
    assert "project=ap-tui-test" in cmd.display()


def test_bootstrap_initialises_before_it_applies():
    """infra/bootstrap has no .terraform/ on a fresh checkout; apply alone fails."""
    shown = stepsmod.build_plan(FULL).by_id("bootstrap").action.display()
    init, apply = shown.index(" init "), shown.index(" apply ")
    assert init < apply
    assert "&&" in shown, "apply must not run if init failed"
    assert shown.count("-chdir=") == 2



def test_image_tags_follow_the_tfvars_project(tfvars_with_project):
    """These must match what images_pushed probes, or a push reads PENDING."""
    tfvars_with_project('project = "ap-tui-test"\n')
    cmds = stepsmod.build_commands(FULL, "images", {"account_id": "123456789012"})
    tags = [a for c in cmds for a in c.argv if "/server:" in a or "/web:" in a]
    assert tags, "no image tags were built"
    for tag in tags:
        assert "ap-tui-test/" in tag
        assert "bap/" not in tag


def test_operations_follow_the_tfvars_project(tfvars_with_project):
    tfvars_with_project('project = "ap-tui-test"\n')
    ops = {o.id: o for o in stepsmod.operations(FULL)}
    assert "ap-tui-test-cluster" in ops["redeploy"].action.display()
    assert "/ecs/ap-tui-test/server" in ops["logs"].action.argv


def test_everything_falls_back_when_tfvars_sets_no_project(tfvars_with_project):
    tfvars_with_project('admin_email = "a@b.c"\n')
    assert "project=bap" in stepsmod.build_plan(FULL).by_id("bootstrap").action.display()
    ops = {o.id: o for o in stepsmod.operations(FULL)}
    assert "bap-cluster" in ops["redeploy"].action.display()


def test_empty_project_falls_back_rather_than_building_a_bare_prefix(tfvars_with_project):
    """`project = ""` must not yield names like "-cluster"."""
    tfvars_with_project('project = ""\n')
    ops = {o.id: o for o in stepsmod.operations(FULL)}
    assert "bap-cluster" in ops["redeploy"].action.display()
    assert "--cluster -cluster" not in ops["redeploy"].action.display()


# ── -backend-config is decided by the file, not by the env name ───────────────
#
# The condition used to be `env.name == "standalone"`. A second remote-state
# environment therefore never got the flag and `terraform init` died with
# "Missing Required Value ... The attribute \"bucket\" is required by the
# backend" — found during a real from-zero deploy, not by these tests, because
# they only ever exercised the known environment names.

def test_init_passes_backend_config_whenever_a_backend_hcl_exists(tmp_path, monkeypatch):
    """A remote-state env with a backend.hcl gets the flag regardless of name."""
    from dataclasses import replace

    other = replace(envmod.get("standalone"), name="another-remote")
    monkeypatch.setattr(type(other), "tf_dir", property(lambda self: tmp_path))
    (tmp_path / "backend.hcl").write_text('bucket = "b"\ndynamodb_table = "t"\n')

    argv = stepsmod.build_plan(other).by_id("init").action.argv
    assert any(a.startswith("-backend-config=") for a in argv), argv


def test_init_omits_backend_config_when_no_backend_hcl_is_present(tmp_path, monkeypatch):
    from dataclasses import replace

    other = replace(envmod.get("standalone"), name="another-remote")
    monkeypatch.setattr(type(other), "tf_dir", property(lambda self: tmp_path))
    # no backend.hcl written

    argv = stepsmod.build_plan(other).by_id("init").action.argv
    assert not any(a.startswith("-backend-config=") for a in argv), argv


# ── every command has to name the region the stack is in ────────────────────
#
# The fixture writes only `region`, so `project` keeps the env default. A
# hardcoded us-east-1 here is the kind of failure that reports success: the push
# lands in a registry the ECS task cannot pull from, and create-memory writes a
# memory the deployment cannot read while the probe still says DONE.

ACCOUNT = "123456789012"


def test_ecr_registry_follows_the_tfvars_region(tfvars_with_project):
    tfvars_with_project('region = "ap-northeast-2"\n')
    cmds = stepsmod.build_commands(FULL, "images", {"account_id": ACCOUNT})
    hosts = [a for c in cmds for a in c.argv if ".dkr.ecr." in a]
    assert hosts, "no registry hostname was built"
    for host in hosts:
        m = re.search(r"\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com", host)
        assert m and m.group(1) == "ap-northeast-2", host


def test_ecr_login_region_matches_the_registry_it_logs_into(tfvars_with_project):
    """Logging into one region and pushing to another fails at push time."""
    tfvars_with_project('region = "ap-northeast-2"\n')
    login = stepsmod.build_commands(FULL, "images", {"account_id": ACCOUNT})[0]
    assert "get-login-password --region ap-northeast-2" in " ".join(login.argv)


def test_recorded_image_uris_use_the_registry_that_was_pushed_to(tfvars_with_project):
    """app.py writes tfvars from registry_host; build_commands pushes. One source."""
    tfvars_with_project('region = "ap-northeast-2"\n')
    pushed = [a for c in stepsmod.build_commands(FULL, "images", {"account_id": ACCOUNT})
              for a in c.argv if "/server:" in a]
    assert pushed
    for image in pushed:
        assert image.startswith(stepsmod.registry_host(FULL, ACCOUNT) + "/")


def test_memory_is_created_in_the_tfvars_region(tfvars_with_project):
    tfvars_with_project('region = "ap-northeast-2"\n')
    argv = stepsmod.build_plan(FULL).by_id("memory").action.argv
    assert argv[argv.index("--region") + 1] == "ap-northeast-2"


def test_operations_target_the_tfvars_region(tfvars_with_project):
    tfvars_with_project('region = "ap-northeast-2"\n')
    ops = {o.id: o for o in stepsmod.operations(FULL)}
    argv = ops["logs"].action.argv
    assert argv[argv.index("--region") + 1] == "ap-northeast-2"
    assert "--region ap-northeast-2" in ops["redeploy"].action.display()
    assert "--region ap-northeast-1" not in ops["redeploy"].action.display()


def test_redeploy_restarts_both_ecs_services():
    """A `latest` re-push changes no task definition, so each service must be
    told explicitly — DEPLOYMENT.md restarts server and web, and so must this."""
    shown = {o.id: o for o in stepsmod.operations(FULL)}["redeploy"].action.display()
    assert "--service server" in shown
    assert "--service web" in shown


def test_memory_strategy_uses_the_cli_parameter_name():
    """create-memory takes `namespaces`; `namespaceTemplates` is rejected."""
    shown = stepsmod.build_plan(FULL).by_id("memory").action.display()
    assert '"namespaces"' in shown
    assert "namespaceTemplates" not in shown
