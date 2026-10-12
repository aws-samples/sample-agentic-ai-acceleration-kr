"""Resolving a Cognito `sub` to something a person recognises.

The pool signs people in by email (`username_attributes = ["email"]`), which
makes Cognito mint the username as a UUID — so the access token's `username`
claim *is* the sub, and nothing the server receives on a request carries the
email. The only way back to a name is `ListUsers`.

The whole pool is read in pages and held as one snapshot, not filtered per sub:
measured on the live pool, a `sub = "…"` filter costs the same ~300 ms as an
unfiltered page of 60, so per-person lookups scale with the number of people on
the page while a snapshot scales with the pool — and the page names the same
few people every minute.

Every failure shape here degrades to "no label": the page then falls back to
the shortened sub it always showed. A directory outage must never turn an
admin's usage page into a 5xx.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.directory_service import DirectoryService  # noqa: E402


class Cognito:
    """Just enough of cognito-idp: paginated, unfiltered `list_users`."""

    def __init__(self, users=None, page_size=60, fail=False):
        self.users = list((users or {}).items())
        self.page_size = page_size
        self.fail = fail
        self.calls = []

    def list_users(self, UserPoolId, Limit, PaginationToken=None):
        self.calls.append((UserPoolId, Limit, PaginationToken))
        if self.fail:
            raise RuntimeError("AccessDeniedException")
        start = int(PaginationToken or 0)
        page = self.users[start:start + self.page_size]
        out = {"Users": [{
            "Username": sub,
            "Attributes": [{"Name": "sub", "Value": sub}, {"Name": "email", "Value": email}],
        } for sub, email in page]}
        if start + self.page_size < len(self.users):
            out["PaginationToken"] = str(start + self.page_size)
        return out


def test_a_known_sub_resolves_to_its_email():
    cognito = Cognito({"sub-1": "alice@example.com", "sub-2": "bob@example.com"})
    svc = DirectoryService(cognito=cognito, user_pool_id="pool-1")

    assert svc.emails(["sub-1"]) == {"sub-1": "alice@example.com"}
    assert cognito.calls == [("pool-1", 60, None)]


def test_the_snapshot_follows_pagination_to_the_end():
    """A pool larger than one page is still one snapshot; a person on page two
    must not come back nameless."""
    users = {f"sub-{i}": f"user{i}@example.com" for i in range(130)}
    cognito = Cognito(users, page_size=60)
    svc = DirectoryService(cognito=cognito, user_pool_id="pool-1")

    assert svc.emails(["sub-129"]) == {"sub-129": "user129@example.com"}
    assert len(cognito.calls) == 3


def test_an_unknown_sub_is_absent_not_blank():
    """A deleted account still owns ledger rows. The client must see *no* label
    and fall back to the sub, not an empty string it would render as nothing."""
    svc = DirectoryService(cognito=Cognito({}), user_pool_id="pool-1")

    assert svc.emails(["ghost"]) == {}


def test_the_snapshot_is_reused_across_calls():
    """`/summary` polls once a minute; the pool is read once per cache window,
    not once per poll and not once per person."""
    cognito = Cognito({"sub-1": "alice@example.com"})
    svc = DirectoryService(cognito=cognito, user_pool_id="pool-1")

    svc.emails(["sub-1", "ghost"])
    svc.emails(["sub-1", "ghost"])
    svc.emails(["ghost"])

    assert len(cognito.calls) == 1


def test_a_directory_failure_yields_no_labels_and_no_exception():
    """The task role may lack ListUsers (it did, before this shipped). The page
    must still render — with subs — rather than fail because a label was
    unavailable."""
    svc = DirectoryService(cognito=Cognito(fail=True), user_pool_id="pool-1")

    assert svc.emails(["sub-1"]) == {}


def test_a_failed_refresh_keeps_serving_the_last_snapshot():
    """A throttled or failed refresh should cost nothing visible: the names that
    were right a moment ago are still right enough."""
    cognito = Cognito({"sub-1": "alice@example.com"})
    svc = DirectoryService(cognito=cognito, user_pool_id="pool-1", cache_seconds=0)
    assert svc.emails(["sub-1"]) == {"sub-1": "alice@example.com"}

    cognito.fail = True
    assert svc.emails(["sub-1"]) == {"sub-1": "alice@example.com"}


def test_an_unconfigured_pool_never_calls_cognito():
    """Local development without a pool: `AUTH_ENFORCED=false`, no pool id, and
    the caller is `local`. There is nothing to resolve and nothing to call."""
    cognito = Cognito({"local": "x@example.com"})
    svc = DirectoryService(cognito=cognito, user_pool_id="")

    assert svc.emails(["local"]) == {}
    assert cognito.calls == []


def test_blank_and_duplicate_subs_are_ignored():
    cognito = Cognito({"sub-1": "alice@example.com"})
    svc = DirectoryService(cognito=cognito, user_pool_id="pool-1")

    assert svc.emails(["", None, "sub-1", "sub-1"]) == {"sub-1": "alice@example.com"}


# ── OIDC users table ─────────────────────────────────────────────────────────

class Users:
    """The USERS_TABLE repository: `labels() -> {sub: email-or-name}`."""

    def __init__(self, rows=None, fail=False):
        self.rows = dict(rows or {})
        self.fail = fail
        self.calls = 0

    def labels(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("ResourceNotFoundException")
        return dict(self.rows)


def test_an_entra_user_is_named_from_the_users_table():
    """Entra people are not in the pool; the record written on login is the
    only source of their name."""
    svc = DirectoryService(cognito=Cognito({"c-1": "carol@example.com"}), user_pool_id="pool-1",
                           users=Users({"e-1": "erin@corp.com"}))

    assert svc.emails(["c-1", "e-1"]) == {"c-1": "carol@example.com", "e-1": "erin@corp.com"}


def test_the_users_table_resolves_without_a_pool():
    """An Entra-only deployment has no Cognito pool to read and must not need one."""
    cognito = Cognito({"x": "x@example.com"})
    svc = DirectoryService(cognito=cognito, user_pool_id="", users=Users({"e-1": "erin@corp.com"}))

    assert svc.emails(["e-1"]) == {"e-1": "erin@corp.com"}
    assert cognito.calls == []


def test_the_pool_wins_when_both_name_the_same_sub():
    svc = DirectoryService(cognito=Cognito({"s": "pool@example.com"}), user_pool_id="pool-1",
                           users=Users({"s": "table@corp.com"}))

    assert svc.emails(["s"]) == {"s": "pool@example.com"}


def test_a_users_table_failure_keeps_the_previous_snapshot():
    """Half a directory is worse than a stale one: losing one IdP's people would
    rename them to subs mid-session."""
    users = Users({"e-1": "erin@corp.com"})
    svc = DirectoryService(cognito=Cognito({"c-1": "carol@example.com"}), user_pool_id="pool-1",
                           users=users, cache_seconds=0)
    assert svc.emails(["c-1", "e-1"]) == {"c-1": "carol@example.com", "e-1": "erin@corp.com"}

    users.fail = True
    assert svc.emails(["c-1", "e-1"]) == {"c-1": "carol@example.com", "e-1": "erin@corp.com"}
