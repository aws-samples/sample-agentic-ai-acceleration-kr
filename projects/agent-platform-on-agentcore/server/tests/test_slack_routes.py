"""Admin API for the Slack integration (routes/slack.py), and Harness deletion's hook.

Every route is admin-only, tokens never come back in any response — including
the error a malformed body would get — and each service failure maps to the
status the web dialog branches on.
"""
import importlib
import os
import sys
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.auth as auth  # noqa: E402
import routes.slack as slack_routes  # noqa: E402
from integrations.slack.repository import IntegrationExists  # noqa: E402
from integrations.slack.service import (  # noqa: E402
    DeletionIncomplete,
    IntegrationNotFound,
    SlackNotConfigured,
    ValidationFailed,
)

ADMIN = auth.AuthUser(sub="u-admin", username="alice", groups=["admin"], role="admin")
USER = auth.AuthUser(sub="u-plain", username="bob", groups=[], role="user")
BOT = "xoxb-1-super-secret"
APP = "xapp-1-super-secret"


class Service:
    def __init__(self):
        self.raise_ = None
        self.calls = []

    def _maybe(self):
        if self.raise_ is not None:
            raise self.raise_

    async def list(self):
        self._maybe()
        return [{"harness_id": "h1", "status": "active", "connection_state": "connected"}]

    async def get(self, harness_id):
        self._maybe()
        return None if harness_id == "none" else {"harness_id": harness_id}

    async def manifest(self, harness_id):
        self._maybe()
        return {"yaml": "x", "json": "{}"}

    async def test(self, harness_id, bot, app):
        self.calls.append(("test", harness_id, bot, app))
        self._maybe()
        return {"ok": True, "checks": []}

    async def create(self, harness_id, bot, app, user):
        self.calls.append(("create", harness_id, bot, app, user.sub))
        self._maybe()
        return {"harness_id": harness_id}

    async def delete(self, harness_id, user):
        self._maybe()
        return {"deleted": True, "steps": []}


@pytest.fixture
def service(monkeypatch):
    svc = Service()
    monkeypatch.setattr(slack_routes, "_service", lambda: svc)
    return svc


def client_as(user):
    app = FastAPI()
    app.include_router(slack_routes.router)
    app.dependency_overrides[auth.current_user] = lambda: user
    return TestClient(app)


ROUTES = [
    ("GET", "/api/slack/integrations", None),
    ("GET", "/api/harnesses/h1/slack", None),
    ("GET", "/api/harnesses/h1/slack/manifest", None),
    ("POST", "/api/harnesses/h1/slack/test", {"bot_token": BOT, "app_token": APP}),
    ("POST", "/api/harnesses/h1/slack", {"bot_token": BOT, "app_token": APP}),
    ("DELETE", "/api/harnesses/h1/slack", None),
]


@pytest.mark.parametrize("method,path,body", ROUTES)
def test_every_route_is_admin_only(service, method, path, body):
    assert client_as(USER).request(method, path, json=body).status_code == 403
    assert client_as(ADMIN).request(method, path, json=body).status_code == 200


def test_tokens_reach_the_service_and_the_caller_is_recorded(service):
    client_as(ADMIN).post("/api/harnesses/h1/slack", json={"bot_token": f" {BOT} ", "app_token": APP})
    assert service.calls == [("create", "h1", BOT, APP, "u-admin")]


@pytest.mark.parametrize(
    "body",
    [b"not json", b"[]", b'{"bot_token": 1, "app_token": "xapp-1-super-secret"}', b'{"bot_token": "xoxb-1-super-secret"}'],
)
def test_a_malformed_body_is_a_bare_400_that_echoes_nothing(service, body):
    response = client_as(ADMIN).post(
        "/api/harnesses/h1/slack/test", content=body, headers={"content-type": "application/json"}
    )
    assert response.status_code == 400
    assert "secret" not in response.text and "input" not in response.text


