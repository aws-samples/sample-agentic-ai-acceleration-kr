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


def test_to_tfvars_nested_dict_block():
    # dict 값은 평평한 key=value가 아니라 중첩 블록으로 직렬화돼야 한다.
    out = config.to_tfvars(
        {
            "eks_access_entries": {
                "developer": {
                    "principal_arn": "arn:aws:iam::1:user/x",
                    "policy_associations": {
                        "admin": {"access_scope": {"type": "cluster"}}
                    },
                }
            },
            "tags": {"CostCenter": "platform-ai"},
        }
    )
    assert "eks_access_entries = {" in out
    assert "developer = {" in out
    assert 'principal_arn = "arn:aws:iam::1:user/x"' in out
    assert 'type = "cluster"' in out
    assert "tags = {" in out
    assert 'CostCenter = "platform-ai"' in out
    # 최상위에 새어나온 scalar가 없어야 함
    assert "\nprincipal_arn = " not in out
    assert "\ntype = " not in out
    assert "\nCostCenter = " not in out


def test_parse_tfvars_skips_nested_block_keys():
    # 중첩 블록 내부의 principal_arn/type/CostCenter는 최상위로 승격되면 안 된다.
    text = (
        'project = "llm-gateway"\n'
        "eks_access_entries = {\n"
        "  developer = {\n"
        '    principal_arn = "arn:aws:iam::1:user/x"\n'
        "    policy_associations = {\n"
        "      admin = {\n"
        '        policy_arn = "arn:aws:eks::aws:cluster-access-policy/X"\n'
        "        access_scope = {\n"
        '          type = "cluster"\n'
        "        }\n"
        "      }\n"
        "    }\n"
        "  }\n"
        "}\n"
        "tags = {\n"
        '  CostCenter = "platform-ai"\n'
        "}\n"
        'environment = "dev"\n'
    )
    parsed = config.parse_tfvars(text)
    assert parsed == {"project": "llm-gateway", "environment": "dev"}
    for leaked in ("principal_arn", "policy_arn", "type", "CostCenter", "access_scope"):
        assert leaked not in parsed


def test_prefill_scalar_reads_nested_value():
    text = (
        "eks_access_entries = {\n"
        "  developer = {\n"
        '    principal_arn = "arn:aws:iam::1:user/x"\n'
        "  }\n"
        "}\n"
    )
    assert config.prefill_scalar(text, "principal_arn") == "arn:aws:iam::1:user/x"
    assert config.prefill_scalar(text, "missing") == ""


def test_tfvars_write_parse_roundtrip_stable():
    # cli.py가 만드는 구조를 직렬화→파싱→재직렬화해도 최상위 scalar 오염이 없어야 한다.
    values = {
        "project": "llm-gateway",
        "aws_region": "ap-northeast-2",
        "eks_access_entries": {
            "developer": {
                "principal_arn": "arn:aws:iam::1:user/x",
                "policy_associations": {
                    "admin": {
                        "policy_arn": "arn:aws:eks::aws:cluster-access-policy/X",
                        "access_scope": {"type": "cluster"},
                    }
                },
            }
        },
        "tags": {"CostCenter": "platform-ai", "Owner": "team"},
    }
    text = config.to_tfvars(values)
    reparsed = config.parse_tfvars(text)
    # 재파싱은 최상위 scalar만 — 블록 키가 새어나오면 안 됨
    assert reparsed == {"project": "llm-gateway", "aws_region": "ap-northeast-2"}


def test_write_tfvars(tmp_path):
    p = tmp_path / "terraform.tfvars"
    config.write_tfvars(p, {"env": "dev"})
    assert 'env = "dev"' in p.read_text()


def test_to_key_file_uses_seed_script_format():
    # seed-tool-secrets.sh는 `engine=value` 줄을 IFS='=' 로 읽는다
    out = config.to_key_file({"tavily": "tvly-abc", "brave": "BSA-xyz"})
    assert "tavily=tvly-abc\n" in out
    assert "brave=BSA-xyz\n" in out


def test_to_key_file_skips_empty_values():
    out = config.to_key_file({"tavily": "tvly-abc", "brave": ""})
    assert "tavily=tvly-abc\n" in out
    assert "brave" not in out


def test_write_key_file_is_0600_and_has_content():
    import os
    import stat

    p = config.write_key_file({"exa": "d89a33"})
    try:
        assert p.read_text() == "exa=d89a33\n"
        mode = stat.S_IMODE(os.stat(p).st_mode)
        assert mode == 0o600  # 평문 키 파일 — 소유자만 읽기
    finally:
        p.unlink()


def test_write_key_file_returns_none_when_no_keys():
    # DuckDuckGo만 켠 경우처럼 키가 하나도 없으면 파일을 만들지 않는다
    assert config.write_key_file({}) is None
    assert config.write_key_file({"brave": ""}) is None


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
