from deployment.tui import paths


def test_scripts_dir_contains_install_eks():
    assert (paths.SCRIPTS_DIR / "install-eks.sh").is_file()


def test_llm_tf_dir_uses_env():
    assert paths.llm_tf_dir("dev").name == "llm-gateway-dev"
    assert paths.llm_tf_dir("prod").name == "llm-gateway-prod"


def test_tool_tf_dir_exists():
    assert paths.TOOL_TF_DIR.name == "tool-gateway-dev"


def test_build_lambdas_script_exists():
    assert paths.BUILD_LAMBDAS_SH.is_file()
