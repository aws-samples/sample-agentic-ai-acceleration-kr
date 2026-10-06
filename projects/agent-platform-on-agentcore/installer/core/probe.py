"""Read-only checks that answer "is this step already done?".

No progress file is kept. Each probe re-derives its answer so that work done
outside the installer is visible, and so a half-finished environment resumes at
the right step.
"""
from __future__ import annotations

import enum
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Protocol

from installer.core import env as envmod
from installer.core import hcl
from installer.core.env import Env
from installer.core.runner import Command

# Memory for the default Runtime agent (session-summary strategy).
MEMORY_NAME = "bap_conversations_default"

# Substrings that mean "the answer is unavailable", as opposed to "absent".
# Treating these as PENDING would re-run finished work; treating them as DONE
# would skip work that never happened. Neither is acceptable, hence UNKNOWN.
_UNDECIDABLE = (
    "AccessDenied",
    "AccessDeniedException",
    "UnauthorizedOperation",
    "ExpiredToken",
    "ExpiredTokenException",
    "InvalidClientTokenId",
    "Unable to locate credentials",
    "no valid credential sources",
    "Could not connect to the endpoint URL",
    "EndpointConnectionError",
    "RequestTimeout",
)

# Errors that genuinely mean "the resource is not there", i.e. the step still
# needs doing. Anything else that fails is treated as undecidable.
#
# The direction matters: an allow-list of undecidable errors leaks. Throttling,
# 5xx, connect timeouts and TLS failures are all real AWS responses that no
# reasonable list enumerates, and every one of them was landing in PENDING —
# telling the user to re-run work that may already be done. Recognising absence
# and defaulting everything else to UNKNOWN fails safe instead.
# Strings below were taken from real AWS responses on this account, not guessed:
#   s3api head-bucket        -> "An error occurred (404) ... : Not Found"
#   dynamodb describe-table  -> "(ResourceNotFoundException) ... not found"
#   ecr describe-images      -> "(RepositoryNotFoundException) ... does not exist"
#   ecs describe-services    -> "(ClusterNotFoundException) ... Cluster not found."
_NOT_FOUND = (
    "NotFound",                # the *NotFoundException family, no space
    "Not Found",               # head-bucket's 404 body
    "(404)",
    "does not exist",
    "not found",               # lower-case tail of several messages
    "NoSuchBucket",
    "NoSuchEntity",
    "NoSuchKey",
)


def _absent(output: str) -> bool:
    """True when the output says the resource does not exist."""
    return any(needle in output for needle in _NOT_FOUND)


def _classify_failure(output: str, absent_detail: str) -> ProbeOutcome:
    """Turn a failed read-only call into PENDING (absent) or UNKNOWN (unclear)."""
    blocker = _undecidable(output)
    if blocker:
        return ProbeOutcome(State.UNKNOWN, f"판정 불가 ({blocker})")
    if _absent(output):
        return ProbeOutcome(State.PENDING, absent_detail)
    first = output.strip().splitlines()[0] if output.strip() else "출력 없음"
    return ProbeOutcome(State.UNKNOWN, f"판정 불가: {first[:160]}")


class State(enum.Enum):
    DONE = "done"
    PENDING = "pending"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProbeOutcome:
    state: State
    detail: str = ""


class Shell(Protocol):
    async def capture(self, cmd: Command) -> tuple[int, str]:
        """Run a read-only command and return (exit code, combined output)."""


Probe = Callable[[Env, Shell], Awaitable[ProbeOutcome]]


def _undecidable(output: str) -> str | None:
    for needle in _UNDECIDABLE:
        if needle in output:
            return needle
    return None


# Indirected for tests: monkeypatched to point at temp files.
def _tfvars_path(env: Env) -> Path:
    return env.tfvars_path


def _backend_hcl_path(env: Env) -> Path:
    return env.tf_dir / "backend.hcl"


def resolve_project(env: Env) -> tuple[str, str]:
    """Delegate to env.resolve_project through this module's tfvars indirection.

    The implementation lives in env.py because steps.py needs it too and cannot
    import this module (probe imports steps' Command, so the reverse would
    cycle). This stays a delegate rather than a copy so the two cannot drift,
    while `_tfvars_path` keeps working for tests that monkeypatch it.
    """
    return envmod.resolve_project(env, _tfvars_path(env))


def resolve_region(env: Env | None) -> str:
    """The region to address, through this module's tfvars indirection.

    A delegate for the same reason resolve_project is one. Drops the detail half
    because probes report on state, not on configuration divergence.
    """
    path = _tfvars_path(env) if env is not None else None
    return envmod.resolve_region(env, path)[0]


