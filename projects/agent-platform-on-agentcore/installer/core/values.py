"""The settings model: load, validate and persist every Terraform variable.

Validation happens here rather than at apply time because apply failures arrive
minutes later, after resources exist. Cognito's password policy in particular is
cheap to check now and expensive to discover then.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from installer.core import files, hcl
from installer.core.env import Env
from installer.core.runner import Command

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Output names the .env files are filled from.
DERIVED_OUTPUTS = (
    "alb_url", "cognito_user_pool_id", "cognito_client_id", "agent_registry_id",
    "artifacts_bucket", "artifacts_table", "skills_bucket", "knowledge_bucket",
    "knowledge_table", "kb_service_role_arn", "kb_gateway_role_arn",
    "harness_execution_role_arn", "mcp_gateway_url", "cluster_name",
    "ecr_repository_urls", "usage_table_name", "guardrail_id", "guardrail_version",
    # writes._RUNTIME_KEYS maps this to EXECUTION_ROLE; it was missing here, so
    # derived() never returned it and .env kept the commented-out template line.
    "agent_runtime_role_arn",
)


@dataclass(frozen=True)
class Field:
    variable: hcl.TfVariable
    value: str
    tab: str


def _tfvars_path(env: Env) -> Path:
    return env.tfvars_path


def _unquote(literal: str) -> str:
    literal = literal.strip()
    if literal.startswith('"') and literal.endswith('"'):
        return literal[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if literal.startswith("["):
        return ", ".join(re.findall(r'"([^"]*)"', literal))
    return literal


def load_fields(env: Env) -> tuple[Field, ...]:
    path = _tfvars_path(env)
    current = hcl.read_values(path.read_text()) if path.is_file() else {}
    out = []
    for variable in hcl.read_variables(env.variables_path):
        literal = current.get(variable.name, variable.default_literal or "")
        out.append(
            Field(
                variable=variable,
                value=_unquote(literal),
                tab="required" if variable.required else "advanced",
            )
        )
    return tuple(out)


def validate(variable: hcl.TfVariable, raw: str) -> str | None:
    """Return an error/warning message, or None when the value is fine."""
    raw = raw.strip()

    if not raw:
        if not variable.has_default:
            return "필수 값입니다."
        return None

    if variable.name.endswith("_email"):
        return None if _EMAIL.match(raw) else "이메일 형식이 아닙니다."

    if variable.sensitive and variable.name.endswith("password"):
        return _password_error(raw)

    if variable.type == "number":
        try:
            int(raw)
        except ValueError:
            return "숫자여야 합니다."
        return None

    if variable.type == "bool":
        return None if raw in ("true", "false") else "true 또는 false 여야 합니다."

    if variable.type.startswith("map("):
        if raw.startswith("{") or all("=" in p for p in re.split(r"[\n,]", raw) if p.strip()):
            return None
        return "`{ key = \"value\" }` 형태의 HCL 또는 `key = value, key2 = value2` 로 입력합니다."

    if variable.name == "azs":
        items = [p for p in re.split(r"[\n,]", raw) if p.strip()]
        if len(items) != 2:
            return "ALB 는 두 개의 AZ 를 요구합니다 (쉼표로 구분)."
        return None

    return None


def _password_error(raw: str) -> str | None:
    """The pool's policy (modules/cognito): 8+ chars with upper, lower and digit.

    `require_symbols = false` there, so a symbol is not demanded here either —
    demanding one blocked saving a password Cognito would have accepted.
    """
    if len(raw) < 8:
        return "8자 이상이어야 합니다 (Cognito 정책)."
    checks = (
        (r"[a-z]", "소문자"),
        (r"[A-Z]", "대문자"),
        (r"\d", "숫자"),
    )
    missing = [label for pattern, label in checks if not re.search(pattern, raw)]
    if missing:
        return f"{', '.join(missing)}가 필요합니다 (Cognito 정책)."
    return None


def save(env: Env, edits: dict[str, str]) -> files.WriteResult:
    variables = {v.name: v for v in hcl.read_variables(env.variables_path)}
    updates = {}
    for name, raw in edits.items():
        if name not in variables:
            continue
        var_type = variables[name].type
        # Convert string values to proper Python types for render_value
        if var_type == "bool":
            value = raw.lower() in ("true", "1", "yes")
        elif var_type == "number":
            value = int(raw)
        else:
            value = raw
        updates[name] = hcl.render_value(value, var_type)
    return files.set_keys(_tfvars_path(env), updates, style="hcl")


async def derived(env: Env, sh) -> dict[str, str]:
    """Read every Terraform output the .env files are built from."""
    code, out = await sh.capture(
        Command(argv=("terraform", f"-chdir={env.tf_dir}", "output", "-json"))
    )
    if code != 0:
        return {}
    try:
        parsed = json.loads(out)
    except ValueError:
        return {}
    result = {}
    for name in DERIVED_OUTPUTS:
        if name in parsed:
            value = parsed[name].get("value")
            result[name] = value if isinstance(value, str) else json.dumps(value)
    return result
