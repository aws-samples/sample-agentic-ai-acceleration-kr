"""
The auth dependency of every route, pinned.

Four routers — threads, mcp, mcp_apps and artifacts — once shipped
with no auth dependency at all, which is what this file exists to stop
recurring. The value is in ROUTES and in test_every_route_is_in_the_table:
the table is the spec, and a new route that is not in it fails the suite.

Only the gate is under test here. A passing case asserts "not 401 and not 403"
rather than a specific 2xx, because the response bodies belong to each router's
own tests.

Ownership is the other axis and lives in test_thread_ownership.py: every thread
route below is reachable by any logged-in user, which is precisely why "who owns
this thread" has to be tested somewhere else. The stubs here therefore hand each
caller a thread they own, so an ownership refusal never masquerades as a gate
failure. A new thread route needs a case in both files.
"""
import os
import sys
from dataclasses import dataclass
from typing import Any, Dict, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.auth as auth  # noqa: E402
import routes.artifacts as artifact_routes  # noqa: E402
import routes.insights as insights_routes  # noqa: E402
import routes.mcp as mcp_routes  # noqa: E402
import routes.mcp_apps as mcp_apps_routes  # noqa: E402
import routes.threads as thread_routes  # noqa: E402
from models.artifact import (  # noqa: E402
    ArtifactContent,
    ArtifactDetail,
    ArtifactVersion,
)
from models.attachment import Attachment  # noqa: E402
from models.thread import Thread  # noqa: E402

ADMIN = auth.AuthUser(sub="u-admin", username="alice", groups=["admin"])
USER = auth.AuthUser(sub="u-plain", username="bob", groups=["user"])


@dataclass(frozen=True)
class Route:
    method: str
    path: str
    admin_only: bool
    body: Optional[Dict[str, Any]] = None
    # Query string for routes whose required parameters live there; kept apart
    # from `path` so the table still matches the app's path template.
    query: Optional[str] = None


