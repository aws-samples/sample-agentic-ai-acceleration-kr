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

불변식은 하나다 — **저장된 역할이 TEAM_LEADER 이면 그 사람이 속한 팀이 그를 리더로 가리킨다.** 그래서 교환의
팀 이동이 실패한 경우와 리더 지정(``set_team_leader``: 구성원만, 예전 리더는 바로 DEVELOPER)도 여기서 본다.
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
from app.core.auth import CurrentUser
from app.core.config import get_settings
from app.core.exceptions import NotFoundError, ValidationError
from app.core.oidc_identity import reconcile_role
from app.models.auth import Department, Organization, Team, User, UserRole
from app.services import cognito_sync_service, oidc_service, user_team_service
from app.services.cognito_sync_service import CognitoSyncService
from app.services.oidc_service import OIDCService
from app.services.user_team_service import UserTeamService
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
        team = Team(
            id=team_id or uuid.uuid4(),
            dept_id=self.dept.id,
            name=name,
            leader_user_id=None,
            created_at=datetime.now(timezone.utc),  # 읽어 온 행처럼(서버 기본값) — TeamResponse 가 요구한다
        )
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

    async def set_leader(self, team_id, user_id):
        team = self._d.teams.get(team_id)
        if team is not None:
            team.leader_user_id = user_id
        return team

    async def get_user(self, user_id):
        return next((u for u in self._d.users if u.id == user_id), None)

    async def update_user_role(self, user_id, role):
        user = await self.get_user(user_id)
        if user is not None:
            user.role = role
        return user

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


async def _exchange(d: _Directory, who, groups: list[str], *, transfers=None):
    claims = {"sub": who.sso_subject, "email": who.email, "name": who.display_name, "groups": list(groups)}
    issued = SimpleNamespace(virtual_key="vk-test", expires_at=datetime.now(timezone.utc))
    svc = OIDCService(
        verifier=_Verified(claims),
        key_service=MagicMock(issue_key=AsyncMock(return_value=issued)),
        user_team_service=transfers or _Transfers(d),
        http_client=None,
    )
    return await svc.exchange_jwt_for_vk(AsyncMock(), redis=None, token="idp-token", device_name="cli")


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


# ─────────────────────────────────────────────────────────────────────────────
# 5. 교환 — 팀 이동(transfer_user)이 실패했을 때
# ─────────────────────────────────────────────────────────────────────────────


class _FailingTransfers:
    """``transfer_user`` 가 실패한다. ``after_move`` 면 팀을 옮긴 **뒤에** 실패한다(캐시·감사 단계의 실패 형상)."""

    def __init__(self, d: _Directory, *, after_move: bool) -> None:
        self._d = d
        self._after_move = after_move

    async def transfer_user(self, session, *, user_id, new_team_id, actor, ip_address="", request_id=""):
        if self._after_move:
            next(u for u in self._d.users if u.id == user_id).team_id = new_team_id
        raise RuntimeError("transfer_user failed")


async def test_failed_team_move_keeps_the_old_teams_leader_a_leader(d):
    """역할은 옮겨 갈 팀(B) 기준으로 먼저 정해진다. 옮기지 못했으면 남은 팀(A — 그를 가리킨다) 기준이어야 한다."""
    a = d.team("A")
    d.team("B")  # 옮겨 갈 팀. 이동은 실패한다.
    lee = d.user("lee", team=a, role=UserRole.TEAM_LEADER)
    a.leader_user_id = lee.id
    issued = await _exchange(d, lee, ["Claude_B"], transfers=_FailingTransfers(d, after_move=False))
    assert (lee.team_id, lee.role) == (a.id, UserRole.TEAM_LEADER)
    assert (issued.virtual_key, issued.team_id) == ("vk-test", str(a.id))  # 로그인은 그래도 된다


