# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""관리 콘솔 SSO 의 배선 — terraform 이 만든 app client 가 Helm 값까지 이어지는지 확인한다.

관리 콘솔(admin-ui)의 정식 로그인은 세 곳이 맞물려야 열린다:

  terraform   admin-ui 용 Cognito app client (``cognito_admin_ui_callback_urls`` 를 채웠을 때만)
  install-eks 그 client id 와 hosted UI 엔드포인트를 ``--set`` 으로 Helm 에 전달
  chart       admin-ui 는 그 값으로 IdP 에 보내고, admin-api 는 **같은 client id** 로 발급된 토큰만 받는다

한 곳이 어긋나면 로그인 화면은 뜨는데 모든 API 가 401 이다 — 그리고 CI 에는 terraform 잡이 없다.
그래서 "기본값은 아무것도 만들지 않는다" 와 "두 컨테이너가 같은 client id 를 받는다" 를 여기서
정적으로 고정한다.

⚠️ HCL·bash 를 정규식으로 읽는다. 확인하려는 것은 "특정 문자열이 특정 블록 안에 있는가" 이고,
   각 단정에 대조군(다른 블록에는 그 성질이 없다)이 붙어 있다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CHART = Path(__file__).resolve().parents[1]
DEPLOYMENT = CHART.parents[1]
TF = DEPLOYMENT / "terraform"
MODULE = TF / "modules" / "cognito"
ENVIRONMENTS = ["llm-gateway-dev", "llm-gateway-prod"]


