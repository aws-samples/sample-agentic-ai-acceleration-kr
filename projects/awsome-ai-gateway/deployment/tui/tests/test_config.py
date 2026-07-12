from deployment.tui import config


def test_to_tfvars_types():
    out = config.to_tfvars({"project": "llm-gateway", "enable_x": True, "arns": ["a", "b"]})
    assert 'project = "llm-gateway"' in out
    assert "enable_x = true" in out
    assert 'arns = ["a", "b"]' in out


def test_find_placeholders_flags_change_me():
    vals = {"cognito_domain_suffix": "vanilla-auth-CHANGE_ACCOUNT_ID", "ok": "real"}
    assert config.find_placeholders(vals) == ["cognito_domain_suffix"]


def test_find_placeholders_empty_when_clean():
    assert config.find_placeholders({"a": "real", "b": "123456789012"}) == []


def test_parse_tfvars_roundtrip():
    text = 'project = "llm-gateway"\nenable_x = true\n# comment\n\nenv = "dev"'
    parsed = config.parse_tfvars(text)
    assert parsed["project"] == "llm-gateway"
    assert parsed["enable_x"] is True
    assert parsed["env"] == "dev"


def test_write_tfvars(tmp_path):
    p = tmp_path / "terraform.tfvars"
    config.write_tfvars(p, {"env": "dev"})
    assert 'env = "dev"' in p.read_text()


def test_backend_config_args():
    bc = config.BackendConfig(bucket="llm-gateway-tfstate-123", dynamodb_table="llm-gateway-tflock")
    assert bc.backend_args() == [
        "-backend-config=bucket=llm-gateway-tfstate-123",
        "-backend-config=dynamodb_table=llm-gateway-tflock",
    ]


def test_backend_config_omits_region_when_empty():
    # region 미지정 시 backend.tf 하드코딩 region을 쓰도록 -backend-config에 추가 안 함
    bc = config.BackendConfig(bucket="b", dynamodb_table="t")
    assert not any(a.startswith("-backend-config=region=") for a in bc.backend_args())


def test_backend_config_injects_region_when_set():
    bc = config.BackendConfig(bucket="b", dynamodb_table="t", region="us-west-2")
    assert "-backend-config=region=us-west-2" in bc.backend_args()
