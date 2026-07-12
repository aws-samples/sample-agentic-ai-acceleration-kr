# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""워크플로우별 Step 시퀀스 정의. 순서 자체가 배포 함정 회피의 핵심."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import paths
from .config import BackendConfig


@dataclass
class Step:
    name: str
    argv: list[str]
    cwd: Path | None = None
    env: dict[str, str] | None = None
    skippable: bool = False


def _flags_to_env(flags: dict[str, bool]) -> dict[str, str]:
    return {k: ("true" if v else "false") for k, v in flags.items()}


def build_llm_workflow(*, env: str, backend: BackendConfig,
                       enable_chat_db_tools: bool, flags: dict[str, bool],
                       cluster_name: str = "llm-gateway") -> list[Step]:
    tf_dir = paths.llm_tf_dir(env)
    wf: list[Step] = []

    # ★함정④: chat_db tools 켜지면 terraform 앞에 Lambda 빌드 선행
    if enable_chat_db_tools:
        wf.append(Step("build-lambdas", ["bash", str(paths.BUILD_LAMBDAS_SH)]))

    # ★함정⑤: tf-init에 -backend-config 자동 주입
    wf.append(Step("tf-init",
                   ["terraform", "init", "-input=false", "-reconfigure", *backend.backend_args()],
                   cwd=tf_dir))
    wf.append(Step("tf-plan", ["terraform", "plan", "-input=false"], cwd=tf_dir))
    wf.append(Step("tf-apply", ["terraform", "apply", "-auto-approve", "-input=false"], cwd=tf_dir))

    # ★함정③: install-eks에 격리 KUBECONFIG + 실행 플래그 env 주입.
    #   install-eks.sh가 이 파일에 kubectl context를 써 넣는다.
    kubeconfig = f"/tmp/{cluster_name}.kubeconfig"
    install_env = {"KUBECONFIG": kubeconfig, **_flags_to_env(flags)}
    wf.append(Step("install-eks", ["bash", str(paths.script("install-eks.sh")), env],
                   env=install_env))

    # ★함정⑥+④: 배포 후 migration 적용 검증 (smoke-test가 db_health 포함 검증).
    #   반드시 install-eks와 동일한 격리 KUBECONFIG를 써야 한다. 안 그러면 기본
    #   ~/.kube/config(이 계정의 다른 EKS 클러스터로 오염됨)를 봐서 pod를 못 찾고
    #   "Pod 없음"으로 전부 오탐한다(실제 배포는 정상인데 smoke-test만 FAIL).
    wf.append(Step("verify-migration",
                   ["bash", str(paths.script("smoke-test.sh")), "--env", env],
                   env={"KUBECONFIG": kubeconfig},
                   skippable=True))
    return wf


def build_tool_workflow(*, backend: BackendConfig,
                        key_file: Path | None = None) -> list[Step]:
    tf_dir = paths.TOOL_TF_DIR
    # ★함정: API 키는 terraform(tfvars/tfstate)을 거치면 안 된다. tfvars에 넣으면
    #   "undeclared variable" 경고 + 키가 tfstate에 평문 저장된다. provision 스크립트가
    #   TOOL_KEY_FILE(engine=value 형식)을 읽어 seed-tool-secrets.sh로 Secrets Manager에
    #   직접 주입하는 게 유일한 정식 경로다.
    deploy_env = {"TOOL_KEY_FILE": str(key_file)} if key_file else None
    wf: list[Step] = [
        Step("tf-init",
             ["terraform", "init", "-input=false", "-reconfigure", *backend.backend_args()],
             cwd=tf_dir),
        Step("tf-apply",
             ["bash", str(paths.script("provision_tool_gateway.sh")), "deploy"],
             env=deploy_env),
    ]
    return wf


