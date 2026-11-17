# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""dev 로그인은 dev 계열 환경에서만 켜진다(``core/dev_login.py``).

배경 — dev 로그인은 ``dev.<base64url payload>.sig`` 토큰을 **서명 검증 없이** 받아 payload 의
``role`` 을 신원으로 삼는다. 게이트가 ``DEV_LOGIN_ENABLED == "true"`` 하나였기 때문에, 그 값이
운영 배포에 들어가면 관리자 API 전체가 인증 없이 열렸다(헤더와 ``admin_jwt`` 쿠키 양쪽).

여기서 고정하는 것:

  1. 환경이 dev 계열이 아니면 스위치가 켜져 있어도 dev 토큰은 **신원이 되지 않는다**.
  2. 그 조합이면 프로세스가 **기동하지 않는다** — 조용히 무시하지 않는다.
  3. dev 계열 환경에서는 예전과 똑같이 동작한다(대조군). 이 단정이 없으면 "가드가 전부
     막아서 통과" 하는 공허한 테스트가 된다.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.core import dev_login
from app.core.auth import JWTVerifier, _parse_dev_token, get_current_user
from app.core.dev_login import DevLoginNotAllowedError, refuse_dev_login_outside_dev
from app.models.auth import UserRole

DEV_ENVS = ["dev", "development", "local", "test", "DEV", " Development "]
NON_DEV_ENVS = ["prod", "production", "PROD", "staging", "stg", "qa", "prd", "demo", "", "developmentx"]


def _dev_token(role: str = "ADMIN") -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"role": role, "email": "x@dev.local"}).encode())
    return f"dev.{payload.decode().rstrip('=')}.sig"


def _app_env(value: str):
    return patch.object(dev_login, "get_settings", return_value=MagicMock(APP_ENV=value))


def _request(token: str, *, cookie: bool = False) -> MagicMock:
    request = MagicMock()
    request.headers = {"Cookie": f"admin_jwt={token}"} if cookie else {"Authorization": f"Bearer {token}"}
    # 키가 하나도 없는 **진짜** 검증기 — dev 경로를 못 타면 여기서 401 이 나야 한다.
    request.app.state.jwt_verifier = JWTVerifier()
    request.app.state.console_oidc_verifier = None
    return request


# ─────────────────────────────────────────────────────────────────────────────
# 1. 요청 경로
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("app_env", DEV_ENVS)
@pytest.mark.parametrize("cookie", [False, True])
async def test_dev_token_is_an_identity_in_dev_environments(monkeypatch, app_env, cookie):
    """대조군 — dev 계열에서는 예전 그대로 ADMIN 이 된다."""
    monkeypatch.setenv("DEV_LOGIN_ENABLED", "true")
    with _app_env(app_env):
        user = await get_current_user(_request(_dev_token("ADMIN"), cookie=cookie))
    assert user.role == UserRole.ADMIN
    assert user.email == "x@dev.local"


@pytest.mark.parametrize("app_env", NON_DEV_ENVS)
@pytest.mark.parametrize("cookie", [False, True])
async def test_dev_token_is_rejected_outside_dev_even_with_the_switch_on(monkeypatch, app_env, cookie):
    monkeypatch.setenv("DEV_LOGIN_ENABLED", "true")
    with _app_env(app_env):
        assert _parse_dev_token(_dev_token()) is None
        with pytest.raises(HTTPException) as exc:
            await get_current_user(_request(_dev_token("ADMIN"), cookie=cookie))
    assert exc.value.status_code == 401


@pytest.mark.parametrize("flag", [None, "false", "True", "TRUE", "1", "yes", ""])
def test_the_switch_still_has_to_be_exactly_true(monkeypatch, flag):
    """환경 가드를 넣으면서 스위치 판정을 느슨하게 만들지 않았다."""
    if flag is None:
        monkeypatch.delenv("DEV_LOGIN_ENABLED", raising=False)
    else:
        monkeypatch.setenv("DEV_LOGIN_ENABLED", flag)
    with _app_env("dev"):
        assert _parse_dev_token(_dev_token()) is None


# ─────────────────────────────────────────────────────────────────────────────
# 2. 기동
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("app_env", NON_DEV_ENVS)
def test_startup_check_refuses_the_switch_outside_dev(monkeypatch, app_env):
    monkeypatch.setenv("DEV_LOGIN_ENABLED", "true")
    with _app_env(app_env), pytest.raises(DevLoginNotAllowedError) as exc:
        refuse_dev_login_outside_dev()
    # 운영자가 로그 한 줄로 원인과 조치를 알 수 있어야 한다.
    assert "DEV_LOGIN_ENABLED=true" in str(exc.value)
    assert repr(app_env) in str(exc.value)


@pytest.mark.parametrize(
    ("flag", "app_env"),
    [("true", env) for env in DEV_ENVS] + [("false", "prod"), (None, "prod"), ("false", "production")],
)
def test_startup_check_passes_for_every_safe_combination(monkeypatch, flag, app_env):
    if flag is None:
        monkeypatch.delenv("DEV_LOGIN_ENABLED", raising=False)
    else:
        monkeypatch.setenv("DEV_LOGIN_ENABLED", flag)
    with _app_env(app_env):
        refuse_dev_login_outside_dev()


async def test_lifespan_stops_before_opening_any_resource(monkeypatch):
    """가드는 DB·Redis 를 열기 **전에** 터진다 — 반쯤 뜬 프로세스를 남기지 않는다."""
    from app import main

    monkeypatch.setenv("DEV_LOGIN_ENABLED", "true")
    engine = MagicMock()
    redis = AsyncMock()
    with (
        _app_env("prod"),
        patch.object(main, "create_engine", engine),
        patch.object(main, "create_redis_client", redis),
        pytest.raises(DevLoginNotAllowedError),
    ):
        async with main.lifespan(main.app):
            pytest.fail("the application must not start")
    engine.assert_not_called()
    redis.assert_not_called()


def test_the_real_server_process_exits_instead_of_serving():
    """실제 uvicorn 프로세스로 확인한다 — 예외가 어딘가에서 삼켜지면 위 테스트로는 안 보인다."""
    admin_api = Path(__file__).resolve().parents[2]
    env = {
        **os.environ,
        "PYTHONPATH": str(admin_api / "src"),
        "APP_ENV": "prod",
        "DEV_LOGIN_ENABLED": "true",
    }
    proc = subprocess.run(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "0"],
        cwd=admin_api,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, output[-2000:]
    assert "DevLoginNotAllowedError" in output, output[-2000:]
    assert "Application startup failed" in output, output[-2000:]
