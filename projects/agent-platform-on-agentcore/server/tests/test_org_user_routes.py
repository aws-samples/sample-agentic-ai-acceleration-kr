"""People on the Organization page: invite, role, teams, status — with the locks that keep an admin in."""
import inspect
import os
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routes.org as org  # noqa: E402
from core.auth import AuthUser, require_admin  # noqa: E402
from services.audit_service import AuditService  # noqa: E402
from services.org_directory_service import InviteFailed, InviteHalfCreated, NotCognitoUser, OrgUser  # noqa: E402
from services.team_service import TeamService  # noqa: E402
from team_table_stub import seed  # noqa: E402

ADMIN = AuthUser(sub="s-a", username="alice", email="alice@example.com", groups=["admin"])
ROLES = {"finance": "arn:aws:iam::1:role/f"}


class Table:
    def __init__(self):
        self.rows, self.audit = {}, []

    def list_teams(self):
        return [dict(r) for r in self.rows.values()]

    def get_team(self, name):
        return dict(self.rows[name]) if name in self.rows else None

    def put_team(self, item):
        self.rows[item["name"]] = dict(item)

    def delete_team(self, name):
        self.rows.pop(name, None)

    def append_audit(self, e):
        self.audit.append(e)

    def list_audit(self, m, s, e):
        return list(self.audit)


class Directory:
    def __init__(self, users):
        self.users = {u.sub: u for u in users}
        self.calls = []

    def list_users(self, refresh=False):
        self.calls.append(("list", refresh))
        return list(self.users.values())

    def get_user(self, sub):
        return self.users.get(sub)

    def team_groups(self):
        return ["finance", "hr"]

    def invite(self, email, teams, role, send_email=True):
        self.calls.append(("invite", email, tuple(teams), role, send_email))
        if email == "dup@example.com":
            raise InviteFailed("이미 등록된 사용자입니다.")
        u = OrgUser(sub=f"new-{email}", username=email, email=email, provider="cognito", role=role, teams=list(teams))
        self.users[u.sub] = u
        return u, (None if send_email else "Temp1234!abcd")

    def _only_cognito(self, u):
        if u.provider != "cognito":
            raise NotCognitoUser("Entra 사용자는 Entra ID 에서 바꾸세요.")

    def set_role(self, u, role):
        self._only_cognito(u)
        self.calls.append(("role", u.sub, role))
        self.users[u.sub] = u.model_copy(update={"role": role})

    def set_teams(self, u, teams):
        self._only_cognito(u)
        self.calls.append(("teams", u.sub, tuple(teams)))

    def set_enabled(self, u, enabled):
        self._only_cognito(u)
        self.calls.append(("enabled", u.sub, enabled))

    def reset_password(self, u):
        self._only_cognito(u)
        self.calls.append(("reset", u.sub))

    def delete(self, u):
        self._only_cognito(u)
        self.calls.append(("delete", u.sub))
        del self.users[u.sub]


class Usage:
    configured = True

    def begin_read(self):
        pass

    def user_totals(self, s, e):
        return {"s-b": {"turns": 4}}

    def user_costs(self, s, e):
        return {"s-b": {"model_cost_micros": 900}}


PEOPLE = [
    OrgUser(sub="s-a", username="s-a", email="alice@example.com", provider="cognito", role="admin"),
    OrgUser(sub="s-b", username="s-b", email="bob@example.com", provider="cognito", role="user", teams=["finance"], status="CONFIRMED"),
    OrgUser(sub="s-c", username="s-c", email="carol@example.com", provider="cognito", role="user", enabled=False, status="CONFIRMED"),
    OrgUser(sub="e-1", username="dan@corp", email="dan@corp.example", provider="entra", role="user", teams=["hr"], last_login="2026-10-09T00:00:00+00:00"),
]


def setup_function():
    table = Table()
    org._teams_override = TeamService(seed(table, ROLES))
    org._directory_override = Directory(list(PEOPLE))
    org._audit_override = AuditService(table)
    org._usage_override = Usage()
    org._table = table


def teardown_function():
    for name in ("_teams_override", "_directory_override", "_audit_override", "_usage_override"):
        setattr(org, name, None)


def test_admin_only_on_every_user_route():
    for fn in (org.list_users, org.invite_users, org.get_user, org.update_user, org.reset_user_password, org.delete_user):
        deps = [p.default.dependency for p in inspect.signature(fn).parameters.values() if hasattr(p.default, "dependency")]
        assert require_admin in deps, fn.__name__


def test_list_filters_and_joins_usage():
    out = org.list_users(q="", team="", role="", provider="", status="", refresh=False, user=ADMIN)
    rows = {r["sub"]: r for r in out["users"]}
    assert rows["s-b"]["turns_30d"] == 4 and rows["s-b"]["model_cost_micros_30d"] == 900 and rows["s-a"]["turns_30d"] == 0
    assert [r["sub"] for r in org.list_users(q="bob", team="", role="", provider="", status="", refresh=False, user=ADMIN)["users"]] == ["s-b"]
    assert [r["sub"] for r in org.list_users(q="", team="hr", role="", provider="", status="", refresh=False, user=ADMIN)["users"]] == ["e-1"]
    assert [r["sub"] for r in org.list_users(q="", team="", role="admin", provider="", status="", refresh=False, user=ADMIN)["users"]] == ["s-a"]
    assert [r["sub"] for r in org.list_users(q="", team="", role="", provider="entra", status="", refresh=False, user=ADMIN)["users"]] == ["e-1"]
    assert [r["sub"] for r in org.list_users(q="", team="", role="", provider="", status="disabled", refresh=False, user=ADMIN)["users"]] == ["s-c"]
    org.list_users(q="", team="", role="", provider="", status="", refresh=True, user=ADMIN)
    assert ("list", True) in org._directory_override.calls


