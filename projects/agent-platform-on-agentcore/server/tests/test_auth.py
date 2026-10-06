"""
Tests for bearer-token verification: Cognito access tokens and OIDC id_tokens.

The registry's write paths had no auth dependency at all, so these cover the
decisions that keep that from happening again: what gets rejected, that an
unconfigured IdP fails closed, and that the local opt-out is explicit. The
second half pins the OIDC path added for Microsoft Entra ID: the token is
routed by `iss`, the provider's groups-claim and admin rules set the role, and
a Cognito-only deployment refuses a foreign issuer.

Verification itself is stubbed (Cognito: `_decode_token`; OIDC: `verify_any`) —
the crypto is PyJWT's and the OIDC verifier has its own test file. Tokens are
still real JWT shapes because `current_user` reads `iss` before routing.
"""
import os
import sys
import time

import jwt
import pytest
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.auth as auth  # noqa: E402
import core.oidc_verifier as ov  # noqa: E402
from core import config  # noqa: E402
from core.config import OIDCProvider  # noqa: E402

POOL = "us-east-1_pool"
COGNITO_ISS = f"https://cognito-idp.us-east-1.amazonaws.com/{POOL}"

ENTRA = OIDCProvider(
    id="entra",
    label="Microsoft 계정 (Entra ID)",
    issuer_url="https://login.microsoftonline.com/tenant/v2.0",
    audience="client-abc",
    client_id="client-abc",
    groups_claim="groups",
    admin_groups=("PlatformAdmin",),
)


def _app():
    app = FastAPI()

    @app.get("/who")
    def who(user: auth.AuthUser = Depends(auth.current_user)):
        return {"username": user.username, "groups": user.groups}

    @app.get("/sub")
    def subject(user: auth.AuthUser = Depends(auth.current_user)):
        return {"sub": user.sub, "provider": user.provider, "role": user.role, "email": user.email}

    @app.post("/admin-only")
    def admin_only(user: auth.AuthUser = Depends(auth.require_admin)):
        return {"ok": True, "username": user.username}

    return TestClient(app, raise_server_exceptions=False)


def _unsigned(claims: dict) -> str:
    """A JWT whose header and payload parse but whose signature is nothing —
    enough for `iss` routing; the stubbed verifiers never look at it."""
    return jwt.encode(claims, key=None, algorithm="none")


def _cognito_token(**claims) -> str:
    return _unsigned({"iss": COGNITO_ISS, **claims})


def _entra_token(**claims) -> str:
    return _unsigned({"iss": ENTRA.issuer_url, **claims})


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    """Each test picks its own configuration; never inherit the process env."""
    monkeypatch.setattr(config, "COGNITO_USER_POOL_ID", POOL, raising=False)
    monkeypatch.setattr(config, "COGNITO_REGION", "us-east-1", raising=False)
    monkeypatch.setattr(config, "AUTH_ENFORCED", True, raising=False)
    monkeypatch.setattr(config, "OIDC_PROVIDERS", [], raising=False)
    auth.reset_verifier_cache()
    yield
    auth.reset_verifier_cache()


def _stub_cognito(monkeypatch, claims):
    """Replace Cognito JWT decoding so tests exercise our checks, not PyJWT's crypto."""
    def fake_decode(token: str):
        if jwt.decode(token, options={"verify_signature": False}).get("bad") == "signature":
            raise auth.TokenInvalid("signature verification failed")
        return claims

    monkeypatch.setattr(auth, "_decode_token", fake_decode)


def _stub_oidc(monkeypatch, claims=None, error=None, provider=ENTRA):
    def fake(token, providers):
        if error is not None:
            raise error
        return claims, provider

    monkeypatch.setattr(ov, "verify_any", fake)


# ── Cognito ──────────────────────────────────────────────────────────────────

def test_missing_header_is_401():
    assert _app().get("/who").status_code == 401


def test_malformed_header_is_401():
    res = _app().get("/who", headers={"Authorization": "Basic abc"})
    assert res.status_code == 401


def test_a_bearer_that_is_not_a_jwt_is_401():
    res = _app().get("/who", headers={"Authorization": "Bearer not-a-token"})
    assert res.status_code == 401


def test_valid_access_token_resolves_the_user(monkeypatch):
    _stub_cognito(monkeypatch, {
        "token_use": "access",
        "username": "alice",
        "cognito:groups": ["admin"],
        "exp": int(time.time()) + 3600,
    })
    res = _app().get("/who", headers={"Authorization": f"Bearer {_cognito_token()}"})
    assert res.status_code == 200
    assert res.json() == {"username": "alice", "groups": ["admin"]}


