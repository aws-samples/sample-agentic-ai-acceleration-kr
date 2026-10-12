"""
Tests for the file-writing steps.

These three steps are the ones that used to be manual copying, and each has a
specific failure mode worth pinning.

- backend: the bucket name embeds the account id, so it goes into a git-ignored
  backend.hcl rather than the tracked backend.tf.
- local_env: server/.env and agent-runtime/.env are filled from terraform
  outputs. env.example documents that copying by hand, which is where values get
  transposed. Note MEMORY_ID goes to agent-runtime only — the server does not
  read it.
- Values must map to the right key names: the outputs and the env keys do not
  share names (cognito_client_id -> COGNITO_USER_POOL_CLIENT_ID).
"""
import os
import subprocess
import sys


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from installer.core import env as envmod  # noqa: E402
from installer.core import probe  # noqa: E402
from installer.core import writes  # noqa: E402

ENV = envmod.get("standalone")

OUTPUTS = {
    "alb_url": "http://bap-alb-123.us-east-1.elb.amazonaws.com",
    "cognito_user_pool_id": "us-east-1_ABC123",
    "cognito_client_id": "4h9client",
    "agent_registry_id": "reg-xyz",
    "artifacts_bucket": "bap-artifacts",
    "artifacts_table": "agent-artifacts",
    "skills_bucket": "bap-skills",
    "knowledge_bucket": "bap-knowledge",
    "knowledge_table": "bap-knowledge",
    "kb_service_role_arn": "arn:aws:iam::1:role/kb-svc",
    "kb_gateway_role_arn": "arn:aws:iam::1:role/kb-gw",
    "harness_execution_role_arn": "arn:aws:iam::1:role/harness",
    "mcp_gateway_url": "https://gw.example/mcp",
    "usage_table_name": "bap-usage",
    "guardrail_id": "gr-abc123",
    "guardrail_version": "1",
    "agent_runtime_role_arn": "arn:aws:iam::1:role/runtime",
}


def test_output_names_map_to_the_right_env_keys():
    server, runtime = writes.env_updates_from_outputs(
        OUTPUTS, memory_id="mem-1", alb_url=OUTPUTS["alb_url"]
    )
    assert server["COGNITO_USER_POOL_ID"] == "us-east-1_ABC123"
    assert server["COGNITO_USER_POOL_CLIENT_ID"] == "4h9client"   # name differs
    assert server["AGENT_REGISTRY_ID"] == "reg-xyz"
    assert server["ARTIFACTS_BUCKET"] == "bap-artifacts"
    assert server["MCP_GATEWAY_URL"] == "https://gw.example/mcp"
    assert server["USAGE_TABLE"] == "bap-usage"


def test_guardrail_outputs_go_to_the_runtime_env():
    """deploy.sh attaches the guardrail from GUARDRAIL_ID / GUARDRAIL_VERSION in
    agent-runtime/.env; without these the installer's .env still needed two
    manual exports from terraform output (DEPLOYMENT.md, Runtime 에이전트 2))."""
    server, runtime = writes.env_updates_from_outputs(
        OUTPUTS, memory_id="mem-1", alb_url=OUTPUTS["alb_url"]
    )
    assert runtime["GUARDRAIL_ID"] == "gr-abc123"
    assert runtime["GUARDRAIL_VERSION"] == "1"
    assert "GUARDRAIL_ID" not in server


def test_the_stack_runtime_role_goes_to_the_runtime_env():
    """Without EXECUTION_ROLE deploy.sh lets `agentcore configure` create a role,
    and the runtime then lacks InvokeGateway on the platform gateway. The stack
    already made a role with it; the installer hands that one over."""
    server, runtime = writes.env_updates_from_outputs(
        OUTPUTS, memory_id="mem-1", alb_url=OUTPUTS["alb_url"]
    )
    assert runtime["EXECUTION_ROLE"] == "arn:aws:iam::1:role/runtime"
    assert "EXECUTION_ROLE" not in server


def test_memory_id_goes_to_the_runtime_only():
    """server/ has no MEMORY_ID: the runtime restores conversations, not the server."""
    server, runtime = writes.env_updates_from_outputs(
        OUTPUTS, memory_id="mem-1", alb_url=OUTPUTS["alb_url"]
    )
    assert runtime["MEMORY_ID"] == "mem-1"
    assert "MEMORY_ID" not in server


def test_platform_api_url_comes_from_the_alb():
    """deploy.sh registers the runtime through the ALB, not the server directly."""
    _, runtime = writes.env_updates_from_outputs(
        OUTPUTS, memory_id="mem-1", alb_url=OUTPUTS["alb_url"]
    )
    assert runtime["PLATFORM_API_URL"] == OUTPUTS["alb_url"]


def test_auth_enforced_is_not_disabled_for_a_deployed_environment():
    """AUTH_ENFORCED=false is a local-only escape hatch."""
    server, _ = writes.env_updates_from_outputs(
        OUTPUTS, memory_id=None, alb_url=OUTPUTS["alb_url"]
    )
    assert server.get("AUTH_ENFORCED") != "false"


def test_missing_outputs_are_skipped_not_written_blank():
    server, _ = writes.env_updates_from_outputs(
        {"cognito_user_pool_id": "us-east-1_X"}, memory_id=None, alb_url=""
    )
    assert server["COGNITO_USER_POOL_ID"] == "us-east-1_X"
    assert "AGENT_REGISTRY_ID" not in server


# ── backend writing ─────────────────────────────────────────────────────────

