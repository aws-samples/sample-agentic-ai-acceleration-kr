"""Step declarations.

Each step is data: an id, what it depends on, what it does, and how to tell
whether it is already done. The UI renders this list and nothing more, so a new
step costs an entry here rather than a screen change.
"""
from __future__ import annotations

from dataclasses import dataclass

from installer.core import probe
from installer.core.env import Env, resolve_project, resolve_region
from installer.core.probe import Probe, State
from installer.core.runner import Command


@dataclass(frozen=True)
class FileWrite:
    """A step that writes a file the installer owns.

    Distinct from Command because the write path carries constraints a
    subprocess does not: git-ignore verification, 0600, backups, line-level
    replacement. Folding it into Command would scatter those rules.
    """

    target: str
    description: str


Action = Command | FileWrite | None


@dataclass(frozen=True)
class Step:
    id: str
    title: str
    requires: tuple[str, ...]
    action: Action
    probe: Probe
    destructive: bool = False


@dataclass(frozen=True)
class Plan:
    steps: tuple[Step, ...]

    def by_id(self, step_id: str) -> Step:
        for step in self.steps:
            if step.id == step_id:
                return step
        raise KeyError(step_id)

    def blocked_by(self, step_id: str, states: dict[str, State]) -> tuple[str, ...]:
        """Dependencies that are not DONE. UNKNOWN counts as blocking."""
        return tuple(
            dep
            for dep in self.by_id(step_id).requires
            if states.get(dep) is not State.DONE
        )


def _tf(env: Env, *args: str) -> Command:
    return Command(argv=("terraform", f"-chdir={env.tf_dir}", *args))


def _init_command(env: Env) -> Command:
    """Pass `-backend-config` once backend.hcl exists.

    backend.tf declares `backend "s3" {}` without a bucket (the bucket name
    embeds the account id), so init cannot succeed without the file. Before the
    `backend` step writes it, plain init is assembled so --dry-run still shows
    the command.
    """
    backend_hcl = env.tf_dir / "backend.hcl"
    if backend_hcl.is_file():
        return _tf(env, "init", "-input=false", f"-backend-config={backend_hcl}")
    return _tf(env, "init", "-input=false")


def _bootstrap_command(env: Env) -> Command:
    """init, then apply, in infra/bootstrap.

    A fresh checkout has no .terraform/ there, and `apply` alone fails with
    "Inconsistent dependency lock file … run: terraform init" — the first thing a
    new user saw on a real run. init is idempotent, so it runs every time.
    """
    chdir = f"-chdir={env.tf_dir.parents[1] / 'bootstrap'}"
    # tfvars is what terraform reads, so the state backend must be named from
    # the same value the resources are.
    project = resolve_project(env)[0]
    return Command(
        argv=(
            "sh", "-c",
            f"terraform {chdir} init -input=false && "
            f"terraform {chdir} apply -auto-approve -var project={project}",
        )
    )


def build_plan(env: Env) -> Plan:
    steps: list[Step] = [
        Step(
            id="preflight",
            title="사전 점검 (terraform / aws / docker / 자격증명)",
            requires=(),
            action=None,
            probe=probe.tools_present,
        )
    ]

    steps += [
        Step(
            id="bootstrap",
            title="state 백엔드 생성 (S3 버킷 + 잠금 테이블)",
            requires=("preflight",),
            action=_bootstrap_command(env),
            probe=probe.state_backend,
        ),
        Step(
            id="backend",
            title="backend 설정 (계정 ID 기입)",
            requires=("bootstrap",),
            action=FileWrite(
                target="backend",
                description="backend.hcl 생성",
            ),
            probe=probe.backend_configured,
        ),
    ]

    steps += [
        Step(
            id="transaction_search",
            title="Transaction Search (계정 단위, 한 번만)",
            requires=("preflight",),
            action=None,
            probe=probe.transaction_search_enabled,
        ),
        Step(
            id="tfvars",
            title="설정값 입력 → terraform.tfvars",
            requires=("preflight",),
            action=FileWrite(target="tfvars", description="Settings 폼의 값을 기록"),
            probe=probe.tfvars_complete,
        ),
        Step(
            id="init",
            title="terraform init",
            requires=("backend", "tfvars"),
            action=_init_command(env),
            probe=probe.tf_initialised,
        ),
    ]

    steps += [
        Step(
            id="first_apply",
            title="1차 apply (서비스는 0으로, ECR 리포지토리 생성)",
            requires=("init",),
            action=_tf(env, "apply", "-auto-approve", "-var", "desired_count=0"),
            probe=probe.first_apply_done,
        ),
        Step(
            id="images",
            title="이미지 빌드 & ECR push (linux/amd64)",
            requires=("first_apply",),
            action=FileWrite(
                target="images",
                description="ECR 로그인 → docker build/push ×2 → tfvars 에 URI 기록",
            ),
            probe=probe.images_pushed,
        ),
        Step(
            id="second_apply",
            title="2차 apply (서비스 기동)",
            requires=("images",),
            action=_tf(env, "apply", "-auto-approve"),
            probe=probe.services_running,
        ),
        Step(
            id="memory",
            title="AgentCore Memory 생성 (default 에이전트, 세션 요약 전략 포함)",
            requires=("second_apply",),
            # `namespaces`, not `namespaceTemplates`: the CLI rejects the latter
            # ("Unknown parameter in memoryStrategies[0].summaryMemoryStrategy"),
            # which is how this step failed on a real run.
            action=Command(
                argv=(
                    "aws", "bedrock-agentcore-control", "create-memory",
                    "--name", probe.MEMORY_NAME,
                    "--event-expiry-duration", "365",
                    "--memory-strategies",
                    '[{"summaryMemoryStrategy":{"name":"session_summary",'
                    '"namespaces":["/summaries/{actorId}/{sessionId}"]}}]',
                    "--region", resolve_region(env)[0],
                )
            ),
            probe=probe.memory_created,
        ),
        Step(
            id="local_env",
            title="로컬 .env 채우기 (server / agent-runtime)",
            requires=("memory",),
            action=FileWrite(
                target="local_env",
                description="terraform output → server/.env, agent-runtime/.env",
            ),
            probe=probe.local_env_filled,
        ),
    ]

    return Plan(steps=tuple(steps))


