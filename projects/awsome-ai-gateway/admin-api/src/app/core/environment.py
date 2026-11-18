# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

""" "지금 개발 환경인가" 를 **한 곳에서** 판정한다.

개발 편의 기능(서명 없는 dev 로그인, 인증 없는 ``/internal/*``)은 ``APP_ENV`` 가 개발 계열일
때만 열린다. 예전에는 기능마다 문자열을 따로 비교했고 그 값이 어긋나 있었다 — ``/internal/*``
의 가드는 ``APP_ENV == "production"`` 을 봤는데 Helm 차트는 운영 환경에 ``"prod"`` 를 넣는다.
문자열 비교는 값이 달라도 에러를 내지 않으므로 그 가드는 운영 환경에서 한 번도 동작하지
않았고, 인증 없이 virtual key 를 발급하는 라우트가 그대로 열려 있었다.

그래서 금지 목록이 아니라 **허용 목록**이다. 목록에 없는 이름(``prod``, ``production``,
``staging``, 오타)은 전부 개발 환경이 아니다. 금지 목록은 표기가 하나만 달라져도 조용히 죽는다.
"""

from __future__ import annotations

from fastapi import HTTPException

from app.core.config import get_settings

# 개발 환경으로 치는 APP_ENV 값(소문자 비교). APP_ENV 의 기본값은 "development" 다.
# Helm 차트(templates/_helpers.tpl validateDevLogin)와 admin-ui(src/lib/devLogin.ts)가 같은 목록을
# 쓴다 — 차트 테스트가 세 곳이 같은지 확인한다.
DEVELOPMENT_ENVIRONMENTS = frozenset({"dev", "development", "local", "test"})


def is_development_environment(app_env: str) -> bool:
    return app_env.strip().lower() in DEVELOPMENT_ENVIRONMENTS


def require_development_environment() -> None:
    """FastAPI 의존성 — 개발 환경이 아니면 403.

    라우터의 ``dependencies=[...]`` 로 건다. 경로 의존성은 핸들러의 파라미터 의존성(DB 세션 등)
    보다 먼저 풀리므로, 막힌 요청은 DB 커넥션을 잡지 않는다.
    """
    if not is_development_environment(get_settings().APP_ENV):
        raise HTTPException(status_code=403, detail="Not available outside development environments")
