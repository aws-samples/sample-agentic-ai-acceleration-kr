from deployment.tui import steps
from deployment.tui.config import BackendConfig

BE = BackendConfig(bucket="b", dynamodb_table="t")


def _names(wf):
    return [s.name for s in wf]


def test_llm_workflow_includes_build_lambdas_when_chat_db_enabled():
    wf = steps.build_llm_workflow(env="dev", backend=BE, enable_chat_db_tools=True, flags={})
    assert "build-lambdas" in _names(wf)


def test_llm_workflow_omits_build_lambdas_when_chat_db_disabled():
    wf = steps.build_llm_workflow(env="dev", backend=BE, enable_chat_db_tools=False, flags={})
    assert "build-lambdas" not in _names(wf)


def test_tf_init_injects_backend_config():
    wf = steps.build_llm_workflow(env="dev", backend=BE, enable_chat_db_tools=False, flags={})
    tf_init = next(s for s in wf if s.name == "tf-init")
    assert "-backend-config=bucket=b" in tf_init.argv
    assert "-backend-config=dynamodb_table=t" in tf_init.argv


def test_install_eks_isolates_kubeconfig():
    wf = steps.build_llm_workflow(env="dev", backend=BE, enable_chat_db_tools=False,
                                  flags={}, cluster_name="my-cluster")
    install = next(s for s in wf if s.name == "install-eks")
    assert install.env["KUBECONFIG"] == "/tmp/my-cluster.kubeconfig"


def test_verify_migration_shares_install_kubeconfig():
    # smoke-test 스텝도 install-eks와 동일 격리 KUBECONFIG를 써야 한다. 없으면
    # 기본 ~/.kube/config(다른 클러스터로 오염됨)를 봐서 pod를 못 찾고 오탐한다.
    wf = steps.build_llm_workflow(env="dev", backend=BE, enable_chat_db_tools=False,
                                  flags={}, cluster_name="my-cluster")
    verify = next(s for s in wf if s.name == "verify-migration")
    assert verify.env["KUBECONFIG"] == "/tmp/my-cluster.kubeconfig"


def test_install_eks_passes_flags_as_env():
    wf = steps.build_llm_workflow(env="dev", backend=BE, enable_chat_db_tools=False,
                                  flags={"DEBUG_MODE": True, "MIGRATION_ENABLED": False})
    install = next(s for s in wf if s.name == "install-eks")
    assert install.env["DEBUG_MODE"] == "true"
    assert install.env["MIGRATION_ENABLED"] == "false"


def test_llm_workflow_order():
    wf = steps.build_llm_workflow(env="dev", backend=BE, enable_chat_db_tools=True, flags={})
    names = _names(wf)
    assert names.index("build-lambdas") < names.index("tf-init")
    assert names.index("tf-apply") < names.index("install-eks")
    assert names.index("install-eks") < names.index("verify-migration")


def test_tool_workflow_order():
    wf = steps.build_tool_workflow(backend=BE)
    names = _names(wf)
    assert names.index("tf-init") < names.index("tf-apply")


def test_tool_workflow_injects_key_file_env_not_tfvars():
    # ★함정: API 키는 tfvars가 아니라 TOOL_KEY_FILE env로만 넘어가야 한다
    #   (tfstate 평문 저장 + undeclared variable 경고 방지).
    from pathlib import Path

    wf = steps.build_tool_workflow(backend=BE, key_file=Path("/tmp/keys.env"))
    apply = next(s for s in wf if s.name == "tf-apply")
    assert apply.env["TOOL_KEY_FILE"] == "/tmp/keys.env"


def test_tool_workflow_no_key_file_leaves_env_unset():
    # 키가 없으면(예: DuckDuckGo만) TOOL_KEY_FILE을 주입하지 않는다 → seed 스킵.
    wf = steps.build_tool_workflow(backend=BE)
    apply = next(s for s in wf if s.name == "tf-apply")
    assert apply.env is None


def test_llm_teardown_order_helm_before_destroy():
    # ★함정: helm uninstall(ALB 제거)이 terraform destroy보다 먼저여야 VPC가 지워진다
    wf = steps.build_llm_teardown(env="dev", backend=BE)
    names = _names(wf)
    assert names.index("helm-uninstall") < names.index("tf-init")
    assert names.index("tf-init") < names.index("tf-destroy")


def test_llm_teardown_isolates_kubeconfig_and_is_skippable():
    wf = steps.build_llm_teardown(env="dev", backend=BE, cluster_name="my-cluster")
    helm = next(s for s in wf if s.name == "helm-uninstall")
    assert helm.env["KUBECONFIG"] == "/tmp/my-cluster.kubeconfig"
    assert helm.skippable is True  # best-effort — 릴리스 없어도 destroy는 진행


def test_llm_teardown_destroy_injects_backend():
    wf = steps.build_llm_teardown(env="dev", backend=BE)
    tf_init = next(s for s in wf if s.name == "tf-init")
    assert "-backend-config=bucket=b" in tf_init.argv


def test_tool_teardown_calls_provision_script():
    wf = steps.build_tool_teardown()
    assert wf[0].name == "tool-teardown"
    assert "teardown" in wf[0].argv


def test_tool_teardown_cleans_up_credential_providers_after_destroy():
    # ★재발 방지: destroy 후 orphan credential provider 정리 스텝이 뒤따라야 함
    wf = steps.build_tool_teardown()
    names = _names(wf)
    assert names == ["tool-teardown", "cleanup-credential-providers"]
    cleanup = wf[1]
    assert cleanup.skippable is True  # best-effort — 없으면 조용히 통과


def test_cleanup_targets_only_managed_provider_names():
    # 다른 스택의 `-creds` 접미 provider나 무관 provider를 오삭제하면 안 됨
    wf = steps.build_tool_teardown()
    script = wf[1].argv[-1]
    for name in steps.MANAGED_CREDENTIAL_PROVIDERS:
        assert name in script
    # 문자열 완전 일치 비교로 부분일치 오삭제 방지 (grep -w는 '-'를 경계로 오탐)
    assert '[ "$e" = "$n" ]' in script
    assert "grep -w" not in script
    # 이 스택이 만들지 않는 이름은 정리 대상에 없어야 함
    assert "brave-creds" not in steps.MANAGED_CREDENTIAL_PROVIDERS
    assert "NasaInsightAPIKey" not in steps.MANAGED_CREDENTIAL_PROVIDERS


def test_cleanup_exact_match_does_not_target_creds_suffix():
    # tavily가 tavily-creds에 오탐되지 않도록 완전 일치 로직 검증 (실 셸 실행)
    import subprocess

    script = steps.build_tool_teardown()[1].argv[-1]
    # aws를 가짜로 대체: list는 -creds 이름만 반환, delete 호출은 기록
    harness = (
        'aws() {\n'
        '  if [ "$2" = "list-api-key-credential-providers" ]; then\n'
        '    echo "NasaInsightAPIKey brave-creds tavily-creds"; return 0; fi\n'
        '  if [ "$2" = "delete-api-key-credential-provider" ]; then\n'
        '    echo "DELETE_CALLED $4"; return 0; fi\n'
        '}\n'
    )
    out = subprocess.run(
        ["bash", "-c", harness + script],
        capture_output=True, text=True,
    ).stdout
    assert "DELETE_CALLED" not in out  # -creds 이름은 절대 삭제 대상 아님