ROUTES = [
    # threads — every one only needs a session, writes included: this is chat.
    Route("GET", "/threads/t1", False),
    Route("GET", "/threads", False),
    Route("GET", "/threads/t1/state", False),
    Route("POST", "/threads", False, body={}),
    Route("PATCH", "/threads/t1/state", False, body={"values": {"a": 1}}),
    Route("POST", "/threads/t1/runs/stream", False, body={}),
    Route("POST", "/threads/t1/stream", False, body={}),
    Route("GET", "/threads/t1/runs/stream", False),
    Route("POST", "/threads/t1/runs/cancel", False),
    Route("DELETE", "/threads/t1", False),
    Route("POST", "/threads/t1/attachments", False, body={}),
    Route("GET", "/threads/t1/attachments/att1", False),
    Route("GET", "/threads/t1/browser-screenshots/tooluse_1", False),
    # mcp-apps — 앱 안의 버튼 클릭이라 세션만 필요하다. /api/mcp/* 와 달리
    # server_config를 받지 않고 레지스트리 record_id로만 엔드포인트를 정하므로
    # 임의 명령 실행 경로가 없다.
    # (ui-actions는 이 마이그레이션에서 삭제됐다 — 규격의 ui/message와
    #  tools/call이 대체한다.)
    Route("POST", "/api/mcp-apps/resources/read", False,
          body={"record_id": "r1", "uri": "ui://x/view"}),
    Route("POST", "/api/mcp-apps/tools/call", False,
          body={"record_id": "r1", "tool_name": "t", "arguments": {}}),
    # Same scope as tools/call: it reads a tool's schema off a registry record.
    Route("POST", "/api/mcp-apps/tools/describe", False,
          body={"record_id": "r1", "tool_name": "t"}),
    # artifacts — /share included; it mints a URL for what the caller can read.
    Route("GET", "/api/artifacts/thread/t1", False),
    Route("GET", "/api/artifacts/a1", False),
    Route("GET", "/api/artifacts/a1/versions/1/content", False),
    Route("POST", "/api/artifacts/a1/versions/1/share", False),
    Route("GET", "/api/artifacts/a1/versions/1/download", False),
    Route("GET", "/api/artifacts/a1/versions/1/preview", False),
    # mcp — admin throughout. /tools and /tools/call run caller-supplied
    # commands through _create_stdio_client.
    Route("GET", "/api/mcp/servers", True),
    Route("POST", "/api/mcp/tools", True, body={"mcp_servers": {}}),
    Route("POST", "/api/mcp/tools/call", True,
          body={"server_name": "s", "server_config": {}, "tool_name": "t",
                "arguments": {}}),
    Route("POST", "/api/mcp/cognito/login", True, body={"client_id": "x"}),
    # /records/* take no server_config — the endpoint is resolved from the
    # registry, so they could be open like /api/mcp-apps/*. Admin because a raw
    # tool call with hand-written arguments is an operator's action, and this
    # router is admin as a whole so no route here can be forgotten later.
    Route("GET", "/api/mcp/records/r1/tools", True),
    Route("POST", "/api/mcp/records/tools/call", True,
          body={"record_id": "r1", "tool_name": "t", "arguments": {}}),
    # insights — the org-wide aggregates are admin-only: the leaderboard, the
    # metered CloudWatch tier and the composition rollup are every agent's traffic
    # and spend across the org, and the page they feed is admin-only. The 403 is
    # raised before the 501, so a plain user cannot even learn the feature exists.
    Route("GET", "/api/insights/summary", True),
    # The vended tier, on its own route because it is the metered one — and admin,
    # like /summary: same fleet, priced.
    Route("GET", "/api/insights/telemetry", True),
    Route("GET", "/api/insights/composition", True),
    # Per-record insights is NOT admin-gated: the Registry page (open to plain
    # users) reads it for a single record's usage tab.
    Route("GET", "/api/insights/records/rec-1", False),
    Route("GET", "/api/insights/me", False),
    # Usage per person. Admin, unlike every other aggregate here: those name
    # agents, which are public registry records, and this one names people. `/me`
    # stays the owner-scoped door to the same counters.
    Route("GET", "/api/insights/users", True),
    # Spend and policy denials per team; inline is_admin check like /users.
    Route("GET", "/api/insights/teams", True),
    # Model rate card. Admin throughout (inline _require_admin): registering or
    # removing a rate reprices every turn in the ledger, and the Settings tab that
    # drives these is admin-only.
    Route("GET", "/api/insights/rates", True),
    Route("POST", "/api/insights/rates/fetch", True),
    Route("PUT", "/api/insights/rates", True, body={"entries": []}),
    Route("DELETE", "/api/insights/rates", True, query="key=R%23f%7Cr%7Ct%7C2026-01-01"),
    # Span drill-down. Owner-or-admin, unlike the rest of insights: a span
    # timeline is the inside of one person's conversation. A 403 would confirm
    # the thread exists; we return 404 instead, matching thread routes.
    Route("GET", "/api/insights/traces/t1", False),
    # The threads one agent answered in — same owner-or-admin scoping as
    # GET /threads, because the record is public but the conversations are not.
    Route("GET", "/api/insights/records/rec-1/threads", False),
    # Batch evaluations. Evaluators and status are public reads; starting is
    # admin-only. Record evaluation is public.
    Route("GET", "/api/insights/evaluators", False),
    Route("POST", "/api/insights/evaluations", True,
          body={"thread_ids": ["t1"], "evaluator_ids": ["e1"]}),
    Route("GET", "/api/insights/evaluations/b1", False),
    Route("GET", "/api/insights/records/rec-1/evaluation", False),
    # Insights analyses mirror evaluations: starting one is admin-only (it spends
    # model calls on other people's threads); status and record views are public.
    Route("POST", "/api/insights/analyses", True, body={"thread_ids": ["t1"]}),
    Route("GET", "/api/insights/analyses/b1", False),
    Route("GET", "/api/insights/records/rec-1/analysis", False),
    # Dashboard layout: per-user preference storage.
    Route("GET", "/api/insights/layout", False),
    Route("PUT", "/api/insights/layout", False, body={"version": 1, "widgets": []}),
]

