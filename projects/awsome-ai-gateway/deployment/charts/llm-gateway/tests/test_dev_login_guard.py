# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""dev 로그인은 개발 환경에서만 — 차트가 **렌더 단계에서** 막는지 확인한다.

왜 렌더 테스트인가 — dev 로그인은 서명 없는 토큰을 받아 payload 의 role 을 그대로 믿는다.
``DEV_LOGIN_ENABLED=true`` 가 운영 values 에 들어가면 관리자 API 가 인증 없이 열린다. 예전엔
그 조합에 NOTES.txt 가 경고만 했는데, 경고는 설치가 끝난 **뒤에** 출력된다. 지금은
``templates/common/validation.yaml`` 이 렌더를 실패시킨다 — 잘못된 값이 클러스터에 닿지 않는다.

이 파일이 고정하는 것:
  1. 스위치가 켜져 있고 환경이 개발 계열이 아니면 ``helm template`` 이 실패한다(두 컨테이너 각각).
  2. 정상 조합(커밋된 dev·prod overlay 포함)은 그대로 렌더된다 — 이 대조군이 없으면
     "항상 실패해서 통과" 하는 공허한 테스트가 된다.
  3. admin-ui 가 admin-api 와 같은 ``APP_ENV`` 를 받는다 — admin-ui 의 판정이 그 값에 달려 있다.
  4. 허용 목록이 차트·admin-api·admin-ui 세 곳에서 같다.

⚠️ ``helm`` 바이너리를 쓴다. 없으면 skip 하되 CI 에서는 실패시킨다.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="pyyaml 이 필요하다(렌더 결과 파싱)")

CHART = Path(__file__).resolve().parents[1]
PROJECT = CHART.parents[2]
DEV_OVERLAY = CHART / "values-eks-fargate-dev.yaml"
PROD_OVERLAY = CHART / "values-eks-fargate-prod.yaml"


def _helm() -> str:
    exe = shutil.which("helm")
    if exe is None:
        if os.environ.get("CI"):
            raise AssertionError("CI 인데 helm 이 없다 — dev 로그인 가드가 아무 데서도 검증되지 않는다.")
        pytest.skip("helm 바이너리가 없다")
    return exe


