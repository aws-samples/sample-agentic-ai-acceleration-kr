"""Who is in the org: the pool live, Entra as of last login; writes go to Cognito only."""
import os
import sys

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.org_directory_service import NotCognitoUser, OrgDirectoryService  # noqa: E402


def err(code):
    return ClientError({"Error": {"Code": code, "Message": code}}, "op")


class Cognito:
    """Users keyed by Username (= sub in an email-alias pool); groups → set of usernames."""

    def __init__(self, users=None, groups=None, page_size=60):
        self.users = dict(users or {})
        self.groups = {g: set(m) for g, m in (groups or {}).items()}
        self.page_size = page_size
        self.calls = []
        self.n = 0

    def list_users(self, UserPoolId, Limit, PaginationToken=None):
        self.calls.append("list_users")
        items = list(self.users.items())
        start = int(PaginationToken or 0)
        page = items[start:start + self.page_size]
        out = {"Users": [self._user(u, a) for u, a in page]}
        if start + self.page_size < len(items):
            out["PaginationToken"] = str(start + self.page_size)
        return out

    def _user(self, username, a):
        return {"Username": username, "UserStatus": a.get("status", "CONFIRMED"), "Enabled": a.get("enabled", True),
                "UserCreateDate": a.get("created", "2026-10-01T00:00:00+00:00"),
                "Attributes": [{"Name": "sub", "Value": username}, {"Name": "email", "Value": a["email"]}]}

    def list_groups(self, UserPoolId, Limit=60, NextToken=None):
        self.calls.append("list_groups")
        return {"Groups": [{"GroupName": g} for g in sorted(self.groups)]}

    def list_users_in_group(self, UserPoolId, GroupName, Limit=60, NextToken=None):
        self.calls.append(("list_users_in_group", GroupName))
        members = sorted(self.groups.get(GroupName, ()))
        return {"Users": [self._user(u, self.users[u]) for u in members if u in self.users]}

    def admin_create_user(self, UserPoolId, Username, UserAttributes, **kw):
        self.calls.append(("admin_create_user", Username, kw.get("MessageAction"), kw.get("TemporaryPassword")))
        if Username in self.users or any(a["email"] == Username for a in self.users.values()):
            raise err("UsernameExistsException")
        self.n += 1
        sub = f"sub-new-{self.n}"
        self.users[sub] = {"email": Username, "status": "FORCE_CHANGE_PASSWORD"}
        return {"User": self._user(sub, self.users[sub])}

    def get_group(self, UserPoolId, GroupName):
        self.calls.append(("get_group", GroupName))
        if GroupName not in self.groups:
            raise err("ResourceNotFoundException")
        return {"Group": {"GroupName": GroupName}}

    def admin_add_user_to_group(self, UserPoolId, Username, GroupName):
        self.calls.append(("add", Username, GroupName))
        if GroupName not in self.groups:
            raise err("ResourceNotFoundException")
        self.groups[GroupName].add(Username)

    def admin_remove_user_from_group(self, UserPoolId, Username, GroupName):
        self.calls.append(("remove", Username, GroupName))
        self.groups.get(GroupName, set()).discard(Username)

    def admin_disable_user(self, UserPoolId, Username):
        self.calls.append(("disable", Username))
        self.users[Username]["enabled"] = False

    def admin_enable_user(self, UserPoolId, Username):
        self.calls.append(("enable", Username))
        self.users[Username]["enabled"] = True

    def admin_reset_user_password(self, UserPoolId, Username):
        self.calls.append(("reset", Username))

    def admin_delete_user(self, UserPoolId, Username):
        self.calls.append(("delete", Username))
        del self.users[Username]


class Users:
    """The OIDC users table: rows written on login."""

    def __init__(self, rows):
        self.rows = rows

    def list_all(self):
        return list(self.rows)


POOL = {
    "sub-a": {"email": "alice@example.com"},
    "sub-b": {"email": "bob@example.com", "status": "FORCE_CHANGE_PASSWORD"},
    "sub-c": {"email": "carol@example.com", "enabled": False},
}
GROUPS = {"admin": ["sub-a"], "user": ["sub-a", "sub-b", "sub-c"], "team:finance": ["sub-b"], "team:hr": ["sub-b", "sub-c"], "team:ops": []}
ENTRA = [{"sub": "e-1", "provider": "entra", "email": "dan@corp.example", "display_name": "Dan",
          "groups": ["g-hr", "g-x"], "role": "user", "last_login": "2026-10-09T10:00:00+00:00",
          "created_at": "2026-09-01T00:00:00+00:00"}]