# Already gated, with their own route tests (test_registry_rbac.py,
# test_knowledge_routes.py). Listed so the coverage guard below sees full
# coverage; not exercised here, which would only duplicate those files.
GATED_ELSEWHERE = [
    # Sidebar menu visibility (test_settings_routes.py): anyone signed in reads
    # which menus are hidden, only an admin changes it.
    Route("GET", "/api/settings/nav", False),
    Route("PUT", "/api/settings/nav", True),
    Route("GET", "/api/settings/teams", False),
    Route("PUT", "/api/settings/teams/{name}", True),
    # Called once after an OIDC login with the fresh id_token (test_auth_routes.py).
    Route("POST", "/api/auth/session", False),
    Route("GET", "/api/registry/info", False),
    Route("GET", "/api/registry/records", False),
    Route("GET", "/api/registry/records/r1", False),
    Route("GET", "/api/registry/search", False),
    Route("GET", "/api/registry/runtimes", False),
    Route("GET", "/api/registry/gateways", False),
    Route("GET", "/api/registry/deployed", False),
    Route("POST", "/api/registry/sync", True),
    Route("POST", "/api/registry/records", True),
    Route("PATCH", "/api/registry/records/r1", True),
    Route("POST", "/api/registry/records/r1/status", True),
    # Re-fetches the descriptor from the record's source with the registry's
    # outbound credentials and rewrites the record: a curator's action.
    Route("POST", "/api/registry/records/r1/sync", True),
    Route("DELETE", "/api/registry/records/r1", True),
    # Skill bundles (test_skill_routes.py). Validation writes nothing — it exists
    # so the upload dialog can report problems — so it needs no more than a
    # session. Publishing follows POST /records: admin, because it creates a
    # registry record and puts objects in the shared skills bucket.
    Route("POST", "/api/registry/skills/validate", False),
    Route("POST", "/api/registry/skills", True),
    Route("PUT", "/api/registry/skills/r1", True),
    Route("GET", "/api/registry/skills/r1/files", False),
    # Bucket-only skills (registry-off): listing is a read like GET /records;
    # publishing and deleting write the shared skills bucket, so admin.
    Route("GET", "/api/registry/skills/bucket", False),
    Route("POST", "/api/registry/skills/bucket", True),
    Route("DELETE", "/api/registry/skills/bucket", True),
    # Note the plural: the router's prefix is /api/harnesses.
    Route("GET", "/api/harnesses/catalog", False),
    Route("GET", "/api/harnesses", False),
    Route("GET", "/api/harnesses/h1", False),
    Route("POST", "/api/harnesses", True),
    # Admin like delete: no owner on a harness, and recomposing rewrites an
    # agent other people already use.
    Route("PUT", "/api/harnesses/h1", True, body={}),
    Route("DELETE", "/api/harnesses/h1", True),
    Route("GET", "/api/knowledge", False),
    Route("POST", "/api/knowledge", False),
    Route("GET", "/api/knowledge/source-buckets", False),
    Route("GET", "/api/knowledge/kb1", False),
    Route("DELETE", "/api/knowledge/kb1", False),
    Route("POST", "/api/knowledge/kb1/documents", False),
    Route("DELETE", "/api/knowledge/kb1/documents/d1", False),
    Route("POST", "/api/knowledge/kb1/sync", False),
    Route("GET", "/api/knowledge/kb1/sync", False),
]

