"""
Tests for state probes.

The installer stores no progress file — it re-derives every step's status from
AWS and the filesystem each time the dashboard opens. That is what keeps it
honest when someone runs terraform by hand between sessions, and it makes these
probes the load-bearing part of the design.

The distinction the tests exist to protect is three-valued. A probe that cannot
tell must answer UNKNOWN, never DONE: reporting a step complete because the
credentials expired would skip real work and fail later, further from the cause.
- resource absent      -> PENDING (do the step)
- resource present     -> DONE
- cannot tell          -> UNKNOWN (say so; do not guess)
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from installer.core import env as envmod  # noqa: E402
from installer.core import probe  # noqa: E402
from installer.core.probe import State  # noqa: E402


class FakeShell:
    """Answers commands from a table keyed by a substring of the command line."""

    def __init__(self, table):
        self.table = table
        self.calls: list[str] = []

    async def capture(self, cmd):
        line = cmd.display()
        self.calls.append(line)
        for needle, response in self.table.items():
            if needle in line:
                return response
        return (127, f"unexpected command: {line}")


def run(coro):
    return asyncio.run(coro)


ENV = envmod.get("standalone")


# ── the three-valued contract ───────────────────────────────────────────────

def test_state_backend_resolves_the_account_id_into_the_bucket_name(tmp_path, monkeypatch):
    """S3 rejects a wildcard bucket name at parameter validation, so the account
    id must be looked up and the real name assembled."""
    # Ignore the developer's real tfvars: a non-default `project` there renames
    # the bucket and made this test fail on a checkout mid-deployment.
    monkeypatch.setattr(probe, "_tfvars_path", lambda e: tmp_path / "nope.tfvars")
    sh = FakeShell({
        "get-caller-identity": (0, "123456789012\n"),
        "head-bucket": (0, ""),
        "describe-table": (0, '{"Table":{}}'),
    })
    assert run(probe.state_backend(ENV, sh)).state is State.DONE
    head = next(c for c in sh.calls if "head-bucket" in c)
    assert "bap-tfstate-123456789012" in head
    assert "*" not in head


def test_state_backend_unknown_when_the_account_id_cannot_be_read():
    sh = FakeShell({"get-caller-identity": (255, "Unable to locate credentials")})
    assert run(probe.state_backend(ENV, sh)).state is State.UNKNOWN


def test_state_backend_pending_when_bucket_missing():
    sh = FakeShell({
        "get-caller-identity": (0, "123456789012\n"),
        "head-bucket": (255, "Not Found"),
        "describe-table": (0, ""),
    })
    assert run(probe.state_backend(ENV, sh)).state is State.PENDING


def test_state_backend_unknown_when_credentials_are_bad():
    """AccessDenied means "cannot tell", not "not there"."""
    sh = FakeShell({
        "get-caller-identity": (0, "123456789012\n"),
        "head-bucket": (255, "An error occurred (AccessDenied) when calling HeadBucket"),
    })
    outcome = run(probe.state_backend(ENV, sh))
    assert outcome.state is State.UNKNOWN
    assert "AccessDenied" in outcome.detail or "권한" in outcome.detail


def test_state_backend_unknown_on_expired_token():
    sh = FakeShell({
        "get-caller-identity": (0, "123456789012\n"),
        "head-bucket": (255, "ExpiredToken: the security token expired"),
    })
    assert run(probe.state_backend(ENV, sh)).state is State.UNKNOWN


def test_tools_present_does_not_exec_a_shell_builtin(monkeypatch):
    """`command -v` is a shell builtin: exec'ing it raises FileNotFoundError.
    Tool presence is resolved in-process instead."""
    monkeypatch.setattr(probe.shutil, "which", lambda tool: "/usr/bin/" + tool)
    sh = FakeShell({"get-caller-identity": (0, "123456789012\n")})
    assert run(probe.tools_present(ENV, sh)).state is State.DONE
    assert not any("command -v" in c for c in sh.calls)


def test_tools_present_pending_lists_the_missing_tools(monkeypatch):
    monkeypatch.setattr(
        probe.shutil, "which", lambda tool: None if tool == "docker" else "/usr/bin/" + tool
    )
    outcome = run(probe.tools_present(ENV, FakeShell({})))
    assert outcome.state is State.PENDING
    assert "docker" in outcome.detail


# ── filesystem probes ───────────────────────────────────────────────────────

def test_backend_configured_checks_backend_hcl_for_standalone(tmp_path, monkeypatch):
    hcl_path = tmp_path / "backend.hcl"
    monkeypatch.setattr(probe, "_backend_hcl_path", lambda e: hcl_path)
    assert run(probe.backend_configured(ENV, FakeShell({}))).state is State.PENDING
    hcl_path.write_text('bucket = "bap-tfstate-123456789012"\n')
    assert run(probe.backend_configured(ENV, FakeShell({}))).state is State.DONE


def test_tfvars_complete_requires_every_variable_without_a_default(tmp_path, monkeypatch):
    tfvars = tmp_path / "terraform.tfvars"
    tfvars.write_text('admin_email = "a@b.c"\n')       # the other four are missing
    monkeypatch.setattr(probe, "_tfvars_path", lambda e: tfvars)
    outcome = run(probe.tfvars_complete(ENV, FakeShell({})))
    assert outcome.state is State.PENDING
    assert "admin_password" in outcome.detail


def test_tfvars_complete_done_when_all_required_present(tmp_path, monkeypatch):
    tfvars = tmp_path / "terraform.tfvars"
    tfvars.write_text(
        'admin_email = "a@b.c"\nadmin_password = "x"\n'
        'user_email = "u@b.c"\nuser_password = "y"\n'
        'bedrock_model_id = "anthropic.claude-3-5-sonnet-20241022-v2:0"\n'
    )
    monkeypatch.setattr(probe, "_tfvars_path", lambda e: tfvars)
    assert run(probe.tfvars_complete(ENV, FakeShell({}))).state is State.DONE


def test_tfvars_complete_pending_when_file_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "_tfvars_path", lambda e: tmp_path / "nope.tfvars")
    assert run(probe.tfvars_complete(ENV, FakeShell({}))).state is State.PENDING


# ── terraform-output-driven probes ──────────────────────────────────────────

def test_first_apply_done_when_ecr_output_has_values_and_plan_is_clean():
    """ECR outputs AND a converged plan. Both are required — see below."""
    sh = FakeShell({
        "output": (0, '{"server":"123.dkr.ecr.us-east-1.amazonaws.com/x/server"}'),
        "plan": (0, "No changes."),
    })
    assert run(probe.first_apply_done(ENV, sh)).state is State.DONE


def test_first_apply_pending_when_output_is_empty():
    sh = FakeShell({"output": (1, "Warning: No outputs found")})
    assert run(probe.first_apply_done(ENV, sh)).state is State.PENDING


def test_images_pushed_done_when_both_repositories_have_the_tag():
    sh = FakeShell({"describe-images": (0, '{"imageDetails":[{"imageTags":["v1"]}]}')})
    assert run(probe.images_pushed(ENV, sh)).state is State.DONE


def test_images_pushed_pending_when_repository_is_empty():
    sh = FakeShell({"describe-images": (254, "ImageNotFoundException")})
    assert run(probe.images_pushed(ENV, sh)).state is State.PENDING


def test_services_running_pending_when_desired_count_is_zero():
    """A parked environment is pending, not done."""
    sh = FakeShell({"describe-services": (0, '{"services":[{"desiredCount":0,"runningCount":0}]}')})
    assert run(probe.services_running(ENV, sh)).state is State.PENDING


def test_services_running_discriminates_park_from_not_yet_up():
    """desiredCount=0, runningCount=1 is a real state (parked with lingering task).
    This fixture uniquely exercises the desiredCount < 1 branch; the 0/0 case above
    cannot discriminate it because runningCount < 1 also rejects 0/0."""
    sh = FakeShell({"describe-services": (0, '{"services":[{"desiredCount":0,"runningCount":1}]}')})
    outcome = run(probe.services_running(ENV, sh))
    assert outcome.state is State.PENDING
    assert "park" in outcome.detail


def test_services_running_done_when_tasks_are_up():
    sh = FakeShell({"describe-services": (0, '{"services":[{"desiredCount":1,"runningCount":1}]}')})
    assert run(probe.services_running(ENV, sh)).state is State.DONE


def test_memory_created_done_when_named_memory_exists():
    """Realistic fixture: id is name plus 10-char random suffix."""
    sh = FakeShell({"list-memories": (0, "agent_platform_conversations-PqRsT24680\n")})
    assert run(probe.memory_created(ENV, sh)).state is State.DONE


def test_memory_created_pending_when_query_returns_nothing():
    sh = FakeShell({"list-memories": (0, "\n")})
    assert run(probe.memory_created(ENV, sh)).state is State.PENDING


def test_memory_created_unknown_when_the_api_is_unavailable():
    sh = FakeShell({"list-memories": (255, "Could not connect to the endpoint URL")})
    assert run(probe.memory_created(ENV, sh)).state is State.UNKNOWN


def test_memory_created_uses_starts_with_not_name_filter(monkeypatch):
    """The query must use starts_with(id, ...) not name== because list-memories
    does not return a name field. This test prevents regression to the bug."""
    sh = FakeShell({"list-memories": (0, "bap_conversations_default-PqRsT24680\n")})
    outcome = run(probe.memory_created(ENV, sh))
    assert outcome.state is State.DONE
    # Verify the command contains starts_with and bap_conversations_default, not name==
    cmd_line = next(c for c in sh.calls if "list-memories" in c)
    assert "starts_with" in cmd_line
    assert "bap_conversations_default" in cmd_line
    assert "name==" not in cmd_line


# ── project resolution ──────────────────────────────────────────────────────

def test_state_backend_uses_project_from_tfvars(tmp_path, monkeypatch):
    """When tfvars sets project to a different value, probes should use that,
    not the env default."""
    tfvars = tmp_path / "terraform.tfvars"
    tfvars.write_text('project = "ap-tui-test"\n')
    monkeypatch.setattr(probe, "_tfvars_path", lambda e: tfvars)

    sh = FakeShell({
        "get-caller-identity": (0, "123456789012\n"),
        "head-bucket": (0, ""),
        "describe-table": (0, '{"Table":{}}'),
    })
    outcome = run(probe.state_backend(ENV, sh))
    assert outcome.state is State.DONE
    # Verify the bucket name uses ap-tui-test, not bap
    head = next(c for c in sh.calls if "head-bucket" in c)
    assert "ap-tui-test-tfstate-123456789012" in head
    assert "bap" not in head


def test_state_backend_falls_back_to_env_project_when_tfvars_absent(tmp_path, monkeypatch):
    """When tfvars is absent or has no project, use env.project."""
    tfvars = tmp_path / "terraform.tfvars"
    tfvars.write_text('')  # no project key
    monkeypatch.setattr(probe, "_tfvars_path", lambda e: tfvars)

    sh = FakeShell({
        "get-caller-identity": (0, "123456789012\n"),
        "head-bucket": (0, ""),
        "describe-table": (0, '{"Table":{}}'),
    })
    outcome = run(probe.state_backend(ENV, sh))
    assert outcome.state is State.DONE
    # Verify the bucket name uses bap (env default)
    head = next(c for c in sh.calls if "head-bucket" in c)
    assert "bap-tfstate-123456789012" in head


def test_state_backend_falls_back_when_project_is_empty_string(tmp_path, monkeypatch):
    """Empty project in tfvars should not create names like '-tfstate-...'."""
    tfvars = tmp_path / "terraform.tfvars"
    tfvars.write_text('project = ""\n')
    monkeypatch.setattr(probe, "_tfvars_path", lambda e: tfvars)

    sh = FakeShell({
        "get-caller-identity": (0, "123456789012\n"),
        "head-bucket": (0, ""),
        "describe-table": (0, '{"Table":{}}'),
    })
    outcome = run(probe.state_backend(ENV, sh))
    assert outcome.state is State.DONE
    # Verify the bucket name uses bap, not empty prefix
    head = next(c for c in sh.calls if "head-bucket" in c)
    assert "bap-tfstate-123456789012" in head
    assert "-tfstate" in head  # should have the prefix


def test_images_pushed_uses_project_from_tfvars(tmp_path, monkeypatch):
    """ECR repository names should use project from tfvars."""
    tfvars = tmp_path / "terraform.tfvars"
    tfvars.write_text('project = "ap-tui-test"\n')
    monkeypatch.setattr(probe, "_tfvars_path", lambda e: tfvars)

    sh = FakeShell({"describe-images": (0, '{"imageDetails":[{"imageTags":["v1"]}]}')})
    outcome = run(probe.images_pushed(ENV, sh))
    assert outcome.state is State.DONE
    # Verify repo names use ap-tui-test
    cmd = next(c for c in sh.calls if "describe-images" in c)
    assert "ap-tui-test/server" in cmd or "ap-tui-test/web" in cmd
    assert "bap" not in cmd


def test_services_running_uses_project_from_tfvars(tmp_path, monkeypatch):
    """ECS cluster name should use project from tfvars."""
    tfvars = tmp_path / "terraform.tfvars"
    tfvars.write_text('project = "ap-tui-test"\n')
    monkeypatch.setattr(probe, "_tfvars_path", lambda e: tfvars)

    sh = FakeShell({"describe-services": (0, '{"services":[{"desiredCount":1,"runningCount":1}]}')})
    outcome = run(probe.services_running(ENV, sh))
    assert outcome.state is State.DONE
    # Verify cluster name uses ap-tui-test
    cmd = next(c for c in sh.calls if "describe-services" in c)
    assert "ap-tui-test-cluster" in cmd
    assert "bap-cluster" not in cmd


# ── exit code 0 is not proof (verified against real AWS) ─────────────────────
#
# An ECR repository that exists but holds no images answers exit 0 with
# {"imageDetails": []}. Trusting the exit code alone reported DONE for a push
# that never happened — the exact failure the three-valued contract exists to
# prevent. Confirmed by creating an empty repository in the live account.

def test_images_pushed_pending_when_repository_exists_but_is_empty():
    sh = FakeShell({"describe-images": (0, '{\n    "imageDetails": []\n}')})
    outcome = run(probe.images_pushed(ENV, sh))
    assert outcome.state is State.PENDING
    assert "이미지가 없습니다" in outcome.detail


def test_images_pushed_unknown_when_describe_images_body_is_malformed():
    sh = FakeShell({"describe-images": (0, "not json at all")})
    assert run(probe.images_pushed(ENV, sh)).state is State.UNKNOWN


def test_images_pushed_distinguishes_missing_repo_from_empty_repo():
    """Different causes need different messages: create the repo vs push to it."""
    missing = run(probe.images_pushed(ENV, FakeShell({"describe-images": (254, "RepositoryNotFoundException")})))
    empty = run(probe.images_pushed(ENV, FakeShell({"describe-images": (0, '{"imageDetails": []}')})))
    assert missing.state is empty.state is State.PENDING
    assert missing.detail != empty.detail


def test_first_apply_pending_when_output_is_an_empty_map():
    sh = FakeShell({"output": (0, "{}")})
    assert run(probe.first_apply_done(ENV, sh)).state is State.PENDING


def test_first_apply_unknown_when_output_is_malformed():
    sh = FakeShell({"output": (0, "{broken json")})
    assert run(probe.first_apply_done(ENV, sh)).state is State.UNKNOWN


# ── transient AWS failures must not read as "not deployed" ───────────────────
#
# The undecidable list was originally an allow-list, which leaks: throttling,
# 5xx, connect timeouts and TLS errors are all real AWS responses that no such
# list enumerates, and every one landed in PENDING — telling the user to redo
# work that may already be done. The logic is now inverted: only messages that
# genuinely mean absence give PENDING, everything else is UNKNOWN.
#
# The not-found strings below are copied from real responses on a live account,
# not invented, so a wording change shows up here rather than in the field.

_REAL_NOT_FOUND = {
    "ecr": "An error occurred (RepositoryNotFoundException) when calling the "
           "DescribeImages operation: The repository with name 'x/server' does not "
           "exist in the registry with id '123456789012'",
    "s3": "An error occurred (404) when calling the HeadBucket operation: Not Found",
    "dynamodb": "An error occurred (ResourceNotFoundException) when calling the "
                "DescribeTable operation: Requested resource not found: Table: t not found",
    "ecs": "An error occurred (ClusterNotFoundException) when calling the "
           "DescribeServices operation: Cluster not found.",
}

_TRANSIENT = (
    "ThrottlingException: Rate exceeded",
    "ServiceUnavailable: The service is unavailable",
    "An error occurred (InternalError) when calling the DescribeImages operation",
    "botocore.exceptions.ConnectTimeoutError: Connect timeout on endpoint URL",
    "SSLError: certificate verify failed",
)


@pytest.mark.parametrize("message", _TRANSIENT)
def test_transient_failure_is_unknown_not_pending(message):
    outcome = run(probe.images_pushed(ENV, FakeShell({"describe-images": (255, message)})))
    assert outcome.state is State.UNKNOWN, f"{message!r} must not read as absence"


def test_real_repository_not_found_is_pending():
    sh = FakeShell({"describe-images": (254, _REAL_NOT_FOUND["ecr"])})
    assert run(probe.images_pushed(ENV, sh)).state is State.PENDING


def test_real_missing_bucket_is_pending():
    sh = FakeShell({
        "get-caller-identity": (0, "123456789012\n"),
        "head-bucket": (255, _REAL_NOT_FOUND["s3"]),
    })
    assert run(probe.state_backend(ENV, sh)).state is State.PENDING


def test_real_missing_lock_table_is_pending():
    sh = FakeShell({
        "get-caller-identity": (0, "123456789012\n"),
        "head-bucket": (0, ""),
        "describe-table": (255, _REAL_NOT_FOUND["dynamodb"]),
    })
    assert run(probe.state_backend(ENV, sh)).state is State.PENDING


def test_real_missing_cluster_is_pending():
    sh = FakeShell({"describe-services": (255, _REAL_NOT_FOUND["ecs"])})
    assert run(probe.services_running(ENV, sh)).state is State.PENDING


def test_unknown_failure_detail_carries_the_original_message():
    """An uninterpreted error must still show what AWS said."""
    sh = FakeShell({"describe-images": (255, "ThrottlingException: Rate exceeded")})
    assert "Throttling" in run(probe.images_pushed(ENV, sh)).detail


# ── ECR existing is not proof the apply finished ─────────────────────────────
#
# A real from-zero run failed on the NAT gateway (EIP quota) AFTER creating the
# ECR repositories. This probe only looked at the ECR output, so it reported 완료
# for an apply that had errored with ~40 of 136 resources missing. Terraform
# itself knows the difference: a converged state produces an empty plan, which
# `-detailed-exitcode` reports as 0 and pending changes as 2.

def test_first_apply_pending_when_plan_still_has_changes():
    sh = FakeShell({
        "output": (0, '{"server":"123.dkr.ecr.us-east-1.amazonaws.com/x/server"}'),
        "plan": (2, "Plan: 40 to add, 0 to change, 0 to destroy."),
    })
    outcome = run(probe.first_apply_done(ENV, sh))
    assert outcome.state is State.PENDING
    assert "끝나지 않았습니다" in outcome.detail


def test_first_apply_unknown_when_plan_itself_errors():
    """Exit 1 from plan means we cannot tell, which is not the same as absent."""
    sh = FakeShell({
        "output": (0, '{"server":"123.dkr.ecr.us-east-1.amazonaws.com/x/server"}'),
        "plan": (1, "Error: Invalid provider configuration"),
    })
    assert run(probe.first_apply_done(ENV, sh)).state is State.UNKNOWN


def test_first_apply_uses_detailed_exitcode():
    """Without the flag, plan exits 0 whether or not changes are pending."""
    sh = FakeShell({
        "output": (0, '{"server":"x"}'),
        "plan": (0, "No changes."),
    })
    run(probe.first_apply_done(ENV, sh))
    plan_call = next(c for c in sh.calls if " plan " in c)
    assert "-detailed-exitcode" in plan_call


def test_probes_look_in_the_region_the_stack_was_deployed_to(tmp_path, monkeypatch):
    """A probe reading another region answers PENDING for work that was done,
    or DONE for work that landed somewhere the deployment cannot reach."""
    tfvars = tmp_path / "terraform.tfvars"
    tfvars.write_text('region = "ap-northeast-2"\n')
    monkeypatch.setattr(probe, "_tfvars_path", lambda e: tfvars)
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    argv = probe._aws(ENV, "sts", "get-caller-identity").argv
    assert argv[argv.index("--region") + 1] == "ap-northeast-2"


# ── first apply: plan with the first apply's own variables ────────────────────

def test_first_apply_plan_uses_zero_tasks_and_placeholder_images():
    """Without these the plan always wants desired_count 0->1 (and, after the
    images step, a new task definition), so a finished apply read 대기 forever."""
    sh = FakeShell({
        "output": (0, '{"server":"x"}'),
        "plan": (0, "No changes."),
    })
    assert run(probe.first_apply_done(ENV, sh)).state is State.DONE
    plan_call = next(c for c in sh.calls if " plan " in c)
    assert "desired_count=0" in plan_call
    assert "server_image=public.ecr.aws" in plan_call
    assert "web_image=public.ecr.aws" in plan_call


def test_first_apply_is_done_once_services_run_with_tasks():
    """After the second apply the plan would only show 1->0; services running
    prove the first apply converged."""
    sh = FakeShell({
        # describe-services carries `--output text`, so key the ECR answer on the
        # terraform form to keep the two apart.
        "output -json": (0, '{"server":"x"}'),
        "describe-services": (0, "1\t1"),
        "plan": (2, "Plan: 0 to add, 2 to change, 0 to destroy."),
    })
    assert run(probe.first_apply_done(ENV, sh)).state is State.DONE


def test_first_apply_pending_detail_carries_the_plan_summary():
    sh = FakeShell({
        "output": (0, '{"server":"x"}'),
        "plan": (2, "...\nPlan: 40 to add, 0 to change, 0 to destroy.\n"),
    })
    out = run(probe.first_apply_done(ENV, sh))
    assert out.state is State.PENDING
    assert "40 to add" in out.detail


# ── Transaction Search: the destination is what gates span export ─────────────

def test_transaction_search_done_when_destination_is_active_cloudwatch_logs():
    sh = FakeShell({"get-trace-segment-destination":
                    (0, '{"Destination":"CloudWatchLogs","Status":"ACTIVE"}')})
    assert run(probe.transaction_search_enabled(ENV, sh)).state is State.DONE


def test_transaction_search_pending_while_activating():
    sh = FakeShell({"get-trace-segment-destination":
                    (0, '{"Destination":"CloudWatchLogs","Status":"PENDING"}')})
    out = run(probe.transaction_search_enabled(ENV, sh))
    assert out.state is State.PENDING
    assert "PENDING" in out.detail


def test_transaction_search_pending_shows_the_full_cli_recipe():
    """CLI enablement needs the Logs resource policy first; without it the
    xray call fails with AccessDenied on aws/spans — hit on a real account."""
    sh = FakeShell({"get-trace-segment-destination":
                    (0, '{"Destination":"XRay","Status":"ACTIVE"}')})
    out = run(probe.transaction_search_enabled(ENV, sh))
    assert out.state is State.PENDING
    assert "put-resource-policy" in out.detail
    assert "update-trace-segment-destination" in out.detail
    assert "--region" in out.detail