def test_invite_many_reports_each_and_returns_temp_password_only_when_suppressed():
    out = org.invite_users(org.InviteRequest(emails=["erin@example.com", "dup@example.com"], teams=["hr"], role="user"), user=ADMIN)
    assert [i["email"] for i in out["invited"]] == ["erin@example.com"] and "temporary_password" not in out["invited"][0]
    assert out["failed"] == [{"email": "dup@example.com", "detail": "이미 등록된 사용자입니다."}]
    out = org.invite_users(org.InviteRequest(emails=["frank@example.com"], send_email=False), user=ADMIN)
    assert out["invited"][0]["temporary_password"] == "Temp1234!abcd"
    audit = org._table.audit
    assert [e["action"] for e in audit] == ["user.invite", "user.invite", "user.invite"]
    assert all("temporary_password" not in (e["after"] or {}) for e in audit)
    assert audit[1]["result"] == "error"


def test_invite_rejects_unknown_team_and_bad_role_before_calling():
    d = org._directory_override
    with pytest.raises(HTTPException) as bad_team:
        org.invite_users(org.InviteRequest(emails=["x@example.com"], teams=["ghost"]), user=ADMIN)
    assert bad_team.value.status_code == 400
    with pytest.raises(HTTPException) as bad_role:
        org.invite_users(org.InviteRequest(emails=["x@example.com"], role="owner"), user=ADMIN)
    assert bad_role.value.status_code == 400
    assert not any(c[0] == "invite" for c in d.calls)


def test_update_role_teams_enabled_and_audits_each():
    d = org._directory_override
    out = org.update_user("s-b", org.UserUpdate(role="admin", teams=["hr"], enabled=False), user=ADMIN)
    assert ("role", "s-b", "admin") in d.calls and ("teams", "s-b", ("hr",)) in d.calls and ("enabled", "s-b", False) in d.calls
    assert out["sub"] == "s-b"
    assert [e["action"] for e in org._table.audit] == ["user.update_role", "user.update_teams", "user.disable"]


def test_self_demotion_is_always_400():
    # Spec §5.1: refused whether or not another admin exists (final review I6).
    d = org._directory_override
    with pytest.raises(HTTPException) as caught:
        org.update_user("s-a", org.UserUpdate(role="user"), user=ADMIN)
    assert caught.value.status_code == 400 and "자기 자신" in caught.value.detail
    d.users["s-b"] = PEOPLE[1].model_copy(update={"role": "admin"})
    with pytest.raises(HTTPException) as caught:
        org.update_user("s-a", org.UserUpdate(role="user"), user=ADMIN)
    assert caught.value.status_code == 400 and "자기 자신" in caught.value.detail
    assert not any(c[0] == "role" for c in d.calls)


def test_self_cannot_be_disabled_or_deleted():
    for op in (lambda: org.update_user("s-a", org.UserUpdate(enabled=False), user=ADMIN),
               lambda: org.delete_user("s-a", user=ADMIN)):
        with pytest.raises(HTTPException) as caught:
            op()
        assert caught.value.status_code == 400


def test_demoting_one_of_two_admins_is_fine():
    org._directory_override.users["s-b"] = PEOPLE[1].model_copy(update={"role": "admin"})
    org.update_user("s-b", org.UserUpdate(role="user"), user=ADMIN)
    assert ("role", "s-b", "user") in org._directory_override.calls


def test_entra_user_write_is_400_without_cognito_call():
    d = org._directory_override
    before = len(d.calls)
    for op in (lambda: org.update_user("e-1", org.UserUpdate(teams=["finance"]), user=ADMIN),
               lambda: org.reset_user_password("e-1", user=ADMIN),
               lambda: org.delete_user("e-1", user=ADMIN)):
        with pytest.raises(HTTPException) as caught:
            op()
        assert caught.value.status_code == 400 and "Entra" in caught.value.detail
    assert len(d.calls) == before


def test_unknown_team_in_update_is_400():
    with pytest.raises(HTTPException) as caught:
        org.update_user("s-b", org.UserUpdate(teams=["ghost"]), user=ADMIN)
    assert caught.value.status_code == 400


def test_reset_and_delete_audit_and_404():
    org.reset_user_password("s-b", user=ADMIN)
    org.delete_user("s-c", user=ADMIN)
    assert [e["action"] for e in org._table.audit] == ["user.reset_password", "user.delete"]
    with pytest.raises(HTTPException) as caught:
        org.get_user("ghost", user=ADMIN)
    assert caught.value.status_code == 404


def test_half_created_invite_is_reported_as_failed_and_audited():
    def boom(email, teams, role, send_email=True):
        raise InviteHalfCreated("사용자는 만들어졌지만 finance 그룹에 넣지 못했습니다.")

    org._directory_override.invite = boom
    out = org.invite_users(org.InviteRequest(emails=["gina@example.com"], teams=["finance"]), user=ADMIN)
    assert out["invited"] == [] and "finance" in out["failed"][0]["detail"]
    assert org._table.audit[0]["result"] == "error"