def service(cognito=None, users=None, now=None):
    now = now or [0.0]
    return OrgDirectoryService(cognito or Cognito(POOL, GROUPS), "pool-1", users=users,
                               entra_teams=lambda gids: ["hr"] if "g-hr" in gids else [],
                               cache_seconds=60, clock=lambda: now[0]), now


def test_list_users_merges_pool_groups_and_entra_snapshot():
    svc, _ = service(users=Users(ENTRA))
    users = {u.sub: u for u in svc.list_users()}
    assert users["sub-a"].role == "admin" and users["sub-a"].teams == []
    assert users["sub-b"].teams == ["finance", "hr"] and users["sub-b"].status == "FORCE_CHANGE_PASSWORD"
    assert users["sub-c"].enabled is False
    assert users["e-1"].provider == "entra" and users["e-1"].teams == ["hr"] and users["e-1"].name == "Dan"
    assert users["e-1"].last_login.startswith("2026-10-09")
    assert all(u.provider == "cognito" for s, u in users.items() if s.startswith("sub-"))


def test_pool_is_read_once_per_cache_window_and_refresh_forces():
    cognito = Cognito(POOL, GROUPS)
    svc, now = service(cognito)
    svc.list_users()
    svc.list_users()
    assert cognito.calls.count("list_users") == 1
    svc.list_users(refresh=True)
    assert cognito.calls.count("list_users") == 2
    now[0] += 61
    svc.list_users()
    assert cognito.calls.count("list_users") == 3


def test_team_groups_and_live_members():
    cognito = Cognito(POOL, GROUPS)
    svc, _ = service(cognito)
    assert svc.team_groups() == ["finance", "hr", "ops"]
    members = svc.group_members("hr")
    assert [m.sub for m in members] == ["sub-b", "sub-c"]
    assert ("list_users_in_group", "team:hr") in cognito.calls


def test_team_sizes_count_from_the_snapshot_without_a_call_per_team():
    cognito = Cognito(POOL, GROUPS)
    svc, _ = service(cognito, users=Users(ENTRA))
    svc.list_users()
    before = len(cognito.calls)
    # Entra's dan is in hr through the group mapping, as the members tab lists them.
    assert svc.team_sizes() == {"finance": 1, "hr": 3}
    assert len(cognito.calls) == before


def test_team_sizes_follow_a_write_at_once():
    cognito = Cognito(POOL, GROUPS)
    svc, _ = service(cognito)
    assert svc.team_sizes()["hr"] == 2
    svc.add_to_team(svc.get_user("sub-a"), "hr")
    assert svc.team_sizes()["hr"] == 3


def test_invite_creates_user_and_adds_groups_with_email_by_default():
    cognito = Cognito(POOL, GROUPS)
    svc, _ = service(cognito)
    user, temp = svc.invite("erin@example.com", teams=["hr"], role="user")
    assert temp is None
    assert ("admin_create_user", "erin@example.com", None, None) in cognito.calls
    assert ("add", user.username, "user") in cognito.calls and ("add", user.username, "team:hr") in cognito.calls
    assert user.email == "erin@example.com" and user.teams == ["hr"] and user.role == "user"


def test_invite_without_email_returns_a_one_time_temporary_password():
    cognito = Cognito(POOL, GROUPS)
    svc, _ = service(cognito)
    user, temp = svc.invite("frank@example.com", teams=[], role="admin", send_email=False)
    assert temp and len(temp) >= 12 and any(c.isdigit() for c in temp) and any(c.isupper() for c in temp)
    call = next(c for c in cognito.calls if c[0] == "admin_create_user")
    assert call[2] == "SUPPRESS" and call[3] == temp
    assert ("add", user.username, "admin") in cognito.calls


def test_invite_duplicate_is_value_error():
    svc, _ = service()
    with pytest.raises(ValueError):
        svc.invite("alice@example.com", teams=[], role="user")


def test_set_teams_adds_and_removes_only_the_difference():
    cognito = Cognito(POOL, GROUPS)
    svc, _ = service(cognito)
    bob = svc.get_user("sub-b")
    svc.set_teams(bob, ["hr", "ops"])
    assert ("remove", "sub-b", "team:finance") in cognito.calls
    assert ("add", "sub-b", "team:ops") in cognito.calls
    assert not any(c == ("add", "sub-b", "team:hr") for c in cognito.calls)


def test_role_enabled_reset_delete_go_to_cognito_and_invalidate_cache():
    cognito = Cognito(POOL, GROUPS)
    svc, _ = service(cognito)
    bob = svc.get_user("sub-b")
    svc.set_role(bob, "admin")
    assert ("add", "sub-b", "admin") in cognito.calls
    svc.set_role(bob, "user")
    assert ("remove", "sub-b", "admin") in cognito.calls
    svc.set_enabled(bob, False)
    assert svc.get_user("sub-b").enabled is False  # re-read, not stale
    svc.reset_password(bob)
    assert ("reset", "sub-b") in cognito.calls
    svc.delete(bob)
    assert svc.get_user("sub-b") is None