def _aws(env: Env, *args: str) -> Command:
    return Command(argv=("aws", *args, "--region", resolve_region(env)))


def _tf(env: Env, *args: str) -> Command:
    return Command(argv=("terraform", f"-chdir={env.tf_dir}", *args))


async def tools_present(env: Env, sh: Shell) -> ProbeOutcome:
    # `command -v` is a shell builtin, so exec'ing it raises FileNotFoundError.
    # Resolve tool presence in-process instead of spawning anything.
    missing = [t for t in ("terraform", "aws", "docker") if shutil.which(t) is None]
    if missing:
        return ProbeOutcome(State.PENDING, f"설치되지 않음: {', '.join(missing)}")
    code, out = await sh.capture(_aws(env, "sts", "get-caller-identity"))
    if code != 0:
        return ProbeOutcome(State.PENDING, f"AWS 자격증명 없음: {out.strip()[:200]}")
    return ProbeOutcome(State.DONE)


async def account_id(sh: Shell, env: Env | None = None) -> tuple[str | None, str]:
    """Return (account id, raw output). None means it could not be read.

    `env` is optional because the answer is the same in every region — it only
    picks the endpoint. Callers that have one pass it so a caller-supplied
    region is not contradicted here.
    """
    code, out = await sh.capture(
        Command(argv=("aws", "sts", "get-caller-identity",
                      "--query", "Account", "--output", "text",
                      "--region", resolve_region(env)))
    )
    if code != 0 or not out.strip():
        return None, out
    return out.strip().split()[0], out


async def state_backend(env: Env, sh: Shell) -> ProbeOutcome:
    # The bucket name embeds the account id. S3 rejects a wildcard at parameter
    # validation, so the id has to be resolved before the name can be built.
    project, _ = resolve_project(env)
    account, raw = await account_id(sh, env)
    if account is None:
        return ProbeOutcome(State.UNKNOWN, f"계정 ID 를 읽을 수 없습니다: {raw.strip()[:200]}")

    bucket_code, bucket_out = await sh.capture(
        _aws(env, "s3api", "head-bucket", "--bucket", f"{project}-tfstate-{account}")
    )
    if bucket_code != 0:
        # head-bucket answers a bare "Not Found" with no error code, so absence
        # is recognised by _NOT_FOUND matching that string.
        return _classify_failure(bucket_out, "state 버킷이 없습니다")

    table_code, table_out = await sh.capture(
        _aws(env, "dynamodb", "describe-table", "--table-name", f"{project}-tflock")
    )
    if table_code != 0:
        return _classify_failure(table_out, "잠금 테이블이 없습니다")
    return ProbeOutcome(State.DONE)


async def backend_configured(env: Env, sh: Shell) -> ProbeOutcome:
    path = _backend_hcl_path(env)
    if not path.is_file():
        return ProbeOutcome(State.PENDING, "backend.hcl 이 없습니다")
    if not re.search(r"\d{12}", path.read_text()):
        return ProbeOutcome(State.PENDING, "backend.hcl 에 계정 ID 가 없습니다")
    return ProbeOutcome(State.DONE)


async def tfvars_complete(env: Env, sh: Shell) -> ProbeOutcome:
    path = _tfvars_path(env)
    if not path.is_file():
        return ProbeOutcome(State.PENDING, "terraform.tfvars 가 없습니다")
    present = hcl.read_values(path.read_text())
    needed = [
        v.name
        for v in hcl.read_variables(env.variables_path)
        if not v.has_default
    ]
    missing = [n for n in needed if not present.get(n, "").strip('"')]
    if missing:
        return ProbeOutcome(State.PENDING, f"비어 있음: {', '.join(missing)}")
    return ProbeOutcome(State.DONE)


async def tf_initialised(env: Env, sh: Shell) -> ProbeOutcome:
    _, detail = resolve_project(env)
    if not (env.tf_dir / ".terraform").is_dir():
        return ProbeOutcome(State.PENDING, "terraform init 이 필요합니다")
    return ProbeOutcome(State.DONE, detail)


def _first_apply_vars(env: Env) -> tuple[str, ...]:
    """`-var` flags that reproduce the first apply: 0 tasks, placeholder images.

    The placeholders are the variables.tf defaults, so the images step writing
    real URIs into tfvars does not make this probe report the first apply undone.
    """
    defaults = {
        v.name: (v.default_literal or "").strip('"')
        for v in hcl.read_variables(env.variables_path)
        if v.name in ("server_image", "web_image")
    }
    out = ["-var", "desired_count=0"]
    for name in ("server_image", "web_image"):
        if defaults.get(name):
            out += ["-var", f"{name}={defaults[name]}"]
    return tuple(out)


