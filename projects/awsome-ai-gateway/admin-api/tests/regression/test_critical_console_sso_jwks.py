# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""admin-ui SSO 세션 토큰을 IdP 의 JWKS 로 검증한다(``core/auth._verify_console_sso_token``).

배경 — admin-ui 는 OIDC 콜백에서 받은 IdP 토큰을 그대로 ``admin_jwt`` 쿠키에 굽는다. 그 토큰을
admin-api 가 받으려면 운영자가 IdP 서명키를 PEM 으로 바꿔 ``auth.admin_jwt_configs`` 에 손으로
넣어야 했다. 문서도 도구도 없는 작업이고, IdP 가 키를 바꾸는 순간 관리자 로그인이 전부 끊긴다.
그래서 정식 로그인 경로가 사실상 없었고, 그 자리를 dev 로그인이 메웠다.

``OIDC_ADMIN_UI_CLIENT_ID`` 를 채우면 issuer 의 JWKS 로 검증하고 **그 client 에게 발급된
토큰만** 받는다. 여기서는 진짜 RSA 키로 서명한 토큰과 진짜 ``OIDCVerifier`` 를 쓴다 — IdP 만
``httpx.MockTransport`` 로 대신한다. 검증기를 mock 하면 이 파일이 고정하려는 것(서명·발급 대상·
알고리즘·만료)을 하나도 보지 못한다.
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from jose import jwt

from app.core import auth
from app.core.auth import CurrentUser, JWTVerifier, get_current_user
from app.core.oidc_verifier import OIDCVerifier
from app.models.auth import UserRole

ISSUER = "https://idp.example.com/pool-1"
ADMIN_UI_CLIENT = "admin-ui-client-id"
CLI_CLIENT = "cli-client-id"
KID = "idp-key-1"


def _keypair() -> tuple[str, str, rsa.RSAPublicNumbers]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private_pem, public_pem, key.public_key().public_numbers()


def _b64u(number: int) -> str:
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


IDP_PRIVATE, IDP_PUBLIC, _IDP_NUMBERS = _keypair()
ATTACKER_PRIVATE, _, _ = _keypair()
INTERNAL_PRIVATE, INTERNAL_PUBLIC, _ = _keypair()
IDP_JWK = {
    "kty": "RSA",
    "kid": KID,
    "use": "sig",
    "alg": "RS256",
    "n": _b64u(_IDP_NUMBERS.n),
    "e": _b64u(_IDP_NUMBERS.e),
}


class FakeIdP:
    """discovery + JWKS 만 서빙하는 IdP. 호출 수를 세고, 끌 수 있다."""

    def __init__(self) -> None:
        self.jwks_calls = 0
        self.discovery_calls = 0
        self.down = False
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("idp is down", request=request)
        if request.url.path.endswith("/.well-known/openid-configuration"):
            self.discovery_calls += 1
            return httpx.Response(200, json={"issuer": ISSUER, "jwks_uri": f"{ISSUER}/.well-known/jwks.json"})
        if request.url.path.endswith("/.well-known/jwks.json"):
            self.jwks_calls += 1
            return httpx.Response(200, json={"keys": [IDP_JWK]})
        return httpx.Response(404)


def _claims(**overrides) -> dict:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "sub": "idp-subject-1",
        "aud": ADMIN_UI_CLIENT,
        "email": "admin@example.com",
        "groups": ["GatewayAdmin"],
        "iat": now,
        "exp": now + 3600,
        "token_use": "id",
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


def _token(claims: dict, *, key: str = IDP_PRIVATE, kid: str | None = KID, alg: str = "RS256") -> str:
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": kid} if kid else None)


def _console_verifier() -> OIDCVerifier:
    return OIDCVerifier(issuer_url=ISSUER, audience="", required_client_id=ADMIN_UI_CLIENT)


def _request(token: str, idp: FakeIdP, *, console: OIDCVerifier | None, static: JWTVerifier | None = None) -> MagicMock:
    request = MagicMock()
    request.headers = {"Cookie": f"admin_jwt={token}"}
    request.app.state.console_oidc_verifier = console
    request.app.state.oidc_http = idp.client
    # 키가 없는 **진짜** 고정 PEM 검증기 — JWKS 경로를 못 타면 여기서 401 이 난다.
    request.app.state.jwt_verifier = static or JWTVerifier()
    return request


_RESOLVED = CurrentUser(user_id=uuid.uuid4(), email="admin@example.com", role=UserRole.ADMIN, team_id=None)


@pytest.fixture
def idp() -> FakeIdP:
    return FakeIdP()


@pytest.fixture
def resolve():
    """``_resolve_idp_identity`` 는 별도 파일이 고정한다. 여기서는 **불렸는가** 만 본다."""
    with patch.object(auth, "_resolve_idp_identity", AsyncMock(return_value=_RESOLVED)) as m:
        yield m


