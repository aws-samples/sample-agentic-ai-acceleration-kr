"""The shared Insights dashboards (core/cache.StaleWhileRevalidate).

One summary was ~20 s of CPU on the 0.5-vCPU server at 1,000 people, during
which every other request waited (2026-10-18). Computed once per window and
shared: fresh for a minute, then served stale while one background read
replaces it. The admin gate still runs on every request, and a non-admin
reading a record's turns still never sees their owners.
"""
import os
import sys
import threading

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routes.insights as insights  # noqa: E402
from core.auth import AuthUser  # noqa: E402
from core.cache import StaleWhileRevalidate  # noqa: E402


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _cache(clock, spawned):
    return StaleWhileRevalidate(fresh_seconds=60, max_age_seconds=900, clock=clock, spawn=spawned.append)


def test_fresh_values_are_served_without_a_load():
    clock, spawned, loads = Clock(), [], []
    cache = _cache(clock, spawned)
    load = lambda: loads.append(1) or len(loads)  # noqa: E731
    assert cache.get("k", load) == (1, 0.0)
    clock.now += 59
    assert cache.get("k", load) == (1, 59)
    assert loads == [1] and spawned == []


def test_a_stale_value_is_served_while_one_background_load_replaces_it():
    clock, spawned, loads = Clock(), [], []
    cache = _cache(clock, spawned)
    load = lambda: loads.append(1) or len(loads)  # noqa: E731
    cache.get("k", load)
    clock.now += 120
    assert cache.get("k", load)[0] == 1
    assert cache.get("k", load)[0] == 1  # a second reader starts no second refresh
    assert len(spawned) == 1
    spawned.pop()()  # the refresh runs
    assert cache.get("k", load) == (2, 0.0)


def test_a_failed_refresh_keeps_the_stale_value():
    clock, spawned = Clock(), []
    cache = _cache(clock, spawned)
    cache.get("k", lambda: "old")
    clock.now += 120

    def boom():
        raise RuntimeError("ddb down")

    assert cache.get("k", boom)[0] == "old"
    spawned.pop()()
    assert cache.get("k", boom)[0] == "old"
    assert len(spawned) == 1  # and the next reader retries


def test_too_old_is_loaded_by_the_caller_and_invalidate_forgets():
    clock, spawned = Clock(), []
    cache = _cache(clock, spawned)
    cache.get("k", lambda: "old")
    clock.now += 901
    assert cache.get("k", lambda: "new") == ("new", 0.0)
    cache.invalidate()
    assert cache.get("k", lambda: "after write") == ("after write", 0.0)
    assert spawned == []


def test_concurrent_cold_readers_share_one_load():
    cache = StaleWhileRevalidate(fresh_seconds=60, max_age_seconds=900)
    release, started, calls = threading.Event(), threading.Event(), []

    def load():
        calls.append(1)
        started.set()
        release.wait(5)
        return "v"

    results = []
    threads = [threading.Thread(target=lambda: results.append(cache.get("k", load)[0])) for _ in range(8)]
    threads[0].start()
    started.wait(5)
    for t in threads[1:]:
        t.start()
    release.set()
    for t in threads:
        t.join(5)
    assert results == ["v"] * 8 and calls == [1]


# --- the routes -----------------------------------------------------------------

@pytest.fixture
def stub_compute(monkeypatch):
    calls = []

    def compute_record(record_id, days, user):
        calls.append(user.is_admin)
        return {"record_id": record_id, "turns": [
            {"turn_id": "t1", "owner_sub": "alice", "thread_id": "th-alice"},
            {"turn_id": "t2", "owner_sub": "s", "thread_id": "th-mine"},
        ]}

    monkeypatch.setattr(insights, "_compute_record_insights", compute_record)
    monkeypatch.setattr(insights, "_compute_summary", lambda days, user: calls.append("summary") or {"days": days})
    insights._DASHBOARDS.invalidate()
    yield calls
    insights._DASHBOARDS.invalidate()


def _user(admin):
    return AuthUser(sub="s", username="u", groups=["admin"] if admin else [], role="admin" if admin else "user")


def test_a_non_admin_is_refused_before_any_shared_value_is_read(stub_compute):
    insights.summary(days=7, user=_user(True))
    with pytest.raises(HTTPException) as exc:
        insights.summary(days=7, user=_user(False))
    assert exc.value.status_code == 403
    assert stub_compute == ["summary"]


def test_a_record_page_is_computed_once_and_owners_hidden_from_non_admins(stub_compute):
    admin_view = insights.record_insights(record_id="rec-1", days=7, user=_user(True))
    user_view = insights.record_insights(record_id="rec-1", days=7, user=_user(False))
    assert admin_view["turns"][0]["owner_sub"] == "alice"
    assert user_view["turns"][0]["owner_sub"] is None
    assert stub_compute == [True]  # one computation, as the admin view


def test_a_non_admin_sees_threads_only_on_their_own_turns(stub_compute):
    """Someone else's thread id says which conversation used the agent; the
    counters are the public part of a record."""
    turns = insights.record_insights(record_id="rec-1", days=7, user=_user(False))["turns"]
    assert [t["thread_id"] for t in turns] == [None, "th-mine"]
    # turn_id is "<thread>:<message>" in the ledger: someone else's is replaced,
    # stays unique and stable, and gives nothing of the original away.
    assert turns[1]["turn_id"] == "t2"
    assert turns[0]["turn_id"].startswith("t-") and "th-alice" not in turns[0]["turn_id"]
    again = insights.record_insights(record_id="rec-1", days=7, user=_user(False))["turns"]
    assert again[0]["turn_id"] == turns[0]["turn_id"]
    admin_turns = insights.record_insights(record_id="rec-1", days=7, user=_user(True))["turns"]
    assert [t["thread_id"] for t in admin_turns] == ["th-alice", "th-mine"]


def test_the_map_is_bounded_and_drops_the_oldest_computed():
    """record_insights is keyed by any record id a user sends; the map must not
    grow with them."""
    clock = Clock()
    cache = StaleWhileRevalidate(fresh_seconds=60, max_age_seconds=900, clock=clock,
                                 spawn=lambda fn: None, max_entries=3)
    for key in ("a", "b", "c", "d"):
        cache.get(key, lambda key=key: key)
        clock.now += 1
    assert sorted(cache._values) == ["b", "c", "d"]


def test_values_past_max_age_go_first():
    clock = Clock()
    cache = StaleWhileRevalidate(fresh_seconds=60, max_age_seconds=900, clock=clock,
                                 spawn=lambda fn: None, max_entries=2)
    cache.get("old", lambda: 1)
    clock.now += 1000
    cache.get("x", lambda: 2)
    cache.get("y", lambda: 3)
    assert sorted(cache._values) == ["x", "y"]


def test_unsupported_days_share_the_window_they_are_served():
    assert insights._supported_days(12345) == insights._supported_days(90) == max(insights.SUPPORTED_DAYS)
    assert insights._supported_days(7) == 7