@dataclass(frozen=True)
class Operation:
    id: str
    title: str
    action: Action
    destructive: bool = False


def operations(env: Env) -> tuple[Operation, ...]:
    project, _ = resolve_project(env)
    region, _ = resolve_region(env)
    return (
        Operation(
            id="redeploy",
            title="재배포 (같은 태그를 다시 push 했을 때, server + web)",
            # Both services: a `latest` re-push changes neither task definition,
            # so terraform sees nothing to do and each service has to be told.
            action=Command(
                argv=(
                    "sh", "-c",
                    " && ".join(
                        f"aws ecs update-service --cluster {project}-cluster "
                        f"--service {service} --force-new-deployment --region {region}"
                        for service in ("server", "web")
                    ),
                )
            ),
        ),
        Operation(
            id="rebuild",
            title="이미지 재빌드 & push",
            action=FileWrite(target="images", description="docker build/push ×2"),
        ),
        Operation(
            id="logs",
            title="서버 로그 tail (CloudWatch)",
            action=Command(
                argv=(
                    "aws", "logs", "tail", f"/ecs/{project}/server",
                    "--follow", "--region", region,
                )
            ),
        ),
        Operation(
            id="park",
            title="park — 리소스는 남기고 태스크만 0으로",
            action=Command(
                argv=(
                    "terraform", f"-chdir={env.tf_dir}",
                    "apply", "-auto-approve", "-var", "desired_count=0",
                )
            ),
            destructive=True,
        ),
        Operation(
            id="destroy",
            title="destroy — 환경 전체 삭제",
            action=Command(argv=("terraform", f"-chdir={env.tf_dir}", "destroy", "-auto-approve")),
            destructive=True,
        ),
    )


def registry_host(env: Env, account: str) -> str:
    """The ECR registry the images step pushes to.

    app.py records the same URIs in tfvars after the push, and a second copy of
    this expression there is how the two came to disagree about the region.
    """
    region, _ = resolve_region(env)
    return f"{account}.dkr.ecr.{region}.amazonaws.com"


def build_commands(env: Env, step_id: str, values: dict) -> tuple[Command, ...]:
    """Expand a FileWrite step that runs commands (images) into real commands."""
    if step_id != "images":
        raise KeyError(step_id)

    account = values["account_id"]
    region, _ = resolve_region(env)
    registry = registry_host(env, account)
    tag = values.get("tag", "latest")
    root = env.tf_dir.parents[2]
    # Must match what images_pushed probes, or a successful push reads PENDING.
    project, _ = resolve_project(env)

    login = Command(
        argv=("sh", "-c",
              f"aws ecr get-login-password --region {region} | "
              f"docker login --username AWS --password-stdin {registry}")
    )
    out: list[Command] = [login]
    for component in ("server", "web"):
        image = f"{registry}/{project}/{component}:{tag}"
        out.append(
            Command(
                argv=(
                    "docker", "build",
                    # ECS task definitions set no runtimePlatform, so Fargate
                    # runs x86_64. An arm64 image crash-loops with only a
                    # CloudWatch trail to explain it.
                    "--platform", "linux/amd64",
                    "-t", image, str(root / component),
                )
            )
        )
        out.append(Command(argv=("docker", "push", image)))
    return tuple(out)
