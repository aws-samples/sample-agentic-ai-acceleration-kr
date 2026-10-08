"""Environment definition.

The installer deploys one Terraform environment, infra/envs/standalone. See
infra/README.md for the manual procedure the steps mirror.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from installer.core import hcl

# installer/core/env.py -> installer/core -> installer -> repo root
REPO_ROOT = Path(__file__).resolve().parents[2]

# Matches the `region` default in infra/envs/standalone/variables.tf. Only reached
# when neither tfvars nor the shell says anything.
_DEFAULT_REGION = "ap-northeast-1"


@dataclass(frozen=True)
class Env:
    name: str
    project: str

    @property
    def tf_dir(self) -> Path:
        return REPO_ROOT / "infra" / "envs" / self.name

    @property
    def tfvars_path(self) -> Path:
        return self.tf_dir / "terraform.tfvars"

    @property
    def variables_path(self) -> Path:
        return self.tf_dir / "variables.tf"


ALL: tuple[Env, ...] = (
    Env(name="standalone", project="bap"),
)

_BY_NAME = {e.name: e for e in ALL}


def get(name: str) -> Env:
    return _BY_NAME[name]


def tfvars_path(env: Env) -> Path:
    """Indirected so tests can point it at a temp file."""
    return env.tfvars_path


def resolve_project(env: Env, path: Path | None = None) -> tuple[str, str]:
    """Return (effective project, detail).

    `project` is editable in the settings form, and terraform.tfvars is the file
    terraform actually reads — so the prefix every resource name is built from
    must come from there, falling back to the environment's default when unset.
    Lives here rather than in probe.py because steps.py needs it too, and
    probe.py imports steps' Command (importing probe from steps would cycle).

    `path` overrides where tfvars is read from; probe.py passes its own
    indirected path so existing monkeypatched tests keep working.

    detail is non-empty only when tfvars overrides the default, so callers can
    surface the divergence instead of silently reporting on another deployment.
    """
    path = tfvars_path(env) if path is None else path
    if not path.is_file():
        return env.project, ""

    values = hcl.read_values(path.read_text())
    override = values.get("project", "").strip('"').strip()
    if not override:
        return env.project, ""

    detail = "" if override == env.project else f"(tfvars project: {override})"
    return override, detail


def resolve_region(env: Env | None = None, path: Path | None = None) -> tuple[str, str]:
    """Return (effective region, detail).

    Every `aws` call the installer makes, and the ECR registry hostname it
    writes into tfvars, must land in the region terraform deploys to. `region`
    is editable in the settings form and written to terraform.tfvars, so tfvars
    wins; otherwise the default variables.tf declares. The shell's AWS_REGION is
    deliberately *not* consulted for a stack: terraform's provider region is
    `var.region`, so the shell never reaches it, and reading it here made the
    dashboard probe the wrong place — with AWS_REGION=us-east-1 and no tfvars yet
    it reported an existing us-east-1 stack's resources as this Tokyo
    deployment's and marked steps 완료 that had not run (observed 2026-10-06).

    This used to be a `REGION = "us-east-1"` constant in three modules. With
    `region = "ap-northeast-2"` in tfvars terraform built the stack there while
    create-memory, ecs update-service, logs tail and `docker push` all went to
    us-east-1 — so the memory was created in a region the stack could not read,
    and `memory_created` still reported DONE because the probe looked where the
    command had written rather than where the deployment was.

    `env` is None for calls whose answer does not depend on the region (sts
    get-caller-identity) and which have no environment in scope; those only need
    an endpoint that resolves.

    detail is non-empty only when tfvars diverges from that fallback, mirroring
    resolve_project so callers can surface it instead of silently acting on
    another region.
    """
    if env is None:
        # No stack in scope: any endpoint that resolves will do, so follow the
        # CLI's own precedence (AWS_REGION, then AWS_DEFAULT_REGION).
        return (
            os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or _DEFAULT_REGION
        ), ""

    fallback = _DEFAULT_REGION
    path = tfvars_path(env) if path is None else path
    if not path.is_file():
        return fallback, ""

    values = hcl.read_values(path.read_text())
    override = values.get("region", "").strip('"').strip()
    if not override:
        return fallback, ""

    detail = "" if override == fallback else f"(tfvars region: {override})"
    return override, detail
