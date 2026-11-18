# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""팀 리더 역할이 CLI 로그인(``POST /v1/auth/exchange``)과 Cognito 동기화를 지나도 남는다.

배경 — 관리자가 지정한 팀 리더(``PUT /admin/teams/{id}/leader`` → ``teams.leader_user_id`` +
``users.role = TEAM_LEADER``)가 gateway-cli 의 매시간 교환 한 번에 DEVELOPER 로 돌아갔다(실 PostgreSQL·실
IdP 토큰으로 실측). 교환과 동기화가 둘 다 ``derive_role`` 값을 그대로 저장했는데, IdP 는 팀 리더를
모르므로 그 값은 ADMIN 아니면 DEVELOPER 다.

규칙(``core.oidc_identity.reconcile_role``): 기존 사용자는 IdP 가 ADMIN 이라 하면 ADMIN, 아니면 **이번에
속하게 된 팀**이 그를 리더로 가리킬 때만 TEAM_LEADER, 그 밖에는 DEVELOPER. 저장돼 있던 TEAM_LEADER 를
그냥 남기지 않는다 — 다른 팀으로 옮겨진 리더가 역할을 들고 가면 아무도 지정하지 않은 팀의 리더가 된다.

같은 시나리오를 세 진입점(교환·``sync_all``·``sync_user``)에 똑같이 돌린다. 사용자·팀은 진짜 ORM
객체다. 대신하는 것은 저장소(메모리)·토큰 검증·Cognito API·키 발급·``transfer_user``(팀만 옮긴다 —
실물도 역할을 건드리지 않는다)뿐이다.
"""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from structlog.testing import capture_logs

from app.core import oidc_identity
from app.core.config import get_settings
from app.core.oidc_identity import reconcile_role
from app.models.auth import Department, Organization, Team, User, UserRole
from app.services import cognito_sync_service, oidc_service
from app.services.cognito_sync_service import CognitoSyncService
from app.services.oidc_service import OIDCService
from tests.session_double import wire_savepoint

ADMIN_GROUP = "GatewayAdmin"


def _settings():
    """진짜 Settings 의 사본 — 이 파일이 모르는 설정을 코드가 읽어도 MagicMock 이 아니라 기본값이 나온다."""
    return get_settings().model_copy(
        update={
            "ADMIN_GROUPS": [ADMIN_GROUP],
            "ADMIN_EMAILS": [],
            "OIDC_REQUIRED_GROUP": "",
            "OIDC_GROUP_PREFIX": "Claude_",
            "OIDC_REJECT_UNMATCHED_GROUPS": True,
            "OIDC_USER_ID_CLAIM": "sub",
            "OIDC_EMAIL_CLAIM": "email",
            "OIDC_NAME_CLAIM": "name",
            "OIDC_GROUPS_CLAIM": "groups",
            "OIDC_PROVIDER_NAME": "cognito",
            "COGNITO_USER_POOL_ID": "pool-1",
            "COGNITO_SYNC_DEACTIVATE_MISSING": False,
        }
    )


class _Directory:
    """auth 스키마의 메모리판. 행은 진짜 ORM 객체이고, 서비스가 고친 값이 그대로 남는다."""

    def __init__(self, settings) -> None:
        self.org = Organization(id=uuid.uuid4(), name="Org")
        self.dept = Department(id=uuid.UUID(settings.DEFAULT_DEPT_ID), org_id=self.org.id, name="Default Department")
        self.teams: dict[uuid.UUID, Team] = {}
        self.users: list[User] = []
        self.calls: Counter[str] = Counter()
        self.default_team = self.team("Default", team_id=uuid.UUID(settings.DEFAULT_TEAM_ID))

    def team(self, name: str, *, team_id: uuid.UUID | None = None) -> Team:
        team = Team(id=team_id or uuid.uuid4(), dept_id=self.dept.id, name=name, leader_user_id=None)
        self.teams[team.id] = team
        return team

    def user(self, name: str, *, team: Team, role: UserRole) -> User:
        user = User(
            id=uuid.uuid4(),
            email=f"{name}@example.com",
            display_name=name,
            role=role,
            sso_subject=f"sub-{name}",
            team_id=team.id,
            is_active=True,
            provider="cognito",
        )
        self.users.append(user)
        return user

    def by_sub(self, sub: str) -> User | None:
        return next((u for u in self.users if u.sso_subject == sub), None)


class _Repo:
    """``UserRepository`` 의 메모리판. 로그인·동기화가 부르는 메서드만 있다 — 다른 걸 부르면 AttributeError."""

    def __init__(self, d: _Directory) -> None:
        self._d = d

    async def get_default_org(self):
        return self._d.org

    async def get_first_org_id(self):
        return self._d.org.id

    async def get_department(self, dept_id):
        return self._d.dept if dept_id == self._d.dept.id else None

    async def list_departments_lite(self):
        return [self._d.dept]

    async def get_team(self, team_id):
        self._d.calls["get_team"] += 1
        return self._d.teams.get(team_id)

    async def get_team_leader_id(self, team_id):
        team = self._d.teams.get(team_id)
        return team.leader_user_id if team is not None else None

    async def get_team_by_dept_and_name(self, dept_id, name):
        return next((t for t in self._d.teams.values() if (t.dept_id, t.name) == (dept_id, name)), None)

    async def create_team(self, team):
        self._d.teams[team.id] = team
        return team

    async def list_teams_lite(self):
        self._d.calls["list_teams_lite"] += 1
        return list(self._d.teams.values())

    async def list_all_teams(self):
        return list(self._d.teams.values())

    async def get_by_sso_subject(self, sub):
        return self._d.by_sub(sub)

    async def get_by_email(self, email):
        return next((u for u in self._d.users if u.email == email), None)

    async def create_user(self, user):
        self._d.users.append(user)
        return user

    async def flush(self):
        return None

    async def prefetch_users_by_subjects(self, subs):
        # 실물처럼 그 시점 값의 사본(스칼라 스냅샷)이다 — 뒤에 객체가 바뀌어도 스냅샷은 그대로다.
        return {
            u.sso_subject: {
                "id": u.id,
                "email": u.email,
                "display_name": u.display_name,
                "team_id": u.team_id,
                "role": u.role,
                "is_active": u.is_active,
            }
            for u in self._d.users
            if u.sso_subject in subs
        }


@pytest.fixture
def d(monkeypatch) -> _Directory:
    settings = _settings()
    for module in (oidc_service, oidc_identity, cognito_sync_service):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    directory = _Directory(settings)
    monkeypatch.setattr(oidc_service, "UserRepository", lambda session: _Repo(directory))
    monkeypatch.setattr(cognito_sync_service, "UserRepository", lambda session: _Repo(directory))
    budgets = MagicMock(upsert_config=AsyncMock(), get_first_active_config=AsyncMock(return_value=None))
    monkeypatch.setattr(oidc_service, "BudgetRepository", lambda session: budgets)
    return directory


# ─────────────────────────────────────────────────────────────────────────────
# 진입점 — "IdP 가 이 사람을 groups 로 본다" 를 한 번 탄다
# ─────────────────────────────────────────────────────────────────────────────


class _Verified:
    """토큰 검증은 이 파일의 관심사가 아니다(test_critical_console_sso_jwks.py) — 검증된 클레임만 준다."""

    def __init__(self, claims: dict) -> None:
        self._claims = claims

    async def verify_async(self, token, http_client):
        return self._claims


class _Transfers:
    """``UserTeamService.transfer_user`` 대역. 실물처럼 팀만 옮기고 역할은 건드리지 않는다."""

    def __init__(self, d: _Directory) -> None:
        self._d = d

    async def transfer_user(self, session, *, user_id, new_team_id, actor, ip_address="", request_id=""):
        next(u for u in self._d.users if u.id == user_id).team_id = new_team_id


def _attributes(who) -> list[dict]:
    return [
        {"Name": "sub", "Value": who.sso_subject},
        {"Name": "email", "Value": who.email},
        {"Name": "name", "Value": who.display_name},
    ]


def _cognito(d: _Directory, people: list[tuple]) -> tuple[CognitoSyncService, AsyncMock]:
    """풀에는 모든 팀의 그룹이 있고, 각 사람은 자기 groups 에만 속한다."""
    members = {
        who.sso_subject: {"Username": who.email, "Enabled": True, "Attributes": _attributes(who)} for who, _ in people
    }
    in_group: dict[str, list[dict]] = {}
    for who, groups in people:
        for g in groups:
            in_group.setdefault(g, []).append(members[who.sso_subject])
    names = sorted({*(f"Claude_{t.name}" for t in d.teams.values() if t is not d.default_team), *in_group})

    svc = CognitoSyncService(MagicMock())
    svc._list_all_groups = MagicMock(return_value=[{"GroupName": g} for g in names])
    svc._list_users_in_group = MagicMock(side_effect=lambda pool, g: in_group.get(g, []))
    svc._list_all_users = MagicMock(return_value=list(members.values()))
    session = AsyncMock()
    wire_savepoint(session)
    session.expunge_all = MagicMock()
    return svc, session


async def _exchange(d: _Directory, who, groups: list[str]) -> None:
    claims = {"sub": who.sso_subject, "email": who.email, "name": who.display_name, "groups": list(groups)}
    issued = SimpleNamespace(virtual_key="vk-test", expires_at=datetime.now(timezone.utc))
    svc = OIDCService(
        verifier=_Verified(claims),
        key_service=MagicMock(issue_key=AsyncMock(return_value=issued)),
        user_team_service=_Transfers(d),
        http_client=None,
    )
    await svc.exchange_jwt_for_vk(AsyncMock(), redis=None, token="idp-token", device_name="cli")


async def _sync_all(d: _Directory, who, groups: list[str]) -> None:
    svc, session = _cognito(d, [(who, groups)])
    result = await svc.sync_all(session)
    assert result.errors == []  # 동기화는 예외를 errors 에 삼킨다 — 여기서 드러내야 원인이 보인다


async def _sync_user(d: _Directory, who, groups: list[str]) -> None:
    svc = CognitoSyncService(MagicMock())
    svc._admin_get_user = MagicMock(
        return_value={"Username": who.email, "Enabled": True, "UserAttributes": _attributes(who)}
    )
    svc._list_groups_for_user = MagicMock(return_value=list(groups))
    result = await svc.sync_user(AsyncMock(), who.email)
    assert result.errors == []


@pytest.fixture(
    params=[
        pytest.param(_exchange, id="exchange"),
        pytest.param(_sync_all, id="sync_all"),
        pytest.param(_sync_user, id="sync_user"),
    ]
)
def sign_in(request, d):
    async def run(who, groups: list[str]) -> None:
        await request.param(d, who, groups)

    return run


# ─────────────────────────────────────────────────────────────────────────────
# 1. 규칙
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("derived", "leads_own_team", "stored"),
    [
        (UserRole.ADMIN, True, UserRole.ADMIN),
        (UserRole.ADMIN, False, UserRole.ADMIN),
        (UserRole.DEVELOPER, True, UserRole.TEAM_LEADER),
        (UserRole.DEVELOPER, False, UserRole.DEVELOPER),
        # derive_role 은 TEAM_LEADER 를 내지 않는다. 저장값이 잘못 넘어와도 리더 지정 없이는 남지 않는다.
        (UserRole.TEAM_LEADER, True, UserRole.TEAM_LEADER),
        (UserRole.TEAM_LEADER, False, UserRole.DEVELOPER),
    ],
)
def test_reconcile_role_truth_table(derived, leads_own_team, stored):
    assert reconcile_role(derived, leads_own_team=leads_own_team) == stored


# ─────────────────────────────────────────────────────────────────────────────
# 2. 세 진입점에 같은 시나리오
# ─────────────────────────────────────────────────────────────────────────────


async def test_leader_who_stays_in_their_team_keeps_the_role(d, sign_in):
    """이번 결함 — 예전엔 세 진입점 모두 DEVELOPER 로 되돌렸다."""
    a = d.team("A")
    lee = d.user("lee", team=a, role=UserRole.TEAM_LEADER)
    a.leader_user_id = lee.id
    await sign_in(lee, ["Claude_A"])
    assert (lee.role, lee.team_id) == (UserRole.TEAM_LEADER, a.id)


async def test_leader_moved_to_another_team_by_the_idp_is_a_developer_there(d, sign_in):
    """옮기기 **전** 팀(A)은 그를 가리키지만, 이번에 속하게 된 팀(B)은 아니다."""
    a, b = d.team("A"), d.team("B")
    lee = d.user("lee", team=a, role=UserRole.TEAM_LEADER)
    a.leader_user_id = lee.id
    await sign_in(lee, ["Claude_B"])
    assert (lee.role, lee.team_id) == (UserRole.DEVELOPER, b.id)


@pytest.mark.parametrize("designated", ["someone else", "nobody"])
async def test_stored_team_leader_without_the_teams_designation_is_a_developer(d, sign_in, designated):
    """리더가 바뀌었거나 지정이 풀렸다 — 저장된 TEAM_LEADER 만으로는 남지 않는다."""
    a = d.team("A")
    lee = d.user("lee", team=a, role=UserRole.TEAM_LEADER)
    kim = d.user("kim", team=a, role=UserRole.TEAM_LEADER)
    a.leader_user_id = kim.id if designated == "someone else" else None
    await sign_in(lee, ["Claude_A"])
    assert (lee.role, lee.team_id) == (UserRole.DEVELOPER, a.id)


async def test_admin_group_wins_and_the_leader_role_comes_back_without_it(d, sign_in):
    a = d.team("A")
    lee = d.user("lee", team=a, role=UserRole.TEAM_LEADER)
    a.leader_user_id = lee.id
    await sign_in(lee, ["Claude_A", ADMIN_GROUP])
    assert lee.role == UserRole.ADMIN
    # 관리자 그룹이 빠졌다. 팀의 리더 지정은 아직 그를 가리킨다.
    await sign_in(lee, ["Claude_A"])
    assert lee.role == UserRole.TEAM_LEADER


async def test_plain_developer_stays_a_developer(d, sign_in):
    a = d.team("A")
    kim = d.user("kim", team=a, role=UserRole.TEAM_LEADER)
    a.leader_user_id = kim.id
    park = d.user("park", team=a, role=UserRole.DEVELOPER)
    await sign_in(park, ["Claude_A"])
    assert (park.role, kim.role) == (UserRole.DEVELOPER, UserRole.TEAM_LEADER)


async def test_recreated_idp_account_of_a_leader_keeps_the_role(d, sign_in):
    """IdP 계정을 다시 만들면 sub 가 바뀐다 — 이메일로 같은 행을 찾고, 그 행의 id 로 리더 지정을 본다."""
    a = d.team("A")
    lee = d.user("lee", team=a, role=UserRole.TEAM_LEADER)
    a.leader_user_id = lee.id
    await sign_in(SimpleNamespace(sso_subject="sub-lee-recreated", email=lee.email, display_name="lee"), ["Claude_A"])
    assert (lee.sso_subject, lee.role) == ("sub-lee-recreated", UserRole.TEAM_LEADER)


@pytest.mark.parametrize(
    ("groups", "role"),
    [
        pytest.param(["Claude_A"], UserRole.DEVELOPER, id="developer"),
        pytest.param(["Claude_A", ADMIN_GROUP], UserRole.ADMIN, id="admin group"),
    ],
)
async def test_new_user_is_created_as_before(d, sign_in, groups, role):
    a = d.team("A")
    await sign_in(SimpleNamespace(sso_subject="sub-new", email="new@example.com", display_name="new"), groups)
    created = d.by_sub("sub-new")
    assert (created.role, created.team_id) == (role, a.id)


# ─────────────────────────────────────────────────────────────────────────────
# 3. sync_all 의 "안 바뀌었으면 건너뛴다" 게이트
# ─────────────────────────────────────────────────────────────────────────────


async def test_sync_all_skips_an_unchanged_leader_and_rewrites_only_what_changed(d):
    """게이트는 저장될 역할(reconcile_role)과 비교한다. derive_role 값과 비교하면 리더는 매번 '바뀜' 이다.

    대조군이 같은 동기화 안에 있다 — B 의 리더 지정이 choi 에게 넘어간 kim 은 게이트를 지나 강등된다(이게
    없으면 "upsert 를 아예 안 불러서 통과" 와 구분되지 않는다).
    """
    a, b = d.team("A"), d.team("B")
    lee = d.user("lee", team=a, role=UserRole.TEAM_LEADER)
    choi = d.user("choi", team=b, role=UserRole.TEAM_LEADER)
    kim = d.user("kim", team=b, role=UserRole.TEAM_LEADER)  # B 의 예전 리더
    park = d.user("park", team=a, role=UserRole.DEVELOPER)
    a.leader_user_id, b.leader_user_id = lee.id, choi.id

    svc, session = _cognito(
        d, [(lee, ["Claude_A"]), (choi, ["Claude_B"]), (kim, ["Claude_B"]), (park, ["Claude_A"])]
    )
    upsert = AsyncMock(wraps=svc._upsert_one_user)
    svc._upsert_one_user = upsert
    result = await svc.sync_all(session)

    assert result.errors == []
    assert [c.kwargs["sub"] for c in upsert.await_args_list] == ["sub-kim"]
    assert result.users_updated == 1
    assert [u.role for u in (lee, choi, kim, park)] == [
        UserRole.TEAM_LEADER,
        UserRole.TEAM_LEADER,
        UserRole.DEVELOPER,
        UserRole.DEVELOPER,
    ]
    # 리더 지정은 동기화마다 한 번(팀 캐시를 만드는 조회)에서 읽는다 — 사용자마다 팀을 다시 읽지 않는다.
    assert (d.calls["list_teams_lite"], d.calls["get_team"]) == (1, 0)


# ─────────────────────────────────────────────────────────────────────────────
# 4. 그 밖의 경로
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("designated", "role"), [("lee", UserRole.TEAM_LEADER), ("nobody", UserRole.DEVELOPER)])
async def test_sync_group_follows_the_same_rule(d, designated, role):
    """``sync_group`` 도 같은 upsert 를 탄다 — 그 그룹의 팀이 가리키는 사람만 TEAM_LEADER 다."""
    a = d.team("A")
    lee = d.user("lee", team=a, role=UserRole.TEAM_LEADER)
    a.leader_user_id = lee.id if designated == "lee" else None
    svc, session = _cognito(d, [(lee, ["Claude_A"])])
    result = await svc.sync_group(session, "Claude_A")
    assert result.errors == []
    assert (lee.role, lee.team_id) == (role, a.id)


def _role_changes(logs: list[dict]) -> list[tuple[str, str]]:
    return [(e["old_role"], e["new_role"]) for e in logs if e["event"] == "oidc.user_role_changed"]


async def test_exchange_logs_a_role_change_only_when_the_stored_role_changes(d):
    """예전엔 리더가 교환할 때마다 TEAM_LEADER → DEVELOPER 가 찍혔다. 이제는 실제로 바뀔 때만 찍힌다."""
    a, b = d.team("A"), d.team("B")
    lee = d.user("lee", team=a, role=UserRole.TEAM_LEADER)
    a.leader_user_id = lee.id
    with capture_logs() as stayed:
        await _exchange(d, lee, ["Claude_A"])
    with capture_logs() as moved:
        await _exchange(d, lee, ["Claude_B"])
    assert _role_changes(stayed) == []
    assert _role_changes(moved) == [("TEAM_LEADER", "DEVELOPER")]
    assert lee.team_id == b.id
