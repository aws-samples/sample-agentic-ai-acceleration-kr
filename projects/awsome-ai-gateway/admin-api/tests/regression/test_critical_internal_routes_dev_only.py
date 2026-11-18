# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""``/internal/*`` 는 개발 환경에서만 열린다(``core/environment.py``).

배경 — ``/internal/*`` 는 인증이 없는 라우트들이다. ``/internal/test/issue-key`` 는 STS 검증 없이
사용자를 만들고 **실제로 쓸 수 있는 virtual key 를 발급**한다(통합 테스트용).

  * 가드가 ``APP_ENV == "production"`` 이었는데 Helm 차트는 운영 환경에 ``"prod"`` 를 넣는다.
    문자열 비교는 값이 달라도 에러를 내지 않으므로, 그 가드는 운영 환경에서 한 번도 동작하지
    않았다 — 관리자 API 에 닿는 누구나 인증 없이 키를 받았다.
  * ``/internal/cache/retry`` 에는 가드가 아예 없었다.
  * 기존 테스트는 ``APP_ENV="production"`` 으로 돌았기 때문에 이 차이를 원리적으로 잡지 못했다.

여기서 고정하는 것:

  1. 개발 환경이 아니면(``prod`` 포함) 세 라우트 모두 403 이고, DB 세션도 핸들러도 실행되지 않는다.
  2. 개발 환경에서는 예전처럼 동작한다(대조군) — 이게 없으면 "전부 막아서 통과" 하는 공허한 테스트다.
  3. 프로브가 쓰는 ``/health*`` 는 어느 환경에서나 열려 있다.
  4. 실제 앱의 ``/internal/*`` 라우트는 **전부** 이 가드를 달고 있다 — 새 라우트를 가드 없이 추가하면
     여기서 잡힌다.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

from app.core import auth, environment
from app.core.auth import CurrentUser, require_admin_outside_development
from app.core.db import get_db_session
from app.core.environment import require_development_environment
from app.models.auth import UserRole
from app.routers import internal, productivity

DEV_ENVS = ["dev", "development", "local", "test", "DEV", " Development "]
NON_DEV_ENVS = ["prod", "production", "PROD", "staging", "stg", "qa", "demo", "", "developmentx"]
# 디버그·테스트용 — 개발 환경에서만 열린다.
INTERNAL_PATHS = ["/internal/cache/retry", "/internal/scheduler/run", "/internal/test/issue-key"]
# 다른 시스템이 호출하는 수집 라우트 — 운영 환경에서는 ADMIN(서비스 토큰)을 요구한다.
PRODUCTIVITY_PATH = "/internal/productivity"


def _app_env(value: str):
    return patch.object(environment, "get_settings", return_value=MagicMock(APP_ENV=value))


class _Harness:
    """``internal.router`` 만 붙인 앱. DB 세션 의존성이 **열렸는지**를 센다."""

    def __init__(self) -> None:
        self.db_sessions_opened = 0
        self.cache_mgr = MagicMock()
        self.cache_mgr.retry_failed = AsyncMock(return_value=3)

        app = FastAPI()
        app.include_router(internal.router)
        app.state.cache_mgr = self.cache_mgr

        async def _session():
            self.db_sessions_opened += 1
            yield MagicMock()

        app.dependency_overrides[get_db_session] = _session
        self.app = app

    def client(self) -> AsyncClient:
        return AsyncClient(transport=ASGITransport(app=self.app), base_url="http://admin-api.test")


@pytest.fixture
def harness() -> _Harness:
    return _Harness()


# ─────────────────────────────────────────────────────────────────────────────
# 1. 개발 환경이 아닐 때
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("app_env", NON_DEV_ENVS)
@pytest.mark.parametrize("path", INTERNAL_PATHS)
async def test_internal_routes_are_refused_outside_development(harness, app_env, path):
    with _app_env(app_env):
        async with harness.client() as client:
            resp = await client.post(path, json={"email": "attacker@example.com"})
    assert resp.status_code == 403
    # 막힌 요청은 아무 일도 하지 않는다 — DB 커넥션도 잡지 않고 핸들러도 돌지 않는다.
    assert harness.db_sessions_opened == 0
    harness.cache_mgr.retry_failed.assert_not_awaited()


async def test_the_environment_name_the_chart_injects_is_refused(harness):
    """정확히 그 사고의 형상 — 차트는 운영 환경에 ``prod`` 를 넣고, 예전 가드는 ``production`` 만 봤다."""
    with _app_env("prod"):
        async with harness.client() as client:
            resp = await client.post("/internal/test/issue-key", json={"email": "attacker@example.com"})
    assert resp.status_code == 403
    assert "virtual_key" not in resp.text


# ─────────────────────────────────────────────────────────────────────────────
# 2. 대조군 — 개발 환경에서는 예전처럼 동작한다
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("app_env", DEV_ENVS)
async def test_internal_routes_still_work_in_development(harness, app_env):
    with _app_env(app_env):
        async with harness.client() as client:
            resp = await client.post("/internal/cache/retry")
    assert resp.status_code == 200
    assert resp.json() == {"resolved": 3}
    assert harness.db_sessions_opened == 1
    harness.cache_mgr.retry_failed.assert_awaited_once()


# ─────────────────────────────────────────────────────────────────────────────
# 3. 프로브
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("app_env", ["prod", "dev"])
@pytest.mark.parametrize("path", ["/health", "/health/ready"])
async def test_health_probes_are_open_in_every_environment(harness, app_env, path):
    with _app_env(app_env):
        async with harness.client() as client:
            resp = await client.get(path)
    assert resp.status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
# 4. /internal/productivity — 운영 환경에서는 ADMIN 만
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def productivity_client():
    """``productivity.router`` 만 붙인 앱. 본문을 일부러 틀리게 보낸다 — 가드를 통과하면 422, 막히면 401/403."""
    app = FastAPI()
    app.include_router(productivity.router)
    opened: list[int] = []

    async def _session():
        opened.append(1)
        yield MagicMock()

    app.dependency_overrides[get_db_session] = _session
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://admin-api.test")
    client.db_sessions_opened = opened  # type: ignore[attr-defined]
    return client


def _auth_env(value: str):
    return patch.object(auth, "get_settings", return_value=MagicMock(APP_ENV=value))


def _user(role: UserRole) -> CurrentUser:
    import uuid

    return CurrentUser(user_id=uuid.uuid4(), email="svc@example.com", role=role, team_id=None)


@pytest.mark.parametrize("app_env", NON_DEV_ENVS)
async def test_productivity_ingest_needs_a_token_outside_development(productivity_client, app_env):
    """토큰이 없으면 401 — 진짜 ``get_current_user`` 를 탄다."""
    with _auth_env(app_env):
        async with productivity_client as client:
            resp = await client.post(PRODUCTIVITY_PATH, json={"not": "a valid event"})
    assert resp.status_code == 401
    assert productivity_client.db_sessions_opened == []


async def test_productivity_ingest_refuses_a_non_admin_outside_development(productivity_client):
    with _auth_env("prod"), patch.object(auth, "get_current_user", AsyncMock(return_value=_user(UserRole.DEVELOPER))):
        async with productivity_client as client:
            resp = await client.post(PRODUCTIVITY_PATH, json={"not": "a valid event"})
    assert resp.status_code == 403
    assert productivity_client.db_sessions_opened == []


async def test_productivity_ingest_accepts_an_admin_outside_development(productivity_client):
    """서비스 토큰은 ADMIN 신원으로 풀린다. 가드를 지났다는 증거로 본문 검증(422)까지 간다."""
    with _auth_env("prod"), patch.object(auth, "get_current_user", AsyncMock(return_value=_user(UserRole.ADMIN))):
        async with productivity_client as client:
            resp = await client.post(PRODUCTIVITY_PATH, json={"not": "a valid event"})
    assert resp.status_code == 422


@pytest.mark.parametrize("app_env", ["dev", "development", "local", "test"])
async def test_productivity_ingest_is_open_in_development(productivity_client, app_env):
    """대조군 — 개발 환경에서는 토큰 없이도 가드를 지난다(예전 동작)."""
    with _auth_env(app_env):
        async with productivity_client as client:
            resp = await client.post(PRODUCTIVITY_PATH, json={"not": "a valid event"})
    assert resp.status_code == 422


# ─────────────────────────────────────────────────────────────────────────────
# 5. 실제 앱 — 가드 없는 /internal 라우트가 없고, 인증 없는 라우트는 목록에 있는 것뿐이다
# ─────────────────────────────────────────────────────────────────────────────


def _all_dependency_calls(dependant, seen: set | None = None) -> set:
    seen = set() if seen is None else seen
    for dep in dependant.dependencies:
        if dep.call not in seen:
            seen.add(dep.call)
            _all_dependency_calls(dep, seen)
    return seen


def test_every_internal_route_of_the_real_app_carries_a_guard():
    from app.main import app

    internal_routes = [r for r in app.routes if isinstance(r, APIRoute) and r.path.startswith("/internal")]
    # 대조군 — 경로를 잘못 걸러 0개를 "전부 통과" 로 읽지 않는다.
    assert sorted(r.path for r in internal_routes) == sorted([*INTERNAL_PATHS, PRODUCTIVITY_PATH])

    for route in internal_routes:
        guards = [d.call for d in route.dependant.dependencies]
        expected = (
            require_admin_outside_development if route.path == PRODUCTIVITY_PATH else require_development_environment
        )
        assert expected in guards, f"{route.path} has no guard"
        # 가드가 DB 세션보다 먼저 풀려야 막힌 요청이 커넥션을 잡지 않는다.
        assert guards.index(expected) < guards.index(get_db_session)


def test_routes_without_authentication_are_exactly_the_known_ones():
    """인증 의존성이 없는 라우트의 **전수 목록**. 새 라우트를 인증 없이 추가하면 여기서 멈춘다.

    목록에 넣기 전에 물을 것: 이 라우트가 운영 환경에서 인터넷에 열려 있어도 되는가?
    """
    from app.main import app

    authenticators = {
        auth.get_current_user,
        auth.require_admin,
        auth.require_admin_or_team_leader,
        require_development_environment,
        require_admin_outside_development,
    }
    unauthenticated = set()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        calls = _all_dependency_calls(route.dependant)
        names = {getattr(c, "__qualname__", "") for c in calls}
        # require_team_leader_of(...) 는 호출마다 새 클로저를 만든다 — 이름으로 잡는다.
        if calls & authenticators or any(n.startswith("require_team_leader_of") for n in names):
            continue
        unauthenticated.add(route.path)

    assert unauthenticated == {
        "/health",  # 프로브
        "/health/ready",  # 프로브
        "/cli/setup",  # 도구 설정 안내(정적 값)
        "/cli/auth/virtual-key",  # 핸들러가 STS 로 신원을 확인한다 — 계정·역할 대조는 cli_service
        "/v1/auth/exchange",  # 핸들러가 OIDC 토큰을 검증한다
        # ⚠️ 알려진 공백: 서명 검증이 없다(X-Hub-Signature-256). 누구나 git 이벤트를 써넣을 수 있다.
        "/webhooks/git",
    }


def test_health_routes_of_the_real_app_do_not_carry_the_guard():
    """프로브가 운영 환경에서 403 을 받으면 파드가 Ready 가 되지 못한다."""
    from app.main import app

    health = [r for r in app.routes if isinstance(r, APIRoute) and r.path in ("/health", "/health/ready")]
    assert len(health) == 2
    for route in health:
        assert require_development_environment not in [d.call for d in route.dependant.dependencies]
