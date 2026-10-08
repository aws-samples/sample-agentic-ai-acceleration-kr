"""
Route-level RBAC tests for the registry.

These pin down which paths need admin and which only need a session: requiring
admin on a read path would break chat for ordinary users, and leaving a write
path open is what this work is closing.
"""
import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.auth as auth  # noqa: E402
from services.registry_service import RegistryNotConfigured  # noqa: E402
import routes.registry as registry_routes  # noqa: E402

WRITE_PATHS = [
    ("post", "/api/registry/records"),
    ("patch", "/api/registry/records/rec-1"),
    ("post", "/api/registry/records/rec-1/status"),
    ("delete", "/api/registry/records/rec-1"),
    ("post", "/api/registry/sync"),
]

READ_PATHS = [
    "/api/registry/info",
    "/api/registry/records",
    "/api/registry/search?q=x",
]


class StubRegistry:
    """Minimal stub to satisfy read routes without hitting AWS."""

    def get_registry_info(self):
        return {"registry_id": "stub"}

    def list_records(self, descriptor_type=None, status=None, name=None):
        return []

    def search_records(self, query=None, descriptor_types=None):
        return []

    def list_agent_runtimes(self):
        return []

    def list_gateways(self):
        return []

    def list_deployed_targets(self):
        return []

    def get_record(self, record_id):
        return {"record_id": record_id}

    def update_record(self, record_id, req):
        return {"record_id": record_id}


def _client(user, monkeypatch):
    """App wired to the real routes but a stubbed registry layer."""
    # Stub the module-level service so read routes don't hit AWS.
    monkeypatch.setattr(registry_routes, "_service", StubRegistry())
    app = FastAPI()
    app.include_router(registry_routes.router)
    # Bypass token verification; these tests are about the role gate.
    app.dependency_overrides[auth.current_user] = lambda: user
    return TestClient(app, raise_server_exceptions=False)


ADMIN = auth.AuthUser(username="alice", groups=["admin"])
USER = auth.AuthUser(username="bob", groups=["user"])


@pytest.mark.parametrize("method,path", WRITE_PATHS)
def test_write_paths_reject_non_admin(method, path, monkeypatch):
    client = _client(USER, monkeypatch)
    res = client.request(method.upper(), path, json={})
    assert res.status_code == 403


@pytest.mark.parametrize("path", READ_PATHS)
def test_read_paths_allow_a_plain_user(path, monkeypatch):
    client = _client(USER, monkeypatch)
    res = client.get(path)
    # Not 401/403. The role gate did not block the request.
    assert res.status_code not in (401, 403)


def test_write_paths_are_reachable_for_admin(monkeypatch):
    client = _client(ADMIN, monkeypatch)
    res = client.patch("/api/registry/records/rec-1", json={"description": "x"})
    assert res.status_code != 403
