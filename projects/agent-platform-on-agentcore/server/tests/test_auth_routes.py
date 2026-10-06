"""/api/auth/config and /api/auth/session — what the login screen is told, and
what an OIDC login leaves behind.

The password routes (/login, /refresh) talk to Cognito directly and are not
exercised here.
"""
import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.auth as auth  # noqa: E402
import routes.auth as auth_routes  # noqa: E402
from core import config  # noqa: E402
from core.config import OIDCProvider  # noqa: E402

CALLBACKS = ("https://agents.example.com/auth/callback", "http://localhost:3000/auth/callback")

ENTRA = OIDCProvider(
    id="entra", label="Microsoft 계정 (Entra ID)",
    issuer_url="https://login.microsoftonline.com/tenant/v2.0",
    audience="client-abc", client_id="client-abc", redirect_uris=CALLBACKS,
    scopes="openid profile email offline_access",
    groups_claim="groups", admin_groups=("PlatformAdmin",), admin_emails=("boss@corp.com",),
)


class StubRepo:
    def __init__(self):
        self.calls = []

    def upsert_on_login(self, **kw):
        self.calls.append(kw)


@pytest.fixture
def deployment(monkeypatch):
    """Both IdPs configured unless a test says otherwise."""
    monkeypatch.setattr(config, "COGNITO_USER_POOL_ID", "us-east-1_pool", raising=False)
    monkeypatch.setattr(config, "COGNITO_USER_POOL_CLIENT_ID", "cog-client", raising=False)
    monkeypatch.setattr(config, "OIDC_PROVIDERS", [ENTRA], raising=False)


def _client(monkeypatch, user=None, repo=None):
    monkeypatch.setattr(auth_routes, "_user_repo", lambda: repo)
    app = FastAPI()
    app.include_router(auth_routes.router)
    if user is not None:
        app.dependency_overrides[auth.current_user] = lambda: user
    return TestClient(app, raise_server_exceptions=False)


# ── /config ──────────────────────────────────────────────────────────────────

def test_config_lists_password_form_then_oidc_buttons(deployment, monkeypatch):
    res = _client(monkeypatch).get("/api/auth/config")
    assert res.status_code == 200
    providers = res.json()["providers"]
    assert [(p["id"], p["kind"]) for p in providers] == [("cognito", "password"), ("entra", "oidc")]
    entra = providers[1]
    # No Origin header: the first registered callback is the representative one.
    assert entra == {
        "id": "entra",
        "kind": "oidc",
        "label": "Microsoft 계정 (Entra ID)",
        "issuer_url": "https://login.microsoftonline.com/tenant/v2.0",
        "client_id": "client-abc",
        "redirect_uri": "https://agents.example.com/auth/callback",
        "scopes": "openid profile email offline_access",
    }
    # Server-side verification fields never leave.
    assert "audience" not in entra
    assert "admin_groups" not in entra
    assert "admin_emails" not in entra


def test_config_picks_redirect_uri_by_origin(deployment, monkeypatch):
    """A local dev server proxying to the same backend must get its own
    registered callback, not production's — and an unknown origin falls back
    to the representative one so the IdP, not the server, rejects it."""
    client = _client(monkeypatch)
    pick = lambda origin: client.get("/api/auth/config", headers={"origin": origin}).json()["providers"][1]["redirect_uri"]  # noqa: E731
    assert pick("http://localhost:3000") == "http://localhost:3000/auth/callback"
    assert pick("https://agents.example.com") == "https://agents.example.com/auth/callback"
    assert pick("https://evil.example") == "https://agents.example.com/auth/callback"


def test_config_without_cognito_client_has_no_password_form(deployment, monkeypatch):
    """A pool id alone verifies tokens; without the app client the password
    form has nothing to call, so the screen must not offer it."""
    monkeypatch.setattr(config, "COGNITO_USER_POOL_CLIENT_ID", "", raising=False)
    providers = _client(monkeypatch).get("/api/auth/config").json()["providers"]
    assert [p["id"] for p in providers] == ["entra"]


def test_config_cognito_only(deployment, monkeypatch):
    monkeypatch.setattr(config, "OIDC_PROVIDERS", [], raising=False)
    providers = _client(monkeypatch).get("/api/auth/config").json()["providers"]
    assert providers == [{"id": "cognito", "kind": "password", "label": "아이디 · 비밀번호"}]


# ── /session ─────────────────────────────────────────────────────────────────

def test_session_records_oidc_user_and_returns_profile(deployment, monkeypatch):
    repo = StubRepo()
    user = auth.AuthUser(sub="e-1", username="alice@corp.com", email="alice@corp.com",
                         name="Alice", groups=["PlatformAdmin"], role="admin", provider="entra")
    res = _client(monkeypatch, user=user, repo=repo).post("/api/auth/session")
    assert res.status_code == 200
    assert res.json() == {
        "sub": "e-1", "username": "alice@corp.com", "email": "alice@corp.com", "name": "Alice",
        "role": "admin", "groups": ["PlatformAdmin"], "provider": "entra",
    }
    assert repo.calls == [{
        "sub": "e-1", "provider": "entra", "email": "alice@corp.com", "name": "Alice",
        "groups": ["PlatformAdmin"], "role": "admin",
    }]


def test_session_does_not_record_cognito_users(deployment, monkeypatch):
    """The pool is their directory already (ListUsers)."""
    repo = StubRepo()
    user = auth.AuthUser(sub="c-1", username="c-1", groups=["admin"], provider="cognito")
    res = _client(monkeypatch, user=user, repo=repo).post("/api/auth/session")
    assert res.status_code == 200
    assert res.json()["role"] == "admin"
    assert repo.calls == []


def test_session_without_users_table_still_succeeds(deployment, monkeypatch):
    user = auth.AuthUser(sub="e-1", username="a@corp.com", email="a@corp.com", groups=[], role="user", provider="entra")
    res = _client(monkeypatch, user=user, repo=None).post("/api/auth/session")
    assert res.status_code == 200
    assert res.json()["sub"] == "e-1"


def test_session_survives_a_failing_users_table(deployment, monkeypatch):
    class Broken:
        def upsert_on_login(self, **kw):
            raise RuntimeError("ProvisionedThroughputExceeded")

    user = auth.AuthUser(sub="e-1", username="a@corp.com", email="a@corp.com", groups=[], role="user", provider="entra")
    res = _client(monkeypatch, user=user, repo=Broken()).post("/api/auth/session")
    assert res.status_code == 200


def test_session_requires_a_verified_caller(deployment, monkeypatch):
    assert _client(monkeypatch).post("/api/auth/session").status_code == 401