async def test_failed_team_move_does_not_make_the_destinations_designee_a_leader_in_the_old_team(d):
    """B 가 kim 을 리더로 가리키지만 kim 은 아직 A 에 있다(관리자가 옮긴 예전 리더 등) — 인가는 그를 A 의
    리더로 읽으므로 TEAM_LEADER 가 되면 안 된다."""
    a, b = d.team("A"), d.team("B")
    kim = d.user("kim", team=a, role=UserRole.DEVELOPER)
    b.leader_user_id = kim.id
    issued = await _exchange(d, kim, ["Claude_B"], transfers=_FailingTransfers(d, after_move=False))
    assert (kim.team_id, kim.role) == (a.id, UserRole.DEVELOPER)
    assert issued.virtual_key == "vk-test"


async def test_failure_after_the_move_keeps_the_role_for_the_team_the_user_ended_up_in(d):
    """옮긴 뒤에 실패했다면 남은 팀은 B 다. 옛 팀(A 는 그를 가리킨다) 기준으로 되돌리면 안 된다."""
    a, b = d.team("A"), d.team("B")
    lee = d.user("lee", team=a, role=UserRole.TEAM_LEADER)
    a.leader_user_id = lee.id
    issued = await _exchange(d, lee, ["Claude_B"], transfers=_FailingTransfers(d, after_move=True))
    assert (lee.team_id, lee.role) == (b.id, UserRole.DEVELOPER)
    assert issued.virtual_key == "vk-test"


# ─────────────────────────────────────────────────────────────────────────────
# 6. 리더 지정 — PUT /admin/teams/{id}/leader → set_team_leader
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def designate(d, monkeypatch):
    """``set_team_leader`` 를 메모리 저장소로 돌린다. 감사 로그는 호출만 기록한다."""
    audit = MagicMock(log=AsyncMock())
    monkeypatch.setattr(user_team_service, "UserRepository", lambda session: _Repo(d))
    monkeypatch.setattr(user_team_service, "audit_logger", audit)
    svc = UserTeamService(cache_mgr=MagicMock(), key_service=MagicMock())
    admin = CurrentUser(user_id=uuid.uuid4(), email="admin@example.com", role=UserRole.ADMIN, team_id=None)

    async def run(team: Team, user_id: uuid.UUID):
        return await svc.set_team_leader(AsyncMock(), team_id=team.id, user_id=user_id, actor=admin)

    return run, audit


def _state(d: _Directory) -> tuple:
    return (
        {t.id: t.leader_user_id for t in d.teams.values()},
        {u.id: (u.role, u.team_id, u.is_active) for u in d.users},
    )


async def test_designating_a_member_promotes_them(d, designate):
    run, audit = designate
    a = d.team("A")
    park = d.user("park", team=a, role=UserRole.DEVELOPER)
    resp = await run(a, park.id)
    assert (a.leader_user_id, park.role, resp.leader_user_id) == (park.id, UserRole.TEAM_LEADER, str(park.id))
    audit.log.assert_awaited_once()
    assert audit.log.await_args.kwargs["changes"] == {
        "before": {"leader_user_id": None},
        "after": {"leader_user_id": str(park.id)},
    }


async def test_replacing_the_leader_demotes_the_previous_one_at_once(d, designate):
    """예전엔 예전 리더가 다음 CLI 로그인·동기화까지 — 콘솔만 쓰면 끝까지 — TEAM_LEADER 로 남았다."""
    run, audit = designate
    a = d.team("A")
    lee = d.user("lee", team=a, role=UserRole.TEAM_LEADER)
    park = d.user("park", team=a, role=UserRole.DEVELOPER)
    a.leader_user_id = lee.id
    await run(a, park.id)
    assert (a.leader_user_id, lee.role, park.role) == (park.id, UserRole.DEVELOPER, UserRole.TEAM_LEADER)
    assert audit.log.await_args.kwargs["changes"] == {
        "before": {"leader_user_id": str(lee.id), "previous_leader_role": "TEAM_LEADER"},
        "after": {"leader_user_id": str(park.id), "previous_leader_role": "DEVELOPER"},
    }


async def test_previous_leader_who_is_an_admin_stays_admin(d, designate):
    run, audit = designate
    a = d.team("A")
    lee = d.user("lee", team=a, role=UserRole.ADMIN)
    park = d.user("park", team=a, role=UserRole.DEVELOPER)
    a.leader_user_id = lee.id
    await run(a, park.id)
    assert (lee.role, park.role) == (UserRole.ADMIN, UserRole.TEAM_LEADER)
    assert audit.log.await_args.kwargs["changes"]["before"] == {"leader_user_id": str(lee.id)}


