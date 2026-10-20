"""Slack user -> platform user (integrations/slack/identity.py)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.auth as auth  # noqa: E402
from core.config import OIDCProvider  # noqa: E402
from integrations.slack.identity import IdentityResolver  # noqa: E402


class Cognito:
    def __init__(self):
        self.users = {}  # username -> dict
        self.groups = {}  # username -> [names]
        self.list_calls = 0
        self.fail = False

    def add(self, username, email, sub, enabled=True, status="CONFIRMED", verified="true", groups=()):
        self.users[username] = {
            "Username": username,
            "Enabled": enabled,
            "UserStatus": status,
            "UserAttributes": [
                {"Name": "email", "Value": email},
                {"Name": "sub", "Value": sub},
                {"Name": "email_verified", "Value": verified},
            ],
        }
        self.groups[username] = list(groups)

    def list_users(self, UserPoolId, Filter, **_):
        self.list_calls += 1
        if self.fail:
            raise RuntimeError("cognito down")
        email = Filter.split('"')[1]
        found = [
            {"Username": u["Username"]}
            for u in self.users.values()
            if any(a["Name"] == "email" and a["Value"] == email for a in u["UserAttributes"])
        ]
        return {"Users": found}

    def admin_get_user(self, UserPoolId, Username):
        return dict(self.users[Username])

    def admin_list_groups_for_user(self, UserPoolId, Username, **_):
        return {"Groups": [{"GroupName": g} for g in self.groups[Username]]}


class Users:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def list_all(self):
        return [dict(r) for r in self.rows]

    def get(self, provider, sub):
        for r in self.rows:
            if r["provider"] == provider and r["sub"] == sub:
                return dict(r)
        return None


ENTRA = OIDCProvider(id="entra", label="Entra", issuer_url="https://login.example/t/v2.0", required_group="g-allowed", admin_groups=("g-admin",))


def _resolver(cognito=None, users=None, providers=(ENTRA,), enforced=True, clock=None):
    return IdentityResolver(
        cognito=cognito,
        user_pool_id="pool" if cognito is not None else "",
        users=users,
        providers=lambda: list(providers),
        auth_enforced=lambda: enforced,
        **({"clock": clock} if clock else {}),
    )


def test_a_confirmed_cognito_user_maps_with_live_groups():
    c = Cognito()
    c.add("u-name", "kim@example.com", "sub-1", groups=["team:sales"])
    result = _resolver(cognito=c).resolve("kim@example.com")
    assert result.refusal is None
    assert result.user.sub == "sub-1"
    assert result.user.teams == ["sales"]
    assert result.user.is_admin is False
    assert result.user.provider == "cognito"


def test_slack_email_case_still_finds_the_account():
    c = Cognito()
    c.add("u-name", "kim@example.com", "sub-1")
    assert _resolver(cognito=c).resolve("Kim@Example.com").user.sub == "sub-1"


def test_admin_group_makes_an_admin():
    c = Cognito()
    c.add("u", "a@example.com", "s", groups=["admin"])
    assert _resolver(cognito=c).resolve("a@example.com").user.is_admin


def test_unknown_email_is_unmapped():
    assert _resolver(cognito=Cognito(), users=Users()).resolve("nobody@example.com").refusal == "unmapped"
    assert _resolver(cognito=Cognito()).resolve("").refusal == "unmapped"


def test_two_accounts_with_the_email_refuse_even_if_one_is_disabled():
    c = Cognito()
    c.add("u1", "dup@example.com", "s1")
    c.add("u2", "dup@example.com", "s2", enabled=False)
    assert _resolver(cognito=c).resolve("dup@example.com").refusal == "ambiguous"


def test_a_cognito_and_an_entra_account_with_one_email_are_ambiguous():
    c = Cognito()
    c.add("u1", "dup@example.com", "s1")
    users = Users([{"provider": "entra", "sub": "e1", "email": "dup@example.com", "groups": ["g-allowed"]}])
    assert _resolver(cognito=c, users=users).resolve("dup@example.com").refusal == "ambiguous"


def test_disabled_unconfirmed_or_unverified_accounts_are_inactive():
    for kwargs in ({"enabled": False}, {"status": "FORCE_CHANGE_PASSWORD"}, {"verified": "false"}):
        c = Cognito()
        c.add("u", "x@example.com", "s", **kwargs)
        assert _resolver(cognito=c).resolve("x@example.com").refusal == "inactive"


def test_lookup_errors_refuse():
    c = Cognito()
    c.fail = True
    assert _resolver(cognito=c).resolve("x@example.com").refusal == "lookup_failed"


def test_the_email_lookup_is_cached_but_the_verdict_is_not():
    now = [0.0]
    c = Cognito()
    c.add("u", "x@example.com", "s")
    resolver = _resolver(cognito=c, clock=lambda: now[0])
    assert resolver.resolve("x@example.com").user is not None
    # Disabled between two messages: the next one is refused although the
    # email lookup itself is served from cache.
    c.users["u"]["Enabled"] = False
    assert resolver.resolve("x@example.com").refusal == "inactive"
    assert c.list_calls == 1  # one lookup, served twice
    now[0] = 10_000
    resolver.resolve("x@example.com")
    assert c.list_calls == 2


def test_entra_user_teams_are_recomputed_from_the_current_mapping(monkeypatch):
    monkeypatch.setattr(auth, "entra_team_resolver", lambda groups: ["finance"] if "g-fin" in groups else [])
    users = Users([{
        "provider": "entra", "sub": "e1", "email": "lee@example.com",
        # A stale tag from the last sign-in must not survive.
        "groups": ["g-allowed", "g-fin", "team:old"],
    }])
    user = _resolver(users=users).resolve("lee@example.com").user
    assert user.teams == ["finance"]
    assert user.provider == "entra"


def test_entra_user_outside_the_required_group_is_refused(monkeypatch):
    monkeypatch.setattr(auth, "entra_team_resolver", None)
    users = Users([{"provider": "entra", "sub": "e1", "email": "lee@example.com", "groups": ["g-other"]}])
    assert _resolver(users=users).resolve("lee@example.com").refusal == "not_allowed"


def test_entra_user_of_a_removed_provider_is_refused(monkeypatch):
    monkeypatch.setattr(auth, "entra_team_resolver", None)
    users = Users([{"provider": "entra", "sub": "e1", "email": "lee@example.com", "groups": ["g-allowed"]}])
    assert _resolver(users=users, providers=()).resolve("lee@example.com").refusal == "not_allowed"


def test_entra_admin_group_makes_an_admin(monkeypatch):
    monkeypatch.setattr(auth, "entra_team_resolver", None)
    users = Users([{"provider": "entra", "sub": "e1", "email": "lee@example.com", "groups": ["g-allowed", "g-admin"]}])
    assert _resolver(users=users).resolve("lee@example.com").user.is_admin


def test_without_auth_enforced_everyone_is_the_local_user():
    user = _resolver(enforced=False).resolve("anyone@example.com").user
    assert (user.sub, user.is_admin) == ("local", True)