# ★teardown 순서 함정: helm uninstall(ALB 제거)이 terraform destroy보다 먼저여야
# orphaned ENI 없이 VPC가 삭제된다. cluster_name/region은 terraform output에서 조회.
_HELM_UNINSTALL = """\
set -e
CL=$(terraform output -raw cluster_name 2>/dev/null) || {
  echo "cluster_name output 없음 — helm 단계 스킵 (이미 destroy됐거나 미배포)"; exit 0;
}
RG=$(terraform output -json | jq -r '.cluster_endpoint.value' | awk -F. '{print $3}')
echo "cluster=$CL region=$RG"
aws eks update-kubeconfig --region "$RG" --name "$CL"
helm uninstall llm-gateway -n llm-gateway || echo "helm release 없음 — ok"
"""


def build_llm_teardown(*, env: str, backend: BackendConfig,
                       cluster_name: str = "llm-gateway") -> list[Step]:
    """LLM Gateway 삭제: helm uninstall → terraform destroy (순서가 함정 회피)."""
    tf_dir = paths.llm_tf_dir(env)
    return [
        # ★함정③: 격리 KUBECONFIG. best-effort이므로 skippable.
        Step("helm-uninstall", ["bash", "-c", _HELM_UNINSTALL],
             cwd=tf_dir,
             env={"KUBECONFIG": f"/tmp/{cluster_name}.kubeconfig"},
             skippable=True),
        # ★함정⑤: destroy도 backend가 필요 → tf-init에 -backend-config 주입.
        Step("tf-init",
             ["terraform", "init", "-input=false", "-reconfigure", *backend.backend_args()],
             cwd=tf_dir),
        Step("tf-destroy",
             ["terraform", "destroy", "-auto-approve", "-input=false"],
             cwd=tf_dir),
    ]


# AgentCore api-key credential provider 이름 = bare 엔진 키. terraform이
# `enable_x ? {...} : null`로 이 이름들로 만든다. 다른 스택의 `-creds` 접미
# provider나 무관한 provider는 절대 건드리지 않도록 이 고정 집합만 정리한다.
MANAGED_CREDENTIAL_PROVIDERS = (
    "tavily", "brave", "serper", "exa",
    "perplexity", "anthropic", "firecrawl", "you",
)

# ★teardown 드리프트 함정: aws_bedrockagentcore_api_key_credential_provider는
# provider 미성숙으로 terraform destroy가 AWS 리소스를 못 지우고 state에서만
# 빠지는 경우가 있다 → 재배포 시 "already exists". destroy 후 이 이름들이 AWS에
# 남아있으면 control CLI로 삭제한다(best-effort, 존재하는 것만).
_CRED_PROVIDER_CLEANUP = r"""
set -e
REGION="${TOOL_GW_REGION:-us-east-1}"
MANAGED="{names}"
existing=$(aws bedrock-agentcore-control list-api-key-credential-providers \
  --region "$REGION" --query 'credentialProviders[].name' --output text 2>/dev/null || echo "")
for n in $MANAGED; do
  # 정확 일치만 삭제. 단어경계 매칭은 '-'를 경계로 취급해 tavily가 tavily-creds에
  # 오탐되므로, existing의 각 항목과 문자열 완전 일치 비교로 확인한다.
  found=""
  for e in $existing; do
    [ "$e" = "$n" ] && found="yes" && break
  done
  if [ -n "$found" ]; then
    echo "orphan credential provider 삭제: $n"
    aws bedrock-agentcore-control delete-api-key-credential-provider \
      --name "$n" --region "$REGION" 2>&1 | head -2 || echo "  (삭제 실패 — 수동 확인 필요: $n)"
  fi
done
echo "credential provider 정리 완료"
""".replace("{names}", " ".join(MANAGED_CREDENTIAL_PROVIDERS))


def build_tool_teardown() -> list[Step]:
    """Tool Gateway 삭제: provision_tool_gateway.sh teardown (terraform destroy 내장)
    후, terraform이 못 지운 api-key credential provider orphan을 정리한다."""
    return [
        Step("tool-teardown",
             ["bash", str(paths.script("provision_tool_gateway.sh")), "teardown"]),
        # ★재발 방지: destroy가 남긴 orphan credential provider 정리 (best-effort)
        Step("cleanup-credential-providers",
             ["bash", "-c", _CRED_PROVIDER_CLEANUP],
             skippable=True),
    ]