def test_an_id_token_is_rejected(monkeypatch):
    # id_token proves identity; access_token is the API authorisation token.
    _stub_cognito(monkeypatch, {
        "token_use": "id",
        "username": "alice",
        "exp": int(time.time()) + 3600,
    })
    res = _app().get("/who", headers={"Authorization": f"Bearer {_cognito_token()}"})
    assert res.status_code == 401


def test_bad_signature_is_401(monkeypatch):
    _stub_cognito(monkeypatch, {"token_use": "access", "username": "alice"})
    res = _app().get("/who", headers={"Authorization": f"Bearer {_cognito_token(bad='signature')}"})
    assert res.status_code == 401


def test_admin_group_passes_require_admin(monkeypatch):
    _stub_cognito(monkeypatch, {
        "token_use": "access",
        "username": "alice",
        "cognito:groups": ["admin"],
    })
    res = _app().post("/admin-only", headers={"Authorization": f"Bearer {_cognito_token()}"})
    assert res.status_code == 200


def test_non_admin_is_403_not_401(monkeypatch):
    # The caller is authenticated; they just lack the role.
    _stub_cognito(monkeypatch, {
        "token_use": "access",
        "username": "bob",
        "cognito:groups": ["user"],
    })
    res = _app().post("/admin-only", headers={"Authorization": f"Bearer {_cognito_token()}"})
    assert res.status_code == 403


def test_no_groups_claim_is_403(monkeypatch):
    _stub_cognito(monkeypatch, {"token_use": "access", "username": "bob"})
    res = _app().post("/admin-only", headers={"Authorization": f"Bearer {_cognito_token()}"})
    assert res.status_code == 403


def test_unconfigured_idp_fails_closed(monkeypatch):
    monkeypatch.setattr(config, "COGNITO_USER_POOL_ID", "", raising=False)
    res = _app().get("/who", headers={"Authorization": f"Bearer {_cognito_token()}"})
    assert res.status_code == 503


def test_auth_enforced_false_bypasses_verification(monkeypatch):
    monkeypatch.setattr(config, "COGNITO_USER_POOL_ID", "", raising=False)
    monkeypatch.setattr(config, "AUTH_ENFORCED", False, raising=False)
    client = _app()
    # Local development without any IdP: everyone is a local admin.
    assert client.get("/who").status_code == 200
    assert client.post("/admin-only").status_code == 200


def test_the_subject_is_carried_because_it_owns_per_user_resources(monkeypatch):
    """
    Knowledge bases are keyed by `sub`, not `username`.

    Cognito can reassign a username after the account is deleted, which would
    silently transfer the previous holder's knowledge bases to whoever takes the
    name next. `sub` is never reused.
    """
    _stub_cognito(monkeypatch, {
        "sub": "11111111-2222-3333-4444-555555555555",
        "username": "alice",
        "token_use": "access",
        "exp": time.time() + 3600,
    })
    response = _app().get("/sub", headers={"Authorization": f"Bearer {_cognito_token()}"})
    assert response.status_code == 200
    assert response.json()["sub"] == "11111111-2222-3333-4444-555555555555"
    assert response.json()["provider"] == "cognito"


def test_the_local_opt_out_user_has_a_stable_subject(monkeypatch):
    """Otherwise every locally created knowledge base is owned by "" and shared."""
    monkeypatch.setattr(config, "AUTH_ENFORCED", False, raising=False)
    assert _app().get("/sub").json()["sub"] == "local"


def test_a_foreign_issuer_is_401_when_only_cognito_is_configured(monkeypatch):
    """No OIDC provider configured: an Entra-shaped token must not reach the
    OIDC path (which would 503 on "no providers") — it is simply not ours."""
    _stub_oidc(monkeypatch, {"sub": "u1"})
    res = _app().get("/who", headers={"Authorization": f"Bearer {_entra_token(sub='u1')}"})
    assert res.status_code == 401


# ── OIDC (Entra ID) ──────────────────────────────────────────────────────────

@pytest.fixture
def entra(monkeypatch):
    monkeypatch.setattr(config, "OIDC_PROVIDERS", [ENTRA], raising=False)


def test_entra_id_token_resolves_the_user(entra, monkeypatch):
    _stub_oidc(monkeypatch, {
        "sub": "e-1", "preferred_username": "alice@corp.com", "email": "alice@corp.com",
        "name": "Alice", "groups": ["PlatformAdmin"], "exp": int(time.time()) + 3600,
    })
    res = _app().get("/sub", headers={"Authorization": f"Bearer {_entra_token()}"})
    assert res.status_code == 200
    assert res.json() == {"sub": "e-1", "provider": "entra", "role": "admin", "email": "alice@corp.com"}