async def _rejected(request: MagicMock) -> int:
    with pytest.raises(HTTPException) as exc:
        await get_current_user(request)
    return exc.value.status_code


# ─────────────────────────────────────────────────────────────────────────────
# 1. 받아야 하는 토큰
# ─────────────────────────────────────────────────────────────────────────────


async def test_id_token_issued_to_the_admin_ui_client_is_accepted(idp, resolve):
    user = await get_current_user(_request(_token(_claims()), idp, console=_console_verifier()))
    assert user is _RESOLVED
    verified = resolve.await_args.args[0]
    assert verified["sub"] == "idp-subject-1"
    assert verified["groups"] == ["GatewayAdmin"]


async def test_cognito_access_token_is_matched_on_client_id(idp, resolve):
    """Cognito access_token 에는 ``aud`` 가 없고 ``client_id`` 가 있다."""
    token = _token(_claims(aud=None, client_id=ADMIN_UI_CLIENT, token_use="access"))
    assert await get_current_user(_request(token, idp, console=_console_verifier())) is _RESOLVED


async def test_audience_list_containing_the_client_is_accepted(idp, resolve):
    token = _token(_claims(aud=[ADMIN_UI_CLIENT, "another-api"]))
    assert await get_current_user(_request(token, idp, console=_console_verifier())) is _RESOLVED


async def test_verified_claims_reach_identity_resolution_end_to_end(idp):
    """mock 없이 끝까지 — 그룹이 ADMIN 으로, ``sub`` 가 DB 사용자로 이어진다."""
    db_user = MagicMock(
        id=uuid.uuid4(), email="admin@example.com", team_id=None, role=UserRole.DEVELOPER, is_active=True
    )
    repo = MagicMock()
    repo.get_by_sso_subject = AsyncMock(return_value=db_user)
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=MagicMock())
    session_cm.__aexit__ = AsyncMock(return_value=None)
    settings = MagicMock(
        ADMIN_GROUPS=["GatewayAdmin"],
        ADMIN_EMAILS=[],
        OIDC_REQUIRED_GROUP="",
        OIDC_GROUPS_CLAIM="groups",
        OIDC_USER_ID_CLAIM="sub",
        OIDC_EMAIL_CLAIM="email",
        OIDC_NAME_CLAIM="name",
    )
    with (
        patch("app.core.auth.AsyncSessionLocal", return_value=session_cm),
        patch("app.repositories.user_repository.UserRepository", return_value=repo),
        patch("app.core.oidc_identity.get_settings", return_value=settings),
    ):
        user = await get_current_user(_request(_token(_claims()), idp, console=_console_verifier()))
    repo.get_by_sso_subject.assert_awaited_once_with("idp-subject-1")
    assert user.user_id == db_user.id
    assert user.role == UserRole.ADMIN


# ─────────────────────────────────────────────────────────────────────────────
# 2. 거부해야 하는 토큰 — 전부 401, 신원 해석은 불리지 않는다
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "claims",
    [
        pytest.param(_claims(aud=CLI_CLIENT), id="id_token issued to another client"),
        pytest.param(_claims(aud=None, client_id=CLI_CLIENT), id="access_token issued to another client"),
        pytest.param(_claims(aud=None), id="neither aud nor client_id"),
        # aud 가 있으면 aud 가 판정한다 — client_id 가 맞아도 넘어가지 않는다.
        pytest.param(_claims(aud=CLI_CLIENT, client_id=ADMIN_UI_CLIENT), id="aud wrong, client_id right"),
        pytest.param(_claims(exp=int(time.time()) - 60), id="expired"),
        pytest.param(_claims(sub=None), id="no subject"),
    ],
)
async def test_tokens_with_bad_claims_are_rejected(idp, resolve, claims):
    assert await _rejected(_request(_token(claims), idp, console=_console_verifier())) == 401
    resolve.assert_not_awaited()


async def test_token_signed_by_another_key_under_the_idp_kid_is_rejected(idp, resolve):
    token = _token(_claims(), key=ATTACKER_PRIVATE, kid=KID)
    assert await _rejected(_request(token, idp, console=_console_verifier())) == 401
    resolve.assert_not_awaited()


async def test_hs256_token_is_rejected(idp, resolve):
    """알고리즘 혼동 — 공개키를 HMAC 비밀로 쓰는 고전적 공격의 형상."""
    token = _token(_claims(), key="not-a-private-key", alg="HS256")
    assert await _rejected(_request(token, idp, console=_console_verifier())) == 401
    resolve.assert_not_awaited()