async def first_apply_done(env: Env, sh: Shell) -> ProbeOutcome:
    project, detail = resolve_project(env)
    code, out = await sh.capture(_tf(env, "output", "-json", "ecr_repository_urls"))
    blocker = _undecidable(out)
    if blocker:
        return ProbeOutcome(State.UNKNOWN, f"판정 불가 ({blocker})")
    if code != 0 or not out.strip() or out.strip() in ("{}", "null"):
        return ProbeOutcome(State.PENDING, "ECR 리포지토리 output 이 비어 있습니다")
    # Parse rather than trust the text: a malformed body is "cannot tell", and an
    # empty map means the apply did not create the repositories.
    try:
        urls = json.loads(out)
    except ValueError:
        return ProbeOutcome(State.UNKNOWN, "terraform output 을 읽을 수 없습니다")
    if not urls:
        return ProbeOutcome(State.PENDING, "ECR 리포지토리 output 이 비어 있습니다")

    # Once the services run with tasks, the second apply has converged the
    # stack and the first one necessarily finished; the plan below would only
    # report desired_count 1 -> 0 and flip this row back to 대기.
    svc_code, svc_out = await sh.capture(
        _aws(env, "ecs", "describe-services",
             "--cluster", f"{project}-cluster", "--services", "server", "web",
             "--query", "services[].desiredCount", "--output", "text")
    )
    if svc_code == 0 and svc_out.split() and all(
        n.isdigit() and int(n) >= 1 for n in svc_out.split()
    ):
        return ProbeOutcome(State.DONE, detail)

    # ECR existing is not proof the apply finished. A real run failed on the NAT
    # gateway (EIP quota) AFTER creating the repositories, and this probe reported
    # 완료 for an apply that errored — 96 resources in, ~40 still missing. Terraform
    # itself knows: a converged state produces an empty plan.
    #
    # Planned with the same variables the first apply used (0 tasks, placeholder
    # images). Without them the plan always wants desired_count 0 -> 1 and, after
    # the images step, a new task definition — so a finished first apply read
    # 대기 forever on a real run.
    plan_code, plan_out = await sh.capture(
        _tf(env, "plan", "-detailed-exitcode", "-input=false", "-no-color",
            *_first_apply_vars(env))
    )
    # -detailed-exitcode: 0 = no changes, 2 = changes pending, 1 = plan error.
    if plan_code == 2:
        summary = next((l.strip() for l in plan_out.splitlines() if l.strip().startswith("Plan:")), "")
        return ProbeOutcome(
            State.PENDING, f"apply 가 끝나지 않았습니다 (남은 변경: {summary or '있음'})"
        )
    if plan_code not in (0, 2):
        blocker = _undecidable(plan_out)
        note = f" ({blocker})" if blocker else ""
        return ProbeOutcome(State.UNKNOWN, f"plan 을 확인할 수 없습니다{note}")
    return ProbeOutcome(State.DONE, detail)


async def images_pushed(env: Env, sh: Shell) -> ProbeOutcome:
    project, _ = resolve_project(env)
    for repo in ("server", "web"):
        code, out = await sh.capture(
            _aws(env, "ecr", "describe-images", "--repository-name", f"{project}/{repo}")
        )
        if code != 0:
            return _classify_failure(out, f"{repo} 리포지토리가 없습니다")
        # A repository that exists but holds nothing returns exit 0 with
        # {"imageDetails": []} — verified against real ECR. Trusting the exit
        # code alone reports DONE when nothing was ever pushed.
        try:
            details = json.loads(out).get("imageDetails", [])
        except ValueError:
            return ProbeOutcome(State.UNKNOWN, f"{repo}: describe-images 응답을 읽을 수 없습니다")
        if not details:
            return ProbeOutcome(State.PENDING, f"{repo} 이미지가 없습니다")
    return ProbeOutcome(State.DONE)


async def services_running(env: Env, sh: Shell) -> ProbeOutcome:
    project, _ = resolve_project(env)
    code, out = await sh.capture(
        _aws(env, "ecs", "describe-services",
             "--cluster", f"{project}-cluster", "--services", "server", "web")
    )
    if code != 0:
        return _classify_failure(out, "ECS 서비스를 찾을 수 없습니다")
    try:
        services = json.loads(out)["services"]
    except (ValueError, KeyError):
        return ProbeOutcome(State.UNKNOWN, "describe-services 응답을 읽을 수 없습니다")
    if not services:
        return ProbeOutcome(State.PENDING, "ECS 서비스가 없습니다")
    for svc in services:
        if svc.get("desiredCount", 0) < 1:
            return ProbeOutcome(State.PENDING, "서비스가 park 상태입니다 (desired_count=0)")
        if svc.get("runningCount", 0) < 1:
            return ProbeOutcome(State.PENDING, "태스크가 아직 뜨지 않았습니다")
    return ProbeOutcome(State.DONE)


