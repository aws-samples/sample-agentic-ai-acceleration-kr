# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""dev 로그인을 켤 수 있는 환경을 **한 곳에서** 정한다.

dev 로그인은 ``dev.<base64url payload>.sig`` 토큰을 **서명 검증 없이** 받아 payload 의
``role`` 을 그대로 신원으로 삼는다(``core/auth._parse_dev_token``). 즉 이 API 에 닿을 수 있는
누구나 ADMIN 이 된다. 로컬 개발과 dev 환경을 위한 편의 기능이고, 그 밖에서는 인증이
없는 것과 같다.

예전엔 게이트가 ``DEV_LOGIN_ENABLED == "true"`` 하나였다. 값 하나가 잘못 들어가면
(values 복사, ``--set`` 실수) 운영 환경의 관리자 API 가 그대로 열렸고, 차트의 NOTES 경고는
설치가 끝난 뒤에야 출력됐다. 그래서 스위치와 **환경 이름**을 함께 본다:

  * 요청 경로 — 환경이 dev 계열이 아니면 스위치가 켜져 있어도 dev 토큰을 받지 않는다.
  * 기동 — 그 조합이면 프로세스가 뜨지 않는다. 조용히 무시하면 운영자는 스위치가 왜
    안 먹는지 모르고, 잘못된 설정이 배포에 남는다.

⚠️ 허용 목록이다. 목록에 없는 이름(``prod``, ``production``, ``staging``, 오타)은 전부
   거부한다. 금지 목록으로 두면 ``prod`` 와 ``production`` 처럼 표기가 갈리는 순간 가드가
   죽는다.
"""

from __future__ import annotations

import os

from app.core.config import get_settings
from app.core.environment import DEVELOPMENT_ENVIRONMENTS, is_development_environment

# dev 로그인을 켤 수 있는 APP_ENV 값 — 개발 환경 목록 그대로다(core/environment.py).
DEV_LOGIN_ENVIRONMENTS = DEVELOPMENT_ENVIRONMENTS


class DevLoginNotAllowedError(RuntimeError):
    """dev 로그인이 dev 계열이 아닌 환경에서 켜져 있다. 기동을 멈춘다."""


def dev_login_requested() -> bool:
    return os.getenv("DEV_LOGIN_ENABLED") == "true"


def is_dev_login_environment(app_env: str) -> bool:
    return is_development_environment(app_env)


def dev_login_enabled() -> bool:
    """스위치가 켜져 있고 **환경도** dev 계열일 때만 True."""
    return dev_login_requested() and is_dev_login_environment(get_settings().APP_ENV)


def refuse_dev_login_outside_dev() -> None:
    """기동 시 한 번 부른다. dev 계열이 아닌 환경에서 스위치가 켜져 있으면 예외."""
    app_env = get_settings().APP_ENV
    if dev_login_requested() and not is_dev_login_environment(app_env):
        raise DevLoginNotAllowedError(
            f"DEV_LOGIN_ENABLED=true is not allowed when APP_ENV={app_env!r}. "
            "Dev login accepts unsigned tokens, so anyone who can reach this API becomes ADMIN. "
            "Set DEV_LOGIN_ENABLED=false and sign in through OIDC, "
            f"or run with APP_ENV set to one of {sorted(DEV_LOGIN_ENVIRONMENTS)}."
        )
