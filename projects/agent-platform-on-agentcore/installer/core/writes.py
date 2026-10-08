"""The steps that write files instead of running a command.

This is the copying that used to be manual: terraform outputs into
server/.env and agent-runtime/.env, and the account id into the backend config.
env.example documents the manual version; transposing a value there is exactly
the failure this removes.
"""
from __future__ import annotations

from pathlib import Path

from installer.core import files, probe
from installer.core.env import REPO_ROOT, Env
from installer.core.runner import Command

# terraform output name -> server/.env key. The names differ, which is why this
# mapping is explicit rather than an upper() of the output name.
_SERVER_KEYS = {
    "cognito_user_pool_id": "COGNITO_USER_POOL_ID",
    "cognito_client_id": "COGNITO_USER_POOL_CLIENT_ID",
    "agent_registry_id": "AGENT_REGISTRY_ID",
    "artifacts_bucket": "ARTIFACTS_BUCKET",
    "artifacts_table": "ARTIFACTS_TABLE",
    "skills_bucket": "SKILLS_BUCKET",
    "knowledge_bucket": "KNOWLEDGE_BUCKET",
    "knowledge_table": "KNOWLEDGE_TABLE",
    "kb_service_role_arn": "KB_SERVICE_ROLE_ARN",
    "kb_gateway_role_arn": "KB_GATEWAY_ROLE_ARN",
    "harness_execution_role_arn": "HARNESS_EXECUTION_ROLE_ARN",
    "mcp_gateway_url": "MCP_GATEWAY_URL",
    "usage_table_name": "USAGE_TABLE",
}

# deploy.sh reads GUARDRAIL_ID / GUARDRAIL_VERSION / EXECUTION_ROLE from .env;
# DEPLOYMENT.md has the user export them from terraform output by hand. Filling
# them here is what lets the installer's .env go straight into
# `./scripts/deploy.sh` — and EXECUTION_ROLE is what makes the runtime use the
# stack's role (which already has InvokeGateway) instead of one the toolkit
# creates on the fly.
_RUNTIME_KEYS = {
    "mcp_gateway_url": "MCP_GATEWAY_URL",
    "guardrail_id": "GUARDRAIL_ID",
    "guardrail_version": "GUARDRAIL_VERSION",
    "agent_runtime_role_arn": "EXECUTION_ROLE",
}


def _backend_hcl_path(env: Env) -> Path:
    return env.tf_dir / "backend.hcl"


async def account_id(sh, env: Env | None = None) -> str:
    """Raising wrapper over probe.account_id, which returns None on failure.

    The probes must degrade to UNKNOWN; the write steps cannot proceed at all
    without an account id, so here it is an error.
    """
    account, raw = await probe.account_id(sh, env)
    if account is None:
        raise RuntimeError(f"계정 ID 를 조회할 수 없습니다: {raw.strip()[:200]}")
    return account


async def memory_id(env: Env, sh) -> str | None:
    """The memory the `memory` step created, for agent-runtime/.env.

    .env carries one MEMORY_ID and its AGENT_MODULE defaults to `default`, so it
    names that agent's memory; agents you add pass their own at deploy time.
    """
    code, out = await sh.capture(
        Command(argv=("aws", "bedrock-agentcore-control", "list-memories",
                      "--query", f"memories[?starts_with(id, '{probe.MEMORY_NAME}-')].id",
                      "--output", "text", "--region", probe.resolve_region(env)))
    )
    if code != 0 or not out.strip():
        return None
    return out.strip().split()[0]


async def write_backend(env: Env, sh, account: str) -> files.WriteResult:
    """Write the git-ignored backend.hcl that names the account's state bucket."""
    project, _ = probe.resolve_project(env)
    body = (
        f'bucket         = "{project}-tfstate-{account}"\n'
        f'dynamodb_table = "{project}-tflock"\n'
    )
    return files.write_secret_file(_backend_hcl_path(env), body)


# tfvars variable -> the .env key that must carry the same value. These are not
# terraform outputs: the user chooses them in the settings form, and without this
# the local .env keeps whatever the template shipped — so the model and region
# the user picked would silently not be the ones used.
_TFVARS_TO_SERVER = {
    "bedrock_model_id": "BEDROCK_MODEL_ID",
    "region": "AWS_REGION",
}
_TFVARS_TO_RUNTIME = {
    "bedrock_model_id": "MODEL_ID",
    "region": "REGION_NAME",
}


def env_updates_from_outputs(
    outputs: dict[str, str],
    memory_id: str | None,
    alb_url: str,
    tfvars: dict[str, str] | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Split terraform outputs into server/.env and agent-runtime/.env updates.

    `tfvars` carries the settings the user chose (already unquoted); values the
    user picked have to reach the local .env too, not just the deployed tasks.
    """
    server = {
        key: outputs[name] for name, key in _SERVER_KEYS.items() if outputs.get(name)
    }
    runtime = {
        key: outputs[name] for name, key in _RUNTIME_KEYS.items() if outputs.get(name)
    }

    for source, mapping, target in (
        (_TFVARS_TO_SERVER, tfvars or {}, server),
        (_TFVARS_TO_RUNTIME, tfvars or {}, runtime),
    ):
        for var, key in source.items():
            value = mapping.get(var, "")
            if value:
                target[key] = value

    if memory_id:
        # The runtime restores conversations from Memory; the server does not.
        runtime["MEMORY_ID"] = memory_id
    if alb_url:
        # deploy.sh registers through the ALB: the server task is not reachable
        # from outside the VPC, and web proxies /api.
        runtime["PLATFORM_API_URL"] = alb_url
    return server, runtime


async def write_local_env(env: Env, sh, mem_id: str | None) -> tuple[files.WriteResult, ...]:
    from installer.core import values

    outputs = await values.derived(env, sh)
    chosen = {f.variable.name: f.value for f in values.load_fields(env)}
    server_updates, runtime_updates = env_updates_from_outputs(
        outputs, mem_id, outputs.get("alb_url", ""), chosen
    )

    results = []
    pairs = (
        (REPO_ROOT / "server" / ".env", REPO_ROOT / "server" / "env.example", server_updates),
        (REPO_ROOT / "agent-runtime" / ".env",
         REPO_ROOT / "agent-runtime" / ".env.example", runtime_updates),
    )
    for target, template, updates in pairs:
        if not updates:
            continue
        if not target.exists() and template.exists():
            files.write_secret_file(target, template.read_text())
        results.append(files.set_keys(target, updates, style="env"))
    return tuple(results)
