"""The Organization people list reads the pool once per window and the usage once
per minute, however many admins load it at the same time.

Covers the single-flight pool refresh in OrgDirectoryService and the cached,
prefetched usage read behind GET /api/org/users. In-memory fakes, no AWS.
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routes.org as org  # noqa: E402
from services.org_directory_service import OrgDirectoryService  # noqa: E402


class PoolStub:
    """One user in the pool, in `admin` and `team:hr`. The first ListUsers waits
    on `gate` (when given) and then fails if `fail` is set."""

    def __init__(self, gate=None, fail=False):
        self.gate, self.fail = gate, fail
        self.calls = []

    def list_users(self, UserPoolId, Limit, PaginationToken=None):
        self.calls.append("list_users")
        if self.gate is not None and self.calls.count("list_users") == 1:
            self.gate.wait(5)
        if self.fail:
            raise RuntimeError("AccessDenied")
        return {"Users": [{
            "Username": "sub-a", "UserStatus": "CONFIRMED", "Enabled": True,
            "Attributes": [{"Name": "sub", "Value": "sub-a"}, {"Name": "email", "Value": "a@example.com"}],
        }]}

    def list_groups(self, UserPoolId, Limit=60, NextToken=None):
        self.calls.append("list_groups")
        return {"Groups": [{"GroupName": "admin"}, {"GroupName": "team:hr"}]}

    def list_users_in_group(self, UserPoolId, GroupName, Limit=60, NextToken=None):
        self.calls.append(("list_users_in_group", GroupName))
        if GroupName == "team:hr":
            return {"Users": [{"Username": "sub-a", "Attributes": [{"Name": "sub", "Value": "sub-a"}]}]}
        return {"Users": []}


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert predicate(), "timed out waiting for the other thread"


def test_requests_that_find_the_snapshot_expired_share_one_pool_read():
    gate = threading.Event()
    pool = PoolStub(gate=gate)
    svc = OrgDirectoryService(pool, "pool-1", cache_seconds=60)
    results = []
    threads = [threading.Thread(target=lambda: results.append(svc.list_users())) for _ in range(4)]

    threads[0].start()
    wait_until(lambda: pool.calls.count("list_users") == 1)
    for thread in threads[1:]:
        thread.start()
    time.sleep(0.05)
    gate.set()
    for thread in threads:
        thread.join(5)

    assert pool.calls.count("list_users") == 1
    assert len(results) == 4 and all([u.sub for u in r] == ["sub-a"] for r in results)
    assert all(u.teams == ["hr"] and u.role == "user" for u in results[0])


def test_callers_waiting_on_a_failed_pool_read_do_not_each_read_again():
    gate = threading.Event()
    pool = PoolStub(gate=gate, fail=True)
    svc = OrgDirectoryService(pool, "pool-1", cache_seconds=60)
    results = []
    threads = [threading.Thread(target=lambda: results.append(svc.list_users())) for _ in range(4)]

    threads[0].start()
    wait_until(lambda: pool.calls.count("list_users") == 1)
    for thread in threads[1:]:
        thread.start()
    time.sleep(0.05)
    gate.set()
    for thread in threads:
        thread.join(5)

    assert pool.calls.count("list_users") == 1
    assert results == [[], [], [], []]


class UsageStub:
    configured = True

    def __init__(self, complete=True, fail=False):
        self.complete, self.fail = complete, fail
        self.totals_calls = 0
        self.prefetched = []

    def begin_read(self):
        pass

    def prefetch(self, prefixes, start_date, end_date):
        self.prefetched.append(list(prefixes))

    def reads_complete(self):
        return self.complete

    def user_totals(self, start_date, end_date):
        self.totals_calls += 1
        if self.fail:
            raise RuntimeError("DynamoDB unavailable")
        return {"sub-a": {"turns": 3}}

    def user_costs(self, start_date, end_date):
        return {"sub-a": {"model_cost_micros": 1500}}


def test_usage_by_user_is_read_once_and_prefetches_the_user_partitions(monkeypatch):
    usage = UsageStub()
    monkeypatch.setattr(org, "_usage_override", usage)

    first = org._usage_by_user()
    second = org._usage_by_user()

    assert first == second == {"sub-a": {"turns_30d": 3, "model_cost_micros_30d": 1500}}
    assert usage.totals_calls == 1
    assert usage.prefetched == [["USERS#"]]


def test_an_incomplete_usage_read_is_answered_but_read_again(monkeypatch):
    usage = UsageStub(complete=False)
    monkeypatch.setattr(org, "_usage_override", usage)

    assert org._usage_by_user()["sub-a"]["turns_30d"] == 3
    org._usage_by_user()

    assert usage.totals_calls == 2


def test_a_failed_usage_read_is_empty_for_that_load_and_not_remembered(monkeypatch):
    usage = UsageStub(fail=True)
    monkeypatch.setattr(org, "_usage_override", usage)

    assert org._usage_by_user() == {}
    usage.fail = False
    assert org._usage_by_user()["sub-a"]["turns_30d"] == 3
    assert usage.totals_calls == 2