async def test_unsigned_token_is_rejected(idp, resolve):
    def seg(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    token = f"{seg({'alg': 'none', 'kid': KID})}.{seg(_claims())}."
    assert await _rejected(_request(token, idp, console=_console_verifier())) == 401
    resolve.assert_not_awaited()


async def test_made_up_key_ids_cannot_make_the_idp_be_fetched_per_request(idp, resolve):
    """검증 전에는 누구나 kid 를 지어낼 수 있다 — 요청마다 IdP 를 치게 두지 않는다."""
    verifier = _console_verifier()
    for i in range(5):
        token = _token(_claims(), key=ATTACKER_PRIVATE, kid=f"made-up-{i}")
        assert await _rejected(_request(token, idp, console=verifier)) == 401
    assert idp.jwks_calls == 1
    resolve.assert_not_awaited()


# ─────────────────────────────────────────────────────────────────────────────
# 3. 경로 분기 — 이 경로가 건드리면 안 되는 것
# ─────────────────────────────────────────────────────────────────────────────


def _internal_verifier(*, issuer: str, audience: str, pem: str) -> JWTVerifier:
    verifier = JWTVerifier()
    verifier.load_configs(
        [{"id": uuid.uuid4(), "public_key_pem": pem, "issuer": issuer, "audience": audience, "algorithm": "RS256"}]
    )
    return verifier


async def test_internal_admin_jwt_never_touches_the_idp(idp, resolve):
    """다른 issuer 의 토큰(내부 admin JWT)은 예전 그대로 고정 PEM 경로를 탄다."""
    user_id = uuid.uuid4()
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": "internal-admin",
            "aud": "internal",
            "sub": str(user_id),
            "role": "ADMIN",
            "email": "a@example.com",
            "exp": now + 600,
        },
        INTERNAL_PRIVATE,
        algorithm="RS256",
    )
    static = _internal_verifier(issuer="internal-admin", audience="internal", pem=INTERNAL_PUBLIC)
    user = await get_current_user(_request(token, idp, console=_console_verifier(), static=static))
    assert (user.user_id, user.role) == (user_id, UserRole.ADMIN)
    assert (idp.discovery_calls, idp.jwks_calls) == (0, 0)
    resolve.assert_not_awaited()


async def test_setting_off_means_idp_tokens_are_not_verified_through_jwks(idp, resolve):
    """기본값 — ``OIDC_ADMIN_UI_CLIENT_ID`` 가 비면 이 경로는 없다(예전 동작)."""
    assert await _rejected(_request(_token(_claims()), idp, console=None)) == 401
    assert (idp.discovery_calls, idp.jwks_calls) == (0, 0)
    resolve.assert_not_awaited()


async def test_garbage_token_is_a_401_not_a_crash(idp, resolve):
    assert await _rejected(_request("x.y.z", idp, console=_console_verifier())) == 401
    assert (idp.discovery_calls, idp.jwks_calls) == (0, 0)


async def test_a_jwks_rejection_is_final(idp, resolve):
    """JWKS 경로가 거부한 토큰을 고정 PEM 경로가 다시 받아 주지 않는다.

    고정 PEM 행이 같은 issuer·같은 키·**CLI client** audience 로 등록돼 있어도, admin-ui client
    에게 발급되지 않은 토큰은 401 이다. 대조군: 같은 토큰이 JWKS 경로가 꺼져 있으면 통과한다 —
    이 대조군이 없으면 "고정 PEM 행이 어차피 못 받아서 401" 인지 구분할 수 없다.
    """
    token = _token(_claims(aud=CLI_CLIENT, role="ADMIN", sub=str(uuid.uuid4())))
    static = _internal_verifier(issuer=ISSUER, audience=CLI_CLIENT, pem=IDP_PUBLIC)

    control = await get_current_user(_request(token, idp, console=None, static=static))
    assert control.role == UserRole.ADMIN

    assert await _rejected(_request(token, idp, console=_console_verifier(), static=static)) == 401


# ─────────────────────────────────────────────────────────────────────────────
# 4. IdP 가 안 닿을 때
# ─────────────────────────────────────────────────────────────────────────────


async def test_idp_unreachable_with_no_cached_keys_is_503(idp, resolve):
    """판정할 수 없다는 뜻이다. 401 이면 admin-ui 가 쿠키를 지우고 죽은 IdP 로 되돌린다."""
    idp.down = True
    assert await _rejected(_request(_token(_claims()), idp, console=_console_verifier())) == 503
    resolve.assert_not_awaited()


async def test_cached_keys_keep_sessions_alive_while_the_idp_is_down(idp, resolve):
    verifier = _console_verifier()
    token = _token(_claims())
    assert await get_current_user(_request(token, idp, console=verifier)) is _RESOLVED
    assert idp.jwks_calls == 1

    # TTL 이 지났고 IdP 는 죽었다.
    verifier._jwks_fetched_at = time.monotonic() - verifier._jwks_ttl - 1
    idp.down = True
    for _ in range(3):
        assert await get_current_user(_request(token, idp, console=verifier)) is _RESOLVED

    # 살아나면 다음 재시도에서 새 키를 받는다.
    idp.down = False
    verifier._jwks_fetched_at = time.monotonic() - verifier._jwks_ttl - 1
    assert await get_current_user(_request(token, idp, console=verifier)) is _RESOLVED
    assert idp.jwks_calls == 2