def _template(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([_helm(), "template", "guard-test", str(CHART), *args], capture_output=True, text=True)


def _docs(*args: str) -> list[dict]:
    proc = _template(*args)
    assert proc.returncode == 0, f"helm template 실패:\n{proc.stderr[-2000:]}"
    docs = [d for d in yaml.safe_load_all(proc.stdout) if d]
    assert len(docs) > 5, f"렌더 결과가 {len(docs)}개뿐이다 — 차트 경로/값을 확인"
    return docs


def _env_of(docs: list[dict], name_fragment: str) -> dict[str, str | None]:
    for d in docs:
        if d.get("kind") == "Deployment" and name_fragment in d["metadata"]["name"]:
            # 같은 이름이 여러 번 오면 쿠버네티스는 마지막을 쓴다 — dict 빌드 순서가 그 동작과 같다.
            return {e["name"]: e.get("value") for e in d["spec"]["template"]["spec"]["containers"][0].get("env", [])}
    raise AssertionError(f"{name_fragment} Deployment 를 렌더 결과에서 찾지 못했다")


def _on(component: str) -> tuple[str, str]:
    return ("--set-string", f"{component}.env.DEV_LOGIN_ENABLED=true")


# ─────────────────────────────────────────────────────────────────────────────
# 1. 막아야 하는 조합
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("component", ["adminApi", "adminUi"])
@pytest.mark.parametrize("environment", ["prod", "production", "PROD", "staging", "stg", "qa", "demo"])
def test_switch_on_outside_a_dev_environment_fails_the_render(component: str, environment: str):
    proc = _template("--set", f"global.environment={environment}", *_on(component))
    assert proc.returncode != 0, "렌더가 성공했다 — dev 로그인이 켜진 채로 배포된다"
    # 운영자가 어느 키를 고쳐야 하는지 메시지로 알 수 있어야 한다.
    assert f"{component}.env.DEV_LOGIN_ENABLED=true" in proc.stderr
    assert f'APP_ENV="{environment}"' in proc.stderr


@pytest.mark.parametrize("component", ["adminApi", "adminUi"])
def test_the_committed_prod_overlay_cannot_be_flipped_on(component: str):
    proc = _template("-f", str(PROD_OVERLAY), *_on(component))
    assert proc.returncode != 0
    assert f"{component}.env.DEV_LOGIN_ENABLED=true" in proc.stderr


def test_a_yaml_boolean_true_is_caught_too():
    """``--set`` 은 따옴표 없는 true 를 불리언으로 넣는다. 컨테이너에는 "true" 로 렌더된다."""
    proc = _template("--set", "global.environment=prod", "--set", "adminApi.env.DEV_LOGIN_ENABLED=true")
    assert proc.returncode != 0
    assert "adminApi.env.DEV_LOGIN_ENABLED=true" in proc.stderr


def test_the_effective_app_env_is_what_counts():
    """컨테이너가 실제로 받는 값으로 판정한다 — ``<component>.env.APP_ENV`` override 가 이긴다."""
    proc = _template("-f", str(DEV_OVERLAY), "--set-string", "adminApi.env.APP_ENV=production")
    assert proc.returncode != 0
    assert 'APP_ENV="production"' in proc.stderr


# ─────────────────────────────────────────────────────────────────────────────
# 2. 대조군 — 정상 조합은 렌더된다
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("environment", ["dev", "development", "local", "test"])
def test_switch_on_in_a_dev_environment_renders(environment: str):
    docs = _docs("--set", f"global.environment={environment}", *_on("adminApi"), *_on("adminUi"))
    assert _env_of(docs, "admin-api")["DEV_LOGIN_ENABLED"] == "true"
    assert _env_of(docs, "admin-ui")["DEV_LOGIN_ENABLED"] == "true"


@pytest.mark.parametrize("environment", ["prod", "production", "staging"])
def test_switch_off_renders_everywhere(environment: str):
    docs = _docs("--set", f"global.environment={environment}")
    assert _env_of(docs, "admin-api")["DEV_LOGIN_ENABLED"] == "false"


def test_the_committed_overlays_render_as_committed():
    dev = _docs("-f", str(DEV_OVERLAY))
    assert _env_of(dev, "admin-api")["DEV_LOGIN_ENABLED"] == "true"
    assert _env_of(dev, "admin-ui")["DEV_LOGIN_ENABLED"] == "true"

    prod = _docs("-f", str(PROD_OVERLAY))
    assert _env_of(prod, "admin-api")["DEV_LOGIN_ENABLED"] == "false"
    assert _env_of(prod, "admin-ui")["DEV_LOGIN_ENABLED"] == "false"


# ─────────────────────────────────────────────────────────────────────────────
# 3. admin-ui 는 admin-api 와 같은 APP_ENV 를 받는다
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("overlay", [None, DEV_OVERLAY, PROD_OVERLAY], ids=["defaults", "dev", "prod"])
def test_both_containers_get_the_same_app_env(overlay: Path | None):
    docs = _docs(*(("-f", str(overlay)) if overlay else ()))
    api, ui = _env_of(docs, "admin-api"), _env_of(docs, "admin-ui")
    assert api["APP_ENV"], "admin-api 에 APP_ENV 가 없다"
    assert ui.get("APP_ENV") == api["APP_ENV"]


# ─────────────────────────────────────────────────────────────────────────────
# 4. 허용 목록은 세 곳에서 같다
# ─────────────────────────────────────────────────────────────────────────────


def _quoted(text: str) -> set[str]:
    return set(re.findall(r"""["']([a-z]+)["']""", text))


def test_the_allow_list_is_the_same_in_chart_admin_api_and_admin_ui():
    """한 곳만 바꾸면 폼은 열리는데 API 는 401 인(또는 렌더는 되는데 기동은 안 되는) 상태가 된다."""
    helpers = (CHART / "templates" / "_helpers.tpl").read_text(encoding="utf-8")
    chart = re.search(r'define "llm-gateway\.validateDevLogin".*?\$allowed := list ([^\n]*?) -}}', helpers, re.S)
    assert chart, "차트 허용 목록을 찾지 못했다"

    # admin-api 는 목록을 core/environment.py 한 곳에 두고 dev 로그인과 /internal/* 가드가 함께 쓴다.
    api_src = (PROJECT / "admin-api" / "src" / "app" / "core" / "environment.py").read_text(encoding="utf-8")
    api = re.search(r"DEVELOPMENT_ENVIRONMENTS = frozenset\(\{([^}]*)\}\)", api_src)
    assert api, "admin-api 허용 목록을 찾지 못했다"
    dev_login_src = (PROJECT / "admin-api" / "src" / "app" / "core" / "dev_login.py").read_text(encoding="utf-8")
    assert "DEV_LOGIN_ENVIRONMENTS = DEVELOPMENT_ENVIRONMENTS" in dev_login_src, "dev 로그인이 다른 목록을 쓴다"

    ui_src = (PROJECT / "admin-ui" / "src" / "lib" / "devLogin.ts").read_text(encoding="utf-8")
    ui = re.search(r"DEV_LOGIN_ENVIRONMENTS[^=]*= \[([^\]]*)\]", ui_src)
    assert ui, "admin-ui 허용 목록을 찾지 못했다"

    expected = {"dev", "development", "local", "test"}
    assert _quoted(chart.group(1)) == expected
    assert _quoted(api.group(1)) == expected
    assert _quoted(ui.group(1)) == expected


# ─────────────────────────────────────────────────────────────────────────────
# 5. 콘솔 SSO client id — 쓸 때만 렌더된다
# ─────────────────────────────────────────────────────────────────────────────


def test_console_sso_client_id_is_not_rendered_unless_set():
    """빈 값을 항상 찍으면 이 기능을 안 쓰는 설치의 admin-api 파드 스펙이 바뀐다."""
    docs = _docs("--set", "adminApi.oidc.enabled=true")
    assert "OIDC_ADMIN_UI_CLIENT_ID" not in _env_of(docs, "admin-api")


def test_console_sso_client_id_reaches_admin_api_only():
    docs = _docs("--set", "adminApi.oidc.enabled=true", "--set", "adminApi.oidc.adminUiClientId=web-client-1")
    assert _env_of(docs, "admin-api")["OIDC_ADMIN_UI_CLIENT_ID"] == "web-client-1"
    assert "OIDC_ADMIN_UI_CLIENT_ID" not in _env_of(docs, "admin-ui")