# Open by design. `/` is the ALB target group's health check path
# (infra/modules/ecs/main.tf) — a 401 there cycles every task as unhealthy.
# The auth router issues the tokens, so it cannot require one.
# /api/config carries only capability booleans (registry/harness on or off) so the
# login screen and shell can branch before a session exists; no secrets, no ids.
# /api/auth/config is the login screen's bootstrap: which providers exist, the
# OIDC client id and callback — public by design (PKCE has no secret) and read
# before anyone has a token.
UNGATED_PATHS = {"/", "/api/auth/login", "/api/auth/refresh", "/api/auth/config", "/api/config"}


class StubThreadService:
    """Grants ownership to whoever asks.

    require_owned returning a thread unconditionally is the point: this file
    must fail only on the auth gate, so a 403 here can never be an ownership
    refusal. test_thread_ownership.py is where refusals are asserted.
    """

    def _thread(self, thread_id):
        return Thread(
            thread_id=thread_id,
            created_at="2026-08-07T00:00:00",
            updated_at="2026-08-07T00:00:00",
            values={},
            metadata={},
            owner_sub="whoever-is-asking",
        )

    def require_owned(self, thread_id, owner_sub, is_admin=False):
        return self._thread(thread_id)

    def get_thread(self, thread_id):
        return self._thread(thread_id)

    def search_threads(self, owner_sub, **kwargs):
        return []

    def create_thread(self, owner_sub, thread_data=None):
        return {"thread_id": "t1"}

    def update_thread_state(self, thread_id, update):
        return {"thread_id": thread_id}

    def delete_thread(self, thread_id):
        return None


class StubStreamingService:
    async def stream_thread_execution(
        self, thread_id, request, actor_id=None, owner_sub="", caller=None
    ):
        # Signature matches the real one. Accepting only (thread_id, request)
        # made every streaming case here pass through a 500, which "not 401 and
        # not 403" cannot tell apart from a working route.
        return {"ok": True}


class StubAttachmentService:
    def store(self, thread_id, filename, body):
        return Attachment(
            attachment_id="att1", filename=filename, media_type="image/png",
            size_bytes=len(body), kind="image",
        )

    def fetch(self, thread_id, attachment_id):
        return (b"bytes", "image/png", "a.png")


VERSION = ArtifactVersion(
    artifact_id="a1", version=1, thread_id="t1", title="Doc", kind="markdown",
    s3_key="k", size_bytes=3, created_at="2026-08-07T00:00:00",
)


class StubArtifactService:
    """Returns real models, not exceptions.

    Raising here would still satisfy "not 401 and not 403" via a 500, but a 500
    is also what a genuinely broken gate looks like — and `_fail` would log a
    traceback per test. Honest values keep a real failure visible.
    """

    def thread_of(self, artifact_id):
        return VERSION.thread_id

    def list_for_thread(self, thread_id):
        return [VERSION]

    def detail(self, artifact_id):
        return ArtifactDetail(latest=VERSION, versions=[1])

    def content(self, artifact_id, version):
        return ArtifactContent(
            artifact_id=artifact_id, version=version, kind="markdown",
            title="Doc", content="hi",
        )

    def share_url(self, artifact_id, version, expires_in=None, download=False):
        return ("https://example.invalid/signed", 60)

    def binary_body(self, artifact_id, version):
        return b"PK\x03\x04", "보고서.docx", (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )

    def preview(self, artifact_id, version):
        return {"media": "markdown", "text": "preview"}