def test_entra_user_writes_are_refused_before_any_call():
    cognito = Cognito(POOL, GROUPS)
    svc, _ = service(cognito, users=Users(ENTRA))
    dan = svc.get_user("e-1")
    before = len(cognito.calls)
    for op in (lambda: svc.set_role(dan, "admin"), lambda: svc.set_teams(dan, ["hr"]),
               lambda: svc.set_enabled(dan, False), lambda: svc.reset_password(dan), lambda: svc.delete(dan)):
        with pytest.raises(NotCognitoUser):
            op()
    assert len(cognito.calls) == before


def test_pool_failure_serves_previous_snapshot():
    class Flaky(Cognito):
        def list_users(self, *a, **kw):
            if self.fail:
                raise RuntimeError("AccessDenied")
            return super().list_users(*a, **kw)

    cognito = Flaky(POOL, GROUPS)
    cognito.fail = False
    svc, now = service(cognito)
    assert len(svc.list_users()) == 3
    cognito.fail = True
    now[0] += 61
    assert len(svc.list_users()) == 3


def test_unconfigured_without_pool_and_table():
    svc = OrgDirectoryService(Cognito(), "", users=None)
    assert svc.configured is False and svc.list_users() == []


def test_invite_refuses_missing_team_group_before_creating_user():
    cognito = Cognito(POOL, GROUPS)
    svc, _ = service(cognito)
    with pytest.raises(ValueError, match="team:nope"):
        svc.invite("gina@example.com", teams=["hr", "nope"], role="user")
    assert not any(c[0] == "admin_create_user" for c in cognito.calls if isinstance(c, tuple))


def test_invite_group_add_failure_invalidates_cache_and_reports():
    class FailSecond(Cognito):
        def admin_add_user_to_group(self, UserPoolId, Username, GroupName):
            if GroupName == "team:hr":
                raise err("InternalErrorException")
            return super().admin_add_user_to_group(UserPoolId, Username, GroupName)

    cognito = FailSecond(POOL, GROUPS)
    svc, _ = service(cognito)
    svc.list_users()  # warm the cache before the half-failed invite
    with pytest.raises(RuntimeError, match="team:hr"):
        svc.invite("hank@example.com", teams=["hr"], role="user")
    assert any(u.email == "hank@example.com" for u in svc.list_users())


def test_add_and_remove_team_use_fresh_teams_not_a_stale_object():
    cognito = Cognito(POOL, GROUPS)
    svc, _ = service(cognito)
    stale = svc.get_user("sub-a")  # teams == []
    svc.add_to_team(stale, "hr")
    svc.add_to_team(stale, "ops")  # stale.teams is still []; must not drop hr
    assert not any(c[0] == "remove" for c in cognito.calls if isinstance(c, tuple))
    assert svc.get_user("sub-a").teams == ["hr", "ops"]
    svc.remove_from_team(stale, "hr")
    assert svc.get_user("sub-a").teams == ["ops"]


def test_member_subs_sees_members_the_snapshot_does_not():
    # Final review I3: the delete guard must not depend on a ListUsers snapshot.
    groups = {**GROUPS, "team:hr": ["sub-b", "sub-new"]}

    class Failing(Cognito):
        def list_users(self, *a, **kw):
            raise RuntimeError("Throttling")

    pool = {**POOL, "sub-new": {"email": "new@example.com"}}  # made in the console after the snapshot
    svc, _ = service(Failing(pool, groups))
    assert svc.group_members("hr") == []  # decorated view: nobody known
    assert svc.member_subs("hr") == ["sub-b", "sub-new"]


def test_member_subs_raises_when_cognito_does():
    class Down(Cognito):
        def list_users_in_group(self, *a, **kw):
            raise RuntimeError("AccessDenied")

    svc, _ = service(Down(POOL, GROUPS))
    with pytest.raises(RuntimeError):
        svc.member_subs("hr")


def test_list_users_failure_is_a_warning(caplog):
    class Failing(Cognito):
        def list_users(self, *a, **kw):
            raise RuntimeError("AccessDenied")

    svc, _ = service(Failing(POOL, GROUPS))
    with caplog.at_level("WARNING"):
        assert svc.list_users() == []
    assert any(r.levelname == "WARNING" for r in caplog.records)


def test_has_pool():
    assert service()[0].has_pool is True
    assert OrgDirectoryService(Cognito(), "", users=Users([])).has_pool is False