@pytest.mark.parametrize(
    "exc,status",
    [
        (SlackNotConfigured("off"), 503),
        (IntegrationNotFound("h1"), 404),
        (IntegrationExists("app"), 409),
        (IntegrationExists("harness"), 409),
        (ValidationFailed({"ok": False, "checks": [{"id": "bot_scopes", "ok": False, "message": "m"}]}), 400),
        (DeletionIncomplete([{"step": "revoke_token", "ok": False, "error": "network_error"}]), 502),
        (RuntimeError(f"leaked {BOT}"), 500),
    ],
)
def test_service_failures_map_to_statuses_without_leaking(service, exc, status, caplog):
    service.raise_ = exc
    response = client_as(ADMIN).post("/api/harnesses/h1/slack", json={"bot_token": BOT, "app_token": APP})
    assert response.status_code == status
    assert "super-secret" not in response.text
    assert "super-secret" not in caplog.text


def test_validation_failures_carry_the_checks(service):
    service.raise_ = ValidationFailed({"ok": False, "checks": [{"id": "same_app", "ok": False, "message": "m"}]})
    detail = client_as(ADMIN).post("/api/harnesses/h1/slack/test", json={"bot_token": BOT, "app_token": APP}).json()["detail"]
    assert detail["checks"][0]["id"] == "same_app"


def test_incomplete_deletion_returns_its_steps(service):
    service.raise_ = DeletionIncomplete([{"step": "revoke_token", "ok": False, "error": "network_error"}])
    detail = client_as(ADMIN).delete("/api/harnesses/h1/slack").json()["detail"]
    assert detail["steps"][0]["step"] == "revoke_token"


# ── Harness deletion ────────────────────────────────────────────────────────

@pytest.fixture
def harness_routes(monkeypatch):
    """routes.harness against a stand-in core.dependencies, so this runs without
    AWS. Restored afterwards so no other test sees the stand-in."""
    saved = {name: sys.modules.get(name) for name in ("core.dependencies", "routes.harness")}
    fake = types.ModuleType("core.dependencies")
    fake.team_service = None
    fake.slack_service = None
    sys.modules["core.dependencies"] = fake
    sys.modules.pop("routes.harness", None)
    module = importlib.import_module("routes.harness")
    yield module, fake
    for name, mod in saved.items():
        if mod is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = mod


class Cleanup:
    def __init__(self, exc=None):
        self.exc = exc
        self.calls = []

    async def cleanup_for_harness(self, harness_id, user):
        self.calls.append(harness_id)
        if self.exc:
            raise self.exc


class Harnesses:
    def __init__(self):
        self.deleted = []

    def delete_harness(self, harness_id):
        self.deleted.append(harness_id)
        return []


def _harness_client(module):
    app = FastAPI()
    app.include_router(module.router)
    app.dependency_overrides[auth.current_user] = lambda: ADMIN
    return TestClient(app)


def test_harness_delete_cleans_up_slack_first(harness_routes, monkeypatch):
    module, deps = harness_routes
    deps.slack_service = Cleanup()
    harnesses = Harnesses()
    monkeypatch.setattr(module, "_harness", lambda: harnesses)
    assert _harness_client(module).delete("/api/harnesses/h1").status_code == 200
    assert deps.slack_service.calls == ["h1"] and harnesses.deleted == ["h1"]


def test_harness_stays_when_slack_cleanup_is_incomplete(harness_routes, monkeypatch):
    module, deps = harness_routes
    deps.slack_service = Cleanup(DeletionIncomplete([{"step": "revoke_token", "ok": False, "error": "network_error"}]))
    harnesses = Harnesses()
    monkeypatch.setattr(module, "_harness", lambda: harnesses)
    response = _harness_client(module).delete("/api/harnesses/h1")
    assert response.status_code == 409
    assert response.json()["detail"]["steps"][0]["step"] == "revoke_token"
    assert harnesses.deleted == []