async def memory_created(env: Env, sh: Shell) -> ProbeOutcome:
    code, out = await sh.capture(
        _aws(env, "bedrock-agentcore-control", "list-memories",
             "--query", f"memories[?starts_with(id, '{MEMORY_NAME}-')].id", "--output", "text")
    )
    blocker = _undecidable(out)
    if blocker:
        return ProbeOutcome(State.UNKNOWN, f"판정 불가 ({blocker})")
    if code != 0:
        return ProbeOutcome(State.UNKNOWN, out.strip()[:200])
    if not out.strip():
        return ProbeOutcome(State.PENDING, "AgentCore Memory 가 없습니다")
    return ProbeOutcome(State.DONE, out.strip())



async def local_env_filled(env: Env, sh: Shell) -> ProbeOutcome:
    from installer.core.env import REPO_ROOT

    checks = {
        REPO_ROOT / "server" / ".env": ("AGENT_REGISTRY_ID", "COGNITO_USER_POOL_ID"),
        REPO_ROOT / "agent-runtime" / ".env": ("MEMORY_ID",),
    }
    for path, keys in checks.items():
        if not path.is_file():
            return ProbeOutcome(State.PENDING, f"{path.name} 이 없습니다")
        text = path.read_text()
        for key in keys:
            if not re.search(rf"^{key}=.+$", text, re.M):
                return ProbeOutcome(State.PENDING, f"{path.name}: {key} 가 비어 있습니다")
    return ProbeOutcome(State.DONE)


async def transaction_search_enabled(env: Env, sh: Shell) -> ProbeOutcome:
    # Spans are exported once the trace destination is CloudWatch Logs and that
    # change is ACTIVE. The previous check read `IndexingRuleUpdates` /
    # `RuleStatus` from get-indexing-rules, neither of which the API returns, so
    # this row could never reach 완료 on a real account.
    code, out = await sh.capture(_aws(env, "xray", "get-trace-segment-destination"))
    if code != 0:
        return _classify_failure(out, "Transaction Search 상태를 확인할 수 없습니다")
    try:
        body = json.loads(out)
    except ValueError:
        return ProbeOutcome(State.UNKNOWN, "get-trace-segment-destination 응답을 읽을 수 없습니다")

    destination, status = body.get("Destination"), body.get("Status")
    if destination == "CloudWatchLogs" and status == "ACTIVE":
        return ProbeOutcome(State.DONE)
    if destination == "CloudWatchLogs":
        return ProbeOutcome(State.PENDING, f"활성화 중입니다 (Status: {status}). 잠시 후 r 로 재점검하세요.")

    region = resolve_region(env)
    cmd = (
        "# 1) X-Ray 가 스팬을 CloudWatch Logs 에 쓸 수 있도록 리소스 정책을 먼저 둡니다 (CLI 로 켤 때만 필요)\n"
        "ACCOUNT=$(aws sts get-caller-identity --query Account --output text)\n"
        f"aws logs put-resource-policy --region {region} --policy-name TransactionSearchXRayAccess \\\n"
        "  --policy-document '{\"Version\":\"2012-10-17\",\"Statement\":[{\"Sid\":\"TransactionSearchXRayAccess\","
        "\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"xray.amazonaws.com\"},\"Action\":\"logs:PutLogEvents\","
        f"\"Resource\":[\"arn:aws:logs:{region}:'$ACCOUNT':log-group:aws/spans:*\","
        f"\"arn:aws:logs:{region}:'$ACCOUNT':log-group:/aws/application-signals/data:*\"],"
        f"\"Condition\":{{\"ArnLike\":{{\"aws:SourceArn\":\"arn:aws:xray:{region}:'$ACCOUNT':*\"}},"
        "\"StringEquals\":{\"aws:SourceAccount\":\"'$ACCOUNT'\"}}}]}'\n"
        "# 2) 스팬 목적지를 CloudWatch Logs 로 바꾸고 샘플링 비율(과금에 직결)을 정합니다\n"
        f"aws xray update-trace-segment-destination --destination CloudWatchLogs --region {region}\n"
        f"aws xray update-indexing-rule --name Default --region {region} \\\n"
        "  --rule '{\"Probabilistic\":{\"DesiredSamplingPercentage\":10}}'"
    )
    return ProbeOutcome(
        State.PENDING,
        f"Transaction Search 를 켜면 trace 패널이 작동합니다.\n\n"
        f"다른 터미널에서 아래 명령을 실행한 뒤 r 로 재점검하세요:\n\n{cmd}"
    )
