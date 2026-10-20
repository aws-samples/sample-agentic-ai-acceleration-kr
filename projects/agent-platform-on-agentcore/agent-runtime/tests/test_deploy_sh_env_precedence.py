"""deploy.sh: inline / exported values beat .env, and names follow the stack.

DEPLOYMENT.md tells people to `export PLATFORM_API_URL=...` and run deploy.sh, and
says inline values win over .env. load_config kept only a hand-picked list of
names across `source .env`, so .env.example's blank `PLATFORM_API_URL=` erased the
export and the runtime was silently never registered (2026-10-18, us-west-2 load
test stack). The runtime name stem and its Platform tag were also fixed to `bap`
whatever the stack's `project` was.

These run the real load_config in bash, with Terraform replaced by a stub that
answers `output -raw <name>` from a table.
"""
import os
import shutil
import subprocess
import textwrap

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPLOY_SH = os.path.join(HERE, "scripts", "deploy.sh")

OUTPUTS = {
    "project": "acme",
    "region": "us-west-2",
    "mcp_gateway_url": "https://acme-gateway.example/mcp",
    "agent_runtime_role_arn": "arn:aws:iam::1:role/acme-agent-runtime",
    "guardrail_id": "gr123",
    "guardrail_version": "DRAFT",
    "alb_url": "http://acme-alb.example",
}


@pytest.fixture
def workspace(tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    scripts = tmp_path / "agent-runtime" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy(DEPLOY_SH, scripts / "deploy.sh")
    (tmp_path / "infra" / "envs" / "standalone").mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    cases = "\n".join(f'    {k}) echo -n "{v}" ;;' for k, v in OUTPUTS.items())
    (bin_dir / "terraform").write_text(textwrap.dedent("""\
        #!/usr/bin/env bash
        name="${@: -1}"
        case "$name" in
        %s
            *) exit 1 ;;
        esac
        """) % cases)
    (bin_dir / "terraform").chmod(0o755)
    return tmp_path


def _load(workspace, env_text, **env):
    (workspace / "agent-runtime" / ".env").write_text(env_text)
    script = (
        'eval "$(sed -E \'s/^main "\\$@"$//\' deploy.sh)"; load_config >/dev/null 2>&1; '
        'for v in PLATFORM AGENT_NAME REGION_NAME PLATFORM_API_URL MODEL_ID EXECUTION_ROLE '
        'GUARDRAIL_ID MCP_GATEWAY_URL; do echo "$v=${!v}"; done'
    )
    full_env = {"PATH": f"{workspace / 'bin'}:{os.environ['PATH']}", "HOME": str(workspace), **env}
    out = subprocess.run(["bash", "-c", script], cwd=workspace / "agent-runtime" / "scripts",
                         env=full_env, capture_output=True, text=True, check=True).stdout
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def test_exported_value_beats_a_blank_in_env(workspace):
    got = _load(workspace, "PLATFORM_API_URL=\nMODEL_ID=global.anthropic.claude-sonnet-5-5\n",
                PLATFORM_API_URL="http://inline.example", MODEL_ID="global.anthropic.claude-haiku-5-5")
    assert got["PLATFORM_API_URL"] == "http://inline.example"
    assert got["MODEL_ID"] == "global.anthropic.claude-haiku-5-5"


def test_env_value_beats_terraform_autofill(workspace):
    got = _load(workspace, "REGION_NAME=ap-northeast-1\nPLATFORM=bap\n")
    assert got["REGION_NAME"] == "ap-northeast-1"
    assert got["PLATFORM"] == "bap"
    assert got["AGENT_NAME"] == "bap_default"


def test_blanks_follow_the_stack(workspace):
    got = _load(workspace, "MCP_GATEWAY_URL=\nPLATFORM_API_URL=\n")
    assert got["PLATFORM"] == "acme"
    assert got["AGENT_NAME"] == "acme_default"
    assert got["REGION_NAME"] == "us-west-2"
    assert got["PLATFORM_API_URL"] == OUTPUTS["alb_url"]
    assert got["EXECUTION_ROLE"] == OUTPUTS["agent_runtime_role_arn"]
    assert got["GUARDRAIL_ID"] == "gr123"
    assert got["MCP_GATEWAY_URL"] == OUTPUTS["mcp_gateway_url"]


def test_shipped_env_example_does_not_pin_the_region_or_blank_the_api_url():
    with open(os.path.join(HERE, ".env.example"), encoding="utf-8") as fh:
        lines = [line.strip() for line in fh]
    assert not any(line.startswith("REGION_NAME=") for line in lines)
    assert not any(line.startswith("PLATFORM_API_URL=") for line in lines)
