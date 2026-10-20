"""A busy Cognito must not sign anyone out.

`/api/auth/refresh` turned every failure into `success: false` "세션이 만료되었습니다",
and the web clears the session on that. Cognito throttles InitiateAuth (~120/s
per pool by default) and 1,000 people refreshing together reached it (bst,
2026-10-18). Throttling and Cognito's own outages are 503 now, which the web
treats as transient; a rejected refresh token is still `success: false`.
"""
import os
import sys

import pytest
from botocore.exceptions import ClientError
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routes.auth as auth_routes  # noqa: E402
from core import config  # noqa: E402


class Cognito:
    def __init__(self, code):
        self.code = code

    def initiate_auth(self, **kw):
        raise ClientError({"Error": {"Code": self.code, "Message": self.code}}, "InitiateAuth")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(config, "COGNITO_USER_POOL_CLIENT_ID", "cog-client", raising=False)
    monkeypatch.delenv("COGNITO_CLIENT_SECRET", raising=False)
    app = FastAPI()
    app.include_router(auth_routes.router)

    def with_error(code):
        monkeypatch.setattr(auth_routes, "_cognito", lambda region: Cognito(code))
        return TestClient(app)
    return with_error


@pytest.mark.parametrize("code", ["TooManyRequestsException", "InternalErrorException"])
def test_a_throttled_refresh_is_503_not_a_signed_out_session(client, code):
    res = client(code).post("/api/auth/refresh", json={"refresh_token": "r"})
    assert res.status_code == 503
    assert res.headers["retry-after"] == "2"


def test_a_rejected_refresh_token_still_ends_the_session(client):
    res = client("NotAuthorizedException").post("/api/auth/refresh", json={"refresh_token": "r"})
    assert res.status_code == 200 and res.json()["success"] is False


def test_a_throttled_login_is_503_and_a_wrong_password_is_not(client):
    assert client("TooManyRequestsException").post(
        "/api/auth/login", json={"username": "a@b.c", "password": "p"}).status_code == 503
    res = client("NotAuthorizedException").post("/api/auth/login", json={"username": "a@b.c", "password": "p"})
    assert res.status_code == 200 and res.json()["success"] is False
