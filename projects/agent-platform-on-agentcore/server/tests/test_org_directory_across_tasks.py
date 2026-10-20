"""The people directory with more than one server task (bst T1 smoke, 2026-10-18).

Each task keeps its own snapshot of the pool. A user deleted through one task
stayed listed by the other for up to a minute (3 of 16 list calls), and a
second delete answered 500. Writes now bump a version in the teams table and a
task whose snapshot predates it reads the pool again. Also: a reset for a user
who never signed in resends the invitation (Cognito refuses the reset), and a
user Cognito no longer has is UserGone (404), not a 500.
"""
import os
import sys

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.org_directory_service import COGNITO, OrgDirectoryService, OrgUser, UserGone  # noqa: E402


class Version:
    """TeamRepository.directory_version / bump_directory_version over an int."""

    def __init__(self):
        self.v = 0

    def directory_version(self):
        return self.v

    def bump_directory_version(self):
        self.v += 1


class Pool:
    def __init__(self):
        self.users = {"sub-a": "CONFIRMED", "sub-b": "FORCE_CHANGE_PASSWORD"}
        self.calls = []

    def list_users(self, UserPoolId, Limit, PaginationToken=None):
        self.calls.append("list_users")
        return {"Users": [{"Username": sub, "UserStatus": status, "Enabled": True,
                           "Attributes": [{"Name": "sub", "Value": sub}]} for sub, status in self.users.items()]}

    def list_groups(self, UserPoolId, Limit=60, NextToken=None):
        return {"Groups": []}

    def list_users_in_group(self, UserPoolId, GroupName, Limit=60, NextToken=None):
        return {"Users": []}

    def _need(self, username):
        if username not in self.users:
            raise ClientError({"Error": {"Code": "UserNotFoundException", "Message": "gone"}}, "op")

    def admin_delete_user(self, UserPoolId, Username):
        self.calls.append(("delete", Username))
        self._need(Username)
        del self.users[Username]

    def admin_reset_user_password(self, UserPoolId, Username):
        self.calls.append(("reset", Username))
        self._need(Username)
        if self.users[Username] == "FORCE_CHANGE_PASSWORD":
            raise ClientError({"Error": {"Code": "NotAuthorizedException",
                                         "Message": "User password cannot be reset in the current state."}}, "op")

    def admin_create_user(self, **kw):
        # As the live pool (username_attributes = email): RESEND by e-mail only.
        if "@" not in kw["Username"]:
            raise ClientError({"Error": {"Code": "InvalidParameterException",
                                         "Message": "Username should be an email."}}, "AdminCreateUser")
        self.calls.append(("create", kw.get("Username"), kw.get("MessageAction")))
        return {"User": {"Username": kw["Username"]}}


def two_tasks():
    pool, version = Pool(), Version()
    return pool, OrgDirectoryService(pool, "p", version=version), OrgDirectoryService(pool, "p", version=version)


def user(sub):
    # The pool's username is the sub (a UUID); the e-mail is an attribute.
    return OrgUser(sub=sub, username=sub, email=f"{sub}@example.com", provider=COGNITO)


def test_a_delete_through_one_task_is_seen_by_the_other_at_once():
    pool, a, b = two_tasks()
    assert {u.sub for u in b.list_users()} == {"sub-a", "sub-b"}
    a.delete(user("sub-a"))
    assert {u.sub for u in b.list_users()} == {"sub-b"}


def test_an_unchanged_directory_is_served_from_the_snapshot():
    pool, a, b = two_tasks()
    b.list_users()
    b.list_users()
    assert pool.calls.count("list_users") == 1


def test_deleting_a_user_who_is_already_gone_is_user_gone():
    pool, a, b = two_tasks()
    a.delete(user("sub-a"))
    with pytest.raises(UserGone):
        b.delete(user("sub-a"))


def test_a_reset_for_someone_who_never_signed_in_resends_the_invitation():
    pool, a, _ = two_tasks()
    a.reset_password(user("sub-b"))
    assert ("create", "sub-b@example.com", "RESEND") in pool.calls


def test_a_reset_for_a_confirmed_user_is_a_reset():
    pool, a, _ = two_tasks()
    a.reset_password(user("sub-a"))
    assert pool.calls[-1] == ("reset", "sub-a")


def test_without_a_version_store_the_ttl_rules_as_before():
    pool = Pool()
    svc = OrgDirectoryService(pool, "p")
    svc.list_users()
    svc.list_users()
    assert pool.calls.count("list_users") == 1