def test_entra_admin_group_is_the_providers_not_cognitos(entra, monkeypatch):
    """`admin` is Cognito's group name. For Entra the deployment names the
    group (admin_groups); a user in a group called `admin` there is not one."""
    _stub_oidc(monkeypatch, {"sub": "e-1", "email": "a@corp.com", "groups": ["admin"]})
    assert _app().post("/admin-only", headers={"Authorization": f"Bearer {_entra_token()}"}).status_code == 403

    _stub_oidc(monkeypatch, {"sub": "e-1", "email": "a@corp.com", "groups": ["PlatformAdmin"]})
    assert _app().post("/admin-only", headers={"Authorization": f"Bearer {_entra_token()}"}).status_code == 200


def test_entra_admin_email_grants_admin(monkeypatch):
    provider = OIDCProvider(
        id="entra", label="Entra", issuer_url=ENTRA.issuer_url, audience="client-abc",
        admin_emails=("boss@corp.com",),
    )
    monkeypatch.setattr(config, "OIDC_PROVIDERS", [provider], raising=False)
    _stub_oidc(monkeypatch, {"sub": "e-9", "email": "Boss@Corp.com", "groups": []}, provider=provider)
    assert _app().post("/admin-only", headers={"Authorization": f"Bearer {_entra_token()}"}).status_code == 200


def test_entra_required_group_gate_is_403(monkeypatch):
    gated = OIDCProvider(
        id="entra", label="Entra", issuer_url=ENTRA.issuer_url, audience="client-abc",
        required_group="PlatformUsers", admin_groups=("PlatformAdmin",),
    )
    monkeypatch.setattr(config, "OIDC_PROVIDERS", [gated], raising=False)
    _stub_oidc(monkeypatch, {"sub": "e-1", "email": "a@corp.com", "groups": ["Other"]}, provider=gated)
    assert _app().get("/who", headers={"Authorization": f"Bearer {_entra_token()}"}).status_code == 403


def test_entra_invalid_token_is_401(entra, monkeypatch):
    _stub_oidc(monkeypatch, error=ov.OIDCVerifyError("bad sig"))
    assert _app().get("/who", headers={"Authorization": f"Bearer {_entra_token()}"}).status_code == 401


def test_entra_discovery_outage_is_503(entra, monkeypatch):
    _stub_oidc(monkeypatch, error=ov.OIDCConfigError("jwks down"))
    assert _app().get("/who", headers={"Authorization": f"Bearer {_entra_token()}"}).status_code == 503


def test_both_idps_coexist_and_route_by_issuer(entra, monkeypatch):
    """One deployment, two login options: a Cognito access token and an Entra
    id_token are both accepted, each by its own verifier."""
    _stub_cognito(monkeypatch, {"token_use": "access", "username": "c-user", "cognito:groups": []})
    _stub_oidc(monkeypatch, {"sub": "e-1", "email": "a@corp.com", "groups": []})
    client = _app()
    assert client.get("/sub", headers={"Authorization": f"Bearer {_cognito_token()}"}).json()["provider"] == "cognito"
    assert client.get("/sub", headers={"Authorization": f"Bearer {_entra_token()}"}).json()["provider"] == "entra"


def test_oidc_only_deployment_needs_no_pool(monkeypatch):
    """Cognito unset, Entra set: not a 503 — the deployment chose one IdP."""
    monkeypatch.setattr(config, "COGNITO_USER_POOL_ID", "", raising=False)
    monkeypatch.setattr(config, "OIDC_PROVIDERS", [ENTRA], raising=False)
    _stub_oidc(monkeypatch, {"sub": "e-1", "email": "a@corp.com", "groups": []})
    assert _app().get("/who", headers={"Authorization": f"Bearer {_entra_token()}"}).status_code == 200


def test_auth_user_built_from_groups_alone_keeps_the_cognito_rule():
    """Tests and fixtures elsewhere construct AuthUser(groups=["admin"]) with no
    role; that must still read as admin."""
    assert auth.AuthUser(username="x", groups=["admin"]).is_admin
    assert not auth.AuthUser(username="x", groups=["user"]).is_admin
    assert auth.AuthUser(username="x", groups=[], role="admin").is_admin
    assert not auth.AuthUser(username="x", groups=["admin"], role="user").is_admin