def _read(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    assert len(text) > 200, f"{path} 가 너무 짧다 — 경로가 틀렸을 것이다"
    return text


def _blocks(hcl: str, kind: str) -> dict[str, str]:
    """최상위 ``kind "a" "b" { ... }`` 블록들을 이름→본문으로. 중괄호 깊이로 자른다."""
    out: dict[str, str] = {}
    pattern = re.compile(rf'^{kind}\s+("(?:[^"]+)"(?:\s+"[^"]+")?)\s*\{{', re.M)
    for m in pattern.finditer(hcl):
        name = " ".join(p.strip('"') for p in m.group(1).split())
        depth, i = 1, m.end()
        while i < len(hcl) and depth:
            depth += {"{": 1, "}": -1}.get(hcl[i], 0)
            i += 1
        out[name] = hcl[m.end() : i - 1]
    return out


@pytest.fixture(scope="module")
def clients() -> dict[str, str]:
    resources = _blocks(_read(MODULE / "main.tf"), "resource")
    return {
        "cli": resources["aws_cognito_user_pool_client cli"],
        "admin_ui": resources["aws_cognito_user_pool_client admin_ui"],
    }


# ─────────────────────────────────────────────────────────────────────────────
# 1. terraform — 기본값은 아무것도 만들지 않는다
# ─────────────────────────────────────────────────────────────────────────────


def test_the_admin_ui_client_is_only_created_when_callback_urls_are_given(clients):
    assert re.search(r"count\s*=\s*length\(var\.admin_ui_callback_urls\) > 0 \? 1 : 0", clients["admin_ui"])
    # 대조군 — CLI client 는 항상 만든다.
    assert "count" not in clients["cli"]


def test_callback_urls_default_to_empty_everywhere():
    variables = _blocks(_read(MODULE / "variables.tf"), "variable")
    assert re.search(r"default\s*=\s*\[\]", variables["admin_ui_callback_urls"])
    for env in ENVIRONMENTS:
        env_vars = _blocks(_read(TF / "environments" / env / "variables.tf"), "variable")
        assert re.search(r"default\s*=\s*\[\]", env_vars["cognito_admin_ui_callback_urls"]), env
        # 대조군 — CLI 콜백은 기본값이 비어 있지 않다(같은 정규식이 아무 데나 맞지 않는다).
        assert not re.search(r"default\s*=\s*\[\]", env_vars["cognito_callback_urls"]), env


def test_plain_http_callbacks_are_rejected_at_plan_time():
    """Cognito 는 https 콜백만 받는다. apply 중간에 죽는 대신 plan 에서 알린다."""
    variable = _blocks(_read(MODULE / "variables.tf"), "variable")["admin_ui_callback_urls"]
    pattern = re.search(r'regex\("([^"]+)"', variable)
    assert pattern, "콜백 URL 검증이 없다"
    rule = re.compile(pattern.group(1).replace("\\\\", "\\"))
    assert rule.search("https://admin.example.com/api/auth/callback")
    assert rule.search("http://localhost:3000/api/auth/callback")
    assert not rule.search("http://admin.example.com/api/auth/callback")
    assert not rule.search("http://localhost.evil.example/api/auth/callback")
    # 호스트 자리에 userinfo 를 끼운 값 — 보이는 이름과 실제 호스트가 다르다.
    assert not rule.search("https://admin.example.com@other.example/api/auth/callback")
    assert not rule.search("https://user:pass@admin.example.com/api/auth/callback")
    assert rule.search("https://admin.example.com:8443/api/auth/callback")


def test_the_admin_ui_client_is_a_code_flow_client_without_password_auth(clients):
    admin_ui = clients["admin_ui"]
    assert re.search(r'allowed_oauth_flows\s*=\s*\["code"\]', admin_ui)
    assert re.search(r"generate_secret\s*=\s*false", admin_ui)
    # 브라우저 hosted UI 만 쓴다 — 비밀번호를 API 로 직접 받는 흐름은 열지 않는다.
    assert "ALLOW_USER_PASSWORD_AUTH" not in admin_ui
    # 대조군 — CLI client 는 그 흐름을 연다(같은 문자열 검사가 공허하지 않다).
    assert "ALLOW_USER_PASSWORD_AUTH" in clients["cli"]


def test_the_client_id_output_is_null_when_the_client_does_not_exist():
    outputs = _blocks(_read(MODULE / "outputs.tf"), "output")
    assert re.search(r"try\(aws_cognito_user_pool_client\.admin_ui\[0\]\.id, null\)", outputs["admin_ui_client_id"])


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_each_environment_wires_the_module_and_exposes_the_outputs(env: str):
    main = _blocks(_read(TF / "environments" / env / "main.tf"), "module")["cognito"]
    assert re.search(r"admin_ui_callback_urls\s*=\s*var\.cognito_admin_ui_callback_urls", main)
    assert re.search(r"admin_ui_logout_urls\s*=\s*var\.cognito_admin_ui_logout_urls", main)
    assert re.search(r"admin_ui_token_validity_hours\s*=\s*var\.cognito_admin_ui_token_validity_hours", main)

    outputs = _blocks(_read(TF / "environments" / env / "outputs.tf"), "output")
    assert "module.cognito.admin_ui_client_id" in outputs["cognito_admin_ui_client_id"]
    assert "module.cognito.oauth_authorize_url" in outputs["cognito_oauth_authorize_url"]
    assert "module.cognito.oauth_token_url" in outputs["cognito_oauth_token_url"]


# ─────────────────────────────────────────────────────────────────────────────
# 2. install-eks.sh — 두 컨테이너에 같은 client id 를 넣는다
# ─────────────────────────────────────────────────────────────────────────────


def test_install_script_reads_the_outputs_the_environments_expose():
    script = _read(DEPLOYMENT / "scripts" / "install-eks.sh")
    for output in ("cognito_admin_ui_client_id", "cognito_oauth_authorize_url", "cognito_oauth_token_url"):
        assert f".{output}.value // empty" in script, output


def test_install_script_gives_both_containers_the_same_client_id():
    """admin-ui 가 로그인에 쓰는 client 와 admin-api 가 받는 client 가 다르면 전부 401 이다."""
    script = _read(DEPLOYMENT / "scripts" / "install-eks.sh")
    api = re.search(r'"adminApi\.oidc\.adminUiClientId=\$\{(\w+)\}"', script)
    ui = re.search(r'"adminUi\.env\.OIDC_CLIENT_ID=\$\{(\w+)\}"', script)
    assert api and ui, "client id 주입 줄을 찾지 못했다"
    assert api.group(1) == ui.group(1) == "COGNITO_ADMIN_UI_CLIENT_ID"
    # 대조군 — CLI client id 변수는 따로 있고 여기에 쓰이지 않는다.
    assert "COGNITO_CLIENT_ID=" in script and api.group(1) != "COGNITO_CLIENT_ID"


def test_install_script_injects_nothing_when_the_client_was_not_created():
    """값이 없으면 한 줄도 넣지 않는다 — 관리 콘솔 SSO 를 안 쓰는 설치의 렌더가 그대로다."""
    script = _read(DEPLOYMENT / "scripts" / "install-eks.sh")
    guard = re.search(
        r"if ([^\n]*\n?[^\n]*); then\n\s+SET_ARGS\+=\(\n\s+--set-string \"adminApi\.oidc\.adminUiClientId=", script
    )
    assert guard, "주입 블록의 조건을 찾지 못했다"
    for var in ("COGNITO_ISSUER_URL", "COGNITO_ADMIN_UI_CLIENT_ID", "COGNITO_AUTHORIZE_URL", "COGNITO_TOKEN_URL"):
        assert f'[ -n "${{{var}}}" ]' in guard.group(1), var