@pytest.fixture
def app(monkeypatch):
    """The real routers, with everything below them stubbed out.

    No AWS, no subprocess, no model call: `create_mcp_client_from_config`
    returning None makes the MCP routes answer with their "failed to create
    client" body without ever reaching _create_stdio_client.
    """
    threads = StubThreadService()
    monkeypatch.setattr(thread_routes, "thread_service", threads)
    monkeypatch.setattr(thread_routes, "streaming_service", StubStreamingService())
    monkeypatch.setattr(
        thread_routes, "attachment_service", StubAttachmentService()
    )
    monkeypatch.setattr(artifact_routes, "thread_service", threads)
    monkeypatch.setattr(artifact_routes, "artifact_service", StubArtifactService())
    monkeypatch.setattr(
        mcp_routes.MCPService, "create_mcp_client_from_config",
        staticmethod(lambda name, config: None),
    )
    # Stub the relay to avoid real network calls
    stub_relay = type('StubRelay', (), {
        'read_ui_resource': lambda self, r, u: None,
        'tool_meta_for': lambda self, r, t: None,
        'call_tool': lambda self, r, t, a: None,
    })()
    monkeypatch.setattr(mcp_apps_routes, "_relay", lambda: stub_relay)

    # Insights routes answer 501 when USAGE_TABLE is unset, which is valid.
    # The dependency override lets the gate pass tests work.
    class StubUsageService:
        configured = False

    monkeypatch.setattr(insights_routes, "usage_service", StubUsageService())
    monkeypatch.setattr(insights_routes, "telemetry_service", None)

    application = FastAPI()
    application.include_router(thread_routes.router)
    application.include_router(mcp_apps_routes.router)
    application.include_router(mcp_routes.router)
    application.include_router(artifact_routes.router)
    application.include_router(insights_routes.router)
    return application


def _call(application, route, user=None):
    if user is not None:
        application.dependency_overrides[auth.current_user] = lambda: user
    client = TestClient(application, raise_server_exceptions=False)
    url = f"{route.path}?{route.query}" if route.query else route.path
    return client.request(route.method, url, json=route.body)


@pytest.mark.parametrize("route", ROUTES, ids=lambda r: f"{r.method} {r.path}")
def test_no_token_is_401(route, app, monkeypatch):
    """Reaches the real current_user — no dependency override here."""
    monkeypatch.setattr(auth.config, "AUTH_ENFORCED", True, raising=False)
    monkeypatch.setattr(auth.config, "COGNITO_USER_POOL_ID", "us-east-1_pool", raising=False)

    assert _call(app, route).status_code == 401


@pytest.mark.parametrize("route", ROUTES, ids=lambda r: f"{r.method} {r.path}")
def test_a_plain_user_is_allowed_unless_the_route_is_admin_only(route, app):
    response = _call(app, route, USER)

    if route.admin_only:
        assert response.status_code == 403
    else:
        assert response.status_code not in (401, 403)


@pytest.mark.parametrize("route", ROUTES, ids=lambda r: f"{r.method} {r.path}")
def test_an_admin_is_allowed_everywhere(route, app):
    assert _call(app, route, ADMIN).status_code not in (401, 403)


def test_every_route_is_in_the_table():
    """The guard that makes this file more than a snapshot.

    A new route with no gate is invisible to the tests above, because they only
    walk ROUTES. This walks the real app instead, so forgetting the table is
    what fails.
    """
    import main  # noqa: PLC0415 — importing at module scope would boot AWS clients

    tabled = {(r.method, r.path) for r in ROUTES + GATED_ELSEWHERE}
    missing = []
    for route in main.app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if path is None or methods is None or path in UNGATED_PATHS:
            continue
        if path.startswith(("/openapi", "/docs", "/redoc")):
            continue
        for method in methods - {"HEAD", "OPTIONS"}:
            if (method, path) not in tabled and not _is_covered(method, path, tabled):
                missing.append(f"{method} {path}")

    assert not missing, (
        "These routes are not in ROUTES. Add them with the dependency they "
        f"should have, or to UNGATED_PATHS with a reason: {sorted(missing)}"
    )


def _is_covered(method, path, tabled):
    """Match a declared path template against the concrete paths in ROUTES."""
    import re

    pattern = re.sub(r"\{[^}]+\}", "[^/]+", path)
    return any(
        m == method and re.fullmatch(pattern, p) for m, p in tabled
    )
