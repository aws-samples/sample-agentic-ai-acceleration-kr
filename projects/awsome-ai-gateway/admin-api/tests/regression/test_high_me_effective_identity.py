# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""``GET /admin/me`` 는 API 가 판정한 신원을 그대로 돌려준다(``routers/me.py``).

배경 — admin-ui 는 역할을 IdP 토큰의 그룹만 보고 정했다. API 는 그룹·``ADMIN_EMAILS``·``auth.users``
의 역할을 함께 보므로, 이메일로만 관리자인 사용자와 모든 팀 리더가 API 는 받아 주는데 화면마다 403 을
봤다. 콘솔은 이제 이 라우트에 묻는다.

고정하는 것: (1) 역할은 서버 쪽 상태로 정해진다 — **같은 토큰**이 ADMIN 도 TEAM_LEADER 도 받는다.
토큰만 보고 역할을 만드는 핸들러는 이 대조군을 통과할 수 없다. (2) 거부는 ``get_current_user`` 의 것이
그대로 나간다. (3) 200 에는 ``Cache-Control: no-store`` 가 붙는다.

진짜 RSA 서명 토큰·``OIDCVerifier``·예외 핸들러(``register_exception_handlers``)로 라우트를 ASGI 로
탄다. 대신하는 것은 IdP(discovery·JWKS)와 ``auth.users`` 조회뿐이다.
"""

from __future__ import annotations

import base64
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.routing import APIRoute
from jose import jwt

from app.core.auth import JWTVerifier, get_current_user
from app.core.oidc_verifier import OIDCVerifier
from app.main import register_exception_handlers
from app.models.auth import User, UserRole
from app.routers import me

ISSUER = "https://idp.example.com/pool-1"
ADMIN_UI_CLIENT = "admin-ui-client-id"
CLI_CLIENT = "cli-client-id"
KID = "idp-key-1"
SUBJECT = "idp-subject-1"
EMAIL = "kim@example.com"
TEAM_ID = uuid.uuid4()


def _b64u(number: int) -> str:
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


_IDP_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
IDP_PRIVATE = _IDP_KEY.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
).decode()
_IDP_NUMBERS = _IDP_KEY.public_key().public_numbers()
IDP_JWK = {
    "kty": "RSA",
    "kid": KID,
    "use": "sig",
    "alg": "RS256",
    "n": _b64u(_IDP_NUMBERS.n),
    "e": _b64u(_IDP_NUMBERS.e),
}


def _idp(request: httpx.Request) -> httpx.Response:
    """discovery + JWKS 만 서빙하는 IdP."""
    if request.url.path.endswith("/.well-known/openid-configuration"):
        return httpx.Response(200, json={"issuer": ISSUER, "jwks_uri": f"{ISSUER}/.well-known/jwks.json"})
    if request.url.path.endswith("/.well-known/jwks.json"):
        return httpx.Response(200, json={"keys": [IDP_JWK]})
    return httpx.Response(404)


def _token(**overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "sub": SUBJECT,
        "aud": ADMIN_UI_CLIENT,
        "email": EMAIL,
        # 관리자 그룹이 아니다 — 그룹만 보면 이 토큰은 언제나 DEVELOPER 다.
        "groups": ["Claude_Platform"],
        "iat": now,
        "exp": now + 3600,
        "token_use": "id",
    }
    claims.update(overrides)
    return jwt.encode(claims, IDP_PRIVATE, algorithm="RS256", headers={"kid": KID})


def _settings(*, admin_emails: list[str] | None = None, required_group: str = "") -> MagicMock:
    return MagicMock(
        ADMIN_GROUPS=["GatewayAdmin"],
        ADMIN_EMAILS=admin_emails or [],
        OIDC_REQUIRED_GROUP=required_group,
        OIDC_GROUPS_CLAIM="groups",
        OIDC_USER_ID_CLAIM="sub",
        OIDC_EMAIL_CLAIM="email",
        OIDC_NAME_CLAIM="name",
    )


def _row(role: UserRole, *, team_id: uuid.UUID | None = None, is_active: bool = True) -> User:
    """``auth.users`` 행. 진짜 ORM 객체라 속성 이름이 실물과 어긋나면 여기서 드러난다."""
    return User(
        id=uuid.uuid4(),
        email=EMAIL,
        display_name="Kim",
        role=role,
        team_id=team_id,
        sso_subject=SUBJECT,
        is_active=is_active,
    )


def _forbidden(reason: str) -> dict:
    return {"error": {"type": "forbidden", "code": "FORBIDDEN", "message": reason}}


def _unauthorized(message: str) -> dict:
    return {"error": {"type": "unauthorized", "code": "UNAUTHORIZED", "message": message}}


class _Console:
    """``me.router`` 와 진짜 예외 핸들러만 붙인 앱.

    토큰은 ``admin_jwt`` 쿠키로 보낸다. ``bearer=True`` 면 ``Authorization`` 헤더로 보낸다.
    """

    def __init__(self) -> None:
        app = FastAPI()
        register_exception_handlers(app)
        app.include_router(me.router)
        app.state.console_oidc_verifier = OIDCVerifier(
            issuer_url=ISSUER, audience="", required_client_id=ADMIN_UI_CLIENT
        )
        app.state.oidc_http = httpx.AsyncClient(transport=httpx.MockTransport(_idp))
        # 키가 없는 진짜 고정 PEM 검증기 — JWKS 경로를 못 타면 여기서 401 이 난다.
        app.state.jwt_verifier = JWTVerifier()
        self.app = app
        self.repo = MagicMock()

    async def ask(
        self, token: str | None, *, row: User | None, settings: MagicMock | None = None, bearer: bool = False
    ) -> httpx.Response:
        self.repo.get_by_sso_subject = AsyncMock(return_value=row)
        session_cm = MagicMock()
        session_cm.__aenter__ = AsyncMock(return_value=MagicMock())
        session_cm.__aexit__ = AsyncMock(return_value=None)
        if not token:
            headers = {}
        elif bearer:
            headers = {"Authorization": f"Bearer {token}"}
        else:
            headers = {"Cookie": f"admin_jwt={token}"}
        with (
            patch("app.core.auth.AsyncSessionLocal", return_value=session_cm),
            patch("app.repositories.user_repository.UserRepository", return_value=self.repo),
            patch("app.core.oidc_identity.get_settings", return_value=settings or _settings()),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app), base_url="http://admin-api.test"
            ) as client:
                return await client.get("/admin/me", headers=headers)


@pytest.fixture
def console() -> _Console:
    return _Console()


# ─────────────────────────────────────────────────────────────────────────────
# 1. 역할 — API 가 판정한 값
# ─────────────────────────────────────────────────────────────────────────────


async def test_admin_group_in_the_token_is_admin(console):
    row = _row(UserRole.DEVELOPER)
    resp = await console.ask(_token(groups=["GatewayAdmin"]), row=row)
    assert resp.status_code == 200
    # user_id 는 IdP sub 가 아니라 DB 행의 id 다.
    assert resp.json() == {"user_id": str(row.id), "email": EMAIL, "role": "ADMIN", "team_id": None}
    console.repo.get_by_sso_subject.assert_awaited_once_with(SUBJECT)


async def test_admin_listed_only_by_email_is_admin(console):
    """콘솔이 지금 틀리는 경우 — 토큰에 관리자 그룹이 없어서 그룹만 보면 DEVELOPER 다."""
    row = _row(UserRole.DEVELOPER)
    resp = await console.ask(_token(), row=row, settings=_settings(admin_emails=[EMAIL]))
    assert resp.status_code == 200
    assert resp.json() == {"user_id": str(row.id), "email": EMAIL, "role": "ADMIN", "team_id": None}


async def test_team_leader_role_and_team_come_from_the_user_row(console):
    row = _row(UserRole.TEAM_LEADER, team_id=TEAM_ID)
    resp = await console.ask(_token(), row=row)
    assert resp.status_code == 200
    assert resp.json() == {"user_id": str(row.id), "email": EMAIL, "role": "TEAM_LEADER", "team_id": str(TEAM_ID)}


async def test_developer_role_comes_from_the_user_row(console):
    row = _row(UserRole.DEVELOPER, team_id=TEAM_ID)
    resp = await console.ask(_token(), row=row)
    assert resp.status_code == 200
    assert resp.json() == {"user_id": str(row.id), "email": EMAIL, "role": "DEVELOPER", "team_id": str(TEAM_ID)}


async def test_the_same_token_gets_whatever_role_the_server_decides(console):
    """대조군 — 토큰 한 장, 서버 상태 두 가지, 역할 두 가지.

    토큰만 보고 역할을 만드는 핸들러(콘솔이 해 온 방식)는 같은 토큰에 같은 답밖에 낼 수 없다.
    """
    token = _token()
    row = _row(UserRole.TEAM_LEADER, team_id=TEAM_ID)
    by_email = await console.ask(token, row=row, settings=_settings(admin_emails=[EMAIL]))
    by_row = await console.ask(token, row=row)
    assert (by_email.status_code, by_row.status_code) == (200, 200)
    assert (by_email.json()["role"], by_row.json()["role"]) == ("ADMIN", "TEAM_LEADER")


# ─────────────────────────────────────────────────────────────────────────────
# 2. 거부 — get_current_user 의 것이 그대로
# ─────────────────────────────────────────────────────────────────────────────


async def test_valid_token_without_a_user_row_is_403_user_not_provisioned(console):
    resp = await console.ask(_token(), row=None)
    assert resp.status_code == 403
    assert resp.json() == _forbidden("user_not_provisioned")


async def test_deactivated_user_is_403_even_with_an_admin_row(console):
    resp = await console.ask(_token(), row=_row(UserRole.ADMIN, is_active=False))
    assert resp.status_code == 403
    assert resp.json() == _forbidden("user_deactivated")


async def test_token_without_the_required_group_is_403(console):
    resp = await console.ask(
        _token(), row=_row(UserRole.ADMIN), settings=_settings(required_group="GatewayUsers")
    )
    assert resp.status_code == 403
    assert resp.json() == _forbidden("required_group_missing")
    console.repo.get_by_sso_subject.assert_not_awaited()


async def test_no_token_is_401(console):
    resp = await console.ask(None, row=_row(UserRole.ADMIN))
    assert resp.status_code == 401
    assert resp.json() == _unauthorized("Missing or invalid Authorization header")


async def test_token_issued_to_another_client_is_401(console):
    """같은 IdP·같은 키로 서명됐어도 admin-ui client 에게 발급된 토큰이 아니면 받지 않는다."""
    resp = await console.ask(_token(aud=CLI_CLIENT), row=_row(UserRole.ADMIN))
    assert resp.status_code == 401
    assert resp.json() == _unauthorized("Invalid or expired token")
    console.repo.get_by_sso_subject.assert_not_awaited()


# ─────────────────────────────────────────────────────────────────────────────
# 3. 캐시와 배선
# ─────────────────────────────────────────────────────────────────────────────


async def test_the_answer_is_not_cached(console):
    """역할은 요청 사이에 바뀐다 — 브라우저나 중간 캐시가 옛 답을 다시 쓰면 안 된다."""
    resp = await console.ask(_token(), row=_row(UserRole.DEVELOPER))
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"


async def test_bearer_header_and_cookie_get_the_same_answer(console):
    """콘솔은 토큰을 ``admin_jwt`` 쿠키로도 ``Authorization: Bearer`` 로도 보낸다 — 답이 같아야 한다."""
    token, row = _token(), _row(UserRole.TEAM_LEADER, team_id=TEAM_ID)
    by_cookie = await console.ask(token, row=row)
    by_bearer = await console.ask(token, row=row, bearer=True)
    assert (by_cookie.status_code, by_bearer.status_code) == (200, 200)
    expected = {"user_id": str(row.id), "email": EMAIL, "role": "TEAM_LEADER", "team_id": str(TEAM_ID)}
    assert by_cookie.json() == by_bearer.json() == expected


def test_the_real_app_serves_it_behind_get_current_user_alone():
    """역할 검사도 DB 세션도 붙지 않는다 — 어느 역할이든 자기 신원은 물을 수 있어야 한다."""
    from app.main import app

    routes = [r for r in app.routes if isinstance(r, APIRoute) and r.path == "/admin/me"]
    assert [r.methods for r in routes] == [{"GET"}]
    assert [d.call for d in routes[0].dependant.dependencies] == [get_current_user]