async def test_designating_an_admin_keeps_them_admin(d, designate):
    """지정은 남기되 역할은 낮추지 않는다. IdP 에서 관리자가 아니게 되면 그때 TEAM_LEADER 가 된다(reconcile_role)."""
    run, _ = designate
    a = d.team("A")
    lee = d.user("lee", team=a, role=UserRole.ADMIN)
    await run(a, lee.id)
    assert (lee.role, a.leader_user_id) == (UserRole.ADMIN, lee.id)
    assert reconcile_role(UserRole.DEVELOPER, leads_own_team=a.leader_user_id == lee.id) == UserRole.TEAM_LEADER


async def test_previous_leader_who_has_moved_to_another_team_is_left_alone(d, designate):
    """lee 는 이제 B 에 있고 B 가 그를 가리킨다. A 에 남은 옛 지정만 보고 강등하면 B 의 리더를 잃는다."""
    run, _ = designate
    a, b = d.team("A"), d.team("B")
    lee = d.user("lee", team=b, role=UserRole.TEAM_LEADER)
    a.leader_user_id = b.leader_user_id = lee.id
    park = d.user("park", team=a, role=UserRole.DEVELOPER)
    await run(a, park.id)
    assert (lee.role, b.leader_user_id, a.leader_user_id) == (UserRole.TEAM_LEADER, lee.id, park.id)


async def test_designating_the_current_leader_again_changes_no_roles(d, designate):
    run, audit = designate
    a = d.team("A")
    lee = d.user("lee", team=a, role=UserRole.TEAM_LEADER)
    park = d.user("park", team=a, role=UserRole.DEVELOPER)
    a.leader_user_id = lee.id
    await run(a, lee.id)
    assert (a.leader_user_id, lee.role, park.role) == (lee.id, UserRole.TEAM_LEADER, UserRole.DEVELOPER)
    assert audit.log.await_args.kwargs["changes"] == {
        "before": {"leader_user_id": str(lee.id)},
        "after": {"leader_user_id": str(lee.id)},
    }


async def _refused(d: _Directory, designate, team: Team, user_id: uuid.UUID) -> Exception:
    """거부된 지정은 아무것도 쓰지 않는다 — 리더 지정도, 누구의 역할도. 감사 로그도 남기지 않는다."""
    run, audit = designate
    before = _state(d)
    with pytest.raises((ValidationError, NotFoundError)) as refused:
        await run(team, user_id)
    assert _state(d) == before
    audit.log.assert_not_awaited()
    return refused.value


async def test_a_member_of_another_team_is_refused(d, designate):
    """예전엔 받아서 kim 이 TEAM_LEADER 가 됐고, 인가는 그를 A 가 아니라 **B** 의 리더로 읽었다."""
    a, b = d.team("A"), d.team("B")
    lee = d.user("lee", team=a, role=UserRole.TEAM_LEADER)
    a.leader_user_id = lee.id
    kim = d.user("kim", team=b, role=UserRole.DEVELOPER)
    err = await _refused(d, designate, a, kim.id)
    assert (type(err), err.message) == (ValidationError, f"User {kim.id} is not a member of team {a.id}")


async def test_an_unknown_user_is_refused(d, designate):
    a = d.team("A")
    lee = d.user("lee", team=a, role=UserRole.TEAM_LEADER)
    a.leader_user_id = lee.id
    ghost = uuid.uuid4()
    err = await _refused(d, designate, a, ghost)
    assert (type(err), err.message) == (NotFoundError, f"User not found: {ghost}")


async def test_a_deactivated_member_is_refused(d, designate):
    a = d.team("A")
    lee = d.user("lee", team=a, role=UserRole.TEAM_LEADER)
    a.leader_user_id = lee.id
    park = d.user("park", team=a, role=UserRole.DEVELOPER)
    park.is_active = False
    err = await _refused(d, designate, a, park.id)
    assert (type(err), err.message) == (ValidationError, f"User {park.id} is deactivated")