class FakeShell:
    def __init__(self, table):
        self.table = table

    async def capture(self, cmd):
        line = cmd.display()
        for needle, response in self.table.items():
            if needle in line:
                return response
        return (127, "")


def test_standalone_writes_backend_hcl(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("backend.hcl\n")
    target = tmp_path / "backend.hcl"
    monkeypatch.setattr(writes, "_backend_hcl_path", lambda e: target)
    monkeypatch.setattr(probe, "_tfvars_path", lambda e: tmp_path / "nope.tfvars")

    import asyncio
    result = asyncio.run(writes.write_backend(ENV, FakeShell({}), "123456789012"))
    text = target.read_text()
    assert "bap-tfstate-123456789012" in text
    assert "bap-tflock" in text
    assert result.warning is None


def test_account_id_is_read_from_sts():
    import asyncio
    sh = FakeShell({"get-caller-identity": (0, "123456789012\n")})
    assert asyncio.run(writes.account_id(sh)) == "123456789012"


def test_memory_id_is_looked_up_by_name():
    """Realistic fixture: id is name plus 10-char random suffix."""
    import asyncio
    sh = FakeShell({"list-memories": (0, "bap_conversations_default-PqRsT24680\n")})
    assert asyncio.run(writes.memory_id(ENV, sh)) == "bap_conversations_default-PqRsT24680"


def test_memory_id_queries_the_name_the_memory_step_creates():
    """The lookup once kept a retired name after the create step was renamed, so
    MEMORY_ID stayed empty for a memory that existed. FakeShell answers any
    query, so only comparing against the step's own --name catches that."""
    import asyncio
    from installer.core import steps

    create = steps.build_plan(ENV).by_id("memory").action.argv
    name = create[create.index("--name") + 1]

    seen: list[tuple[str, ...]] = []

    class RecordingShell(FakeShell):
        async def capture(self, cmd):
            seen.append(cmd.argv)
            return await super().capture(cmd)

    asyncio.run(writes.memory_id(ENV, RecordingShell({"list-memories": (0, "\n")})))
    query = seen[0][seen[0].index("--query") + 1]
    assert f"starts_with(id, '{name}-')" in query


def test_memory_id_is_none_when_absent():
    import asyncio
    assert asyncio.run(writes.memory_id(ENV, FakeShell({"list-memories": (0, "\n")}))) is None


def test_memory_lookup_targets_the_tfvars_region(tmp_path, monkeypatch):
    """list-memories in another region returns nothing, so agent-runtime/.env
    would get an empty MEMORY_ID for a memory that exists."""
    import asyncio

    tfvars = tmp_path / "terraform.tfvars"
    tfvars.write_text('region = "ap-northeast-2"\n')
    monkeypatch.setattr(writes.probe, "_tfvars_path", lambda e: tfvars)
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    seen: list[str] = []

    class RecordingShell(FakeShell):
        async def capture(self, cmd):
            seen.append(cmd.display())
            return await super().capture(cmd)

    asyncio.run(writes.memory_id(ENV, RecordingShell({"list-memories": (0, "\n")})))
    assert seen and "--region ap-northeast-2" in seen[0]


# ── settings the user chose must reach the local .env too ────────────────────
#
# Not everything comes from terraform output. `bedrock_model_id` and `region`
# are picked in the settings form, and before this was wired up
# the local .env kept whatever env.example shipped — so the model the user chose
# was NOT the model the local server used. Verified on a live checkout where
# server/.env held claude-3-5-sonnet while tfvars specified haiku-4-5.

def test_chosen_model_reaches_both_env_files():
    server, runtime = writes.env_updates_from_outputs(
        OUTPUTS, memory_id=None, alb_url="",
        tfvars={"bedrock_model_id": "global.anthropic.claude-haiku-4-5-20251001-v1:0"},
    )
    assert server["BEDROCK_MODEL_ID"].endswith("haiku-4-5-20251001-v1:0")
    # the runtime reads MODEL_ID, not BEDROCK_MODEL_ID
    assert runtime["MODEL_ID"].endswith("haiku-4-5-20251001-v1:0")


def test_region_maps_to_each_components_own_key_name():
    """server reads AWS_REGION; agent-runtime reads REGION_NAME."""
    server, runtime = writes.env_updates_from_outputs(
        OUTPUTS, memory_id=None, alb_url="", tfvars={"region": "us-west-2"},
    )
    assert server["AWS_REGION"] == "us-west-2"
    assert runtime["REGION_NAME"] == "us-west-2"


def test_blank_chosen_values_are_not_written():
    """An empty choice must not overwrite a value already in .env with "";
    absent is different from deliberately blank here."""
    server, runtime = writes.env_updates_from_outputs(
        OUTPUTS, memory_id=None, alb_url="",
        tfvars={"region": "", "bedrock_model_id": ""},
    )
    assert "AWS_REGION" not in server
    assert "BEDROCK_MODEL_ID" not in server


def test_missing_tfvars_argument_still_works():
    """Older call sites pass no tfvars at all."""
    server, runtime = writes.env_updates_from_outputs(OUTPUTS, memory_id="m", alb_url="u")
    assert "COGNITO_USER_POOL_ID" in server
    assert runtime["MEMORY_ID"] == "m"


def test_every_runtime_env_source_is_a_derived_output():
    """A key in _RUNTIME_KEYS/_SERVER_KEYS that derived() never fetches is
    silently skipped — EXECUTION_ROLE stayed unset on a real run this way."""
    from installer.core import values

    for name in (*writes._RUNTIME_KEYS, *writes._SERVER_KEYS):
        assert name in values.DERIVED_OUTPUTS, name
