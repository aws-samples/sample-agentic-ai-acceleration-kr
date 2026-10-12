"""The threads one agent answered in — the list the trace and evaluation UIs need.

Both of those act on a thread: a span timeline is fetched per thread, and a batch
evaluation is started over thread ids. Neither the registry record nor the usage
rollup table can supply them — usage is counters only, keyed by date and agent —
so this route is the only way the UI gets from "this agent" to "these threads".

Two rules it must not lose:

  * **Scoping follows the thread routes.** A plain user sees only their own
    threads; an admin sees everyone's. The record itself is public, but the
    conversations under it are not.
  * **The record filter happens in the query, not after it.** A thread the caller
    may not see must never be materialised, and a filter applied after the limit
    would silently return fewer rows than asked for.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routes.insights as insights  # noqa: E402
from core.auth import AuthUser  # noqa: E402


class StubThread:
    def __init__(self, thread_id, owner_sub="sub-1", record_id="rec-1"):
        self.thread_id = thread_id
        self.owner_sub = owner_sub
        self.agent_record_id = record_id
        self.created_at = "2026-08-16T10:00:00"
        self.updated_at = "2026-08-16T10:05:00"
        self.status = "idle"
        self.values = {"messages": [{"type": "human"}, {"type": "ai"}]}


class StubThreads:
    """Records the arguments it was called with, so the scoping can be asserted."""

    def __init__(self, threads):
        self.threads = threads
        self.calls = []

    def search_threads(self, owner_sub, limit=20, offset=0, sort_by="updated_at",
                       sort_order="desc", status=None, metadata=None,
                       agent_record_id=None):
        self.calls.append({
            "owner_sub": owner_sub,
            "limit": limit,
            "agent_record_id": agent_record_id,
            "sort_by": sort_by,
            "sort_order": sort_order,
        })
        return self.threads


def user(sub="sub-1", is_admin=False):
    groups = ["admin"] if is_admin else []
    return AuthUser(sub=sub, username=sub, groups=groups)


def teardown_function():
    insights._threads_override = None


def test_a_plain_user_only_sees_their_own_threads():
    threads = StubThreads([StubThread("t-1")])
    insights._threads_override = threads

    insights.record_threads(record_id="rec-1", limit=20, user=user(sub="sub-9"))

    assert threads.calls[0]["owner_sub"] == "sub-9"


def test_an_admin_sees_every_thread():
    threads = StubThreads([StubThread("t-1", owner_sub="someone-else")])
    insights._threads_override = threads

    insights.record_threads(
        record_id="rec-1", limit=20, user=user(sub="admin-1", is_admin=True)
    )

    # None means unscoped, the same signal routes/threads.py uses.
    assert threads.calls[0]["owner_sub"] is None


def test_the_record_filter_is_pushed_into_the_query():
    threads = StubThreads([StubThread("t-1")])
    insights._threads_override = threads

    insights.record_threads(record_id="rec-1", limit=20, user=user())

    assert threads.calls[0]["agent_record_id"] == "rec-1"


def test_the_newest_thread_comes_first():
    threads = StubThreads([StubThread("t-1")])
    insights._threads_override = threads

    insights.record_threads(record_id="rec-1", limit=20, user=user())

    assert threads.calls[0]["sort_by"] == "updated_at"
    assert threads.calls[0]["sort_order"] == "desc"


def test_each_thread_carries_what_the_list_renders():
    threads = StubThreads([StubThread("t-1")])
    insights._threads_override = threads

    body = insights.record_threads(record_id="rec-1", limit=20, user=user())

    assert body["record_id"] == "rec-1"
    row = body["threads"][0]
    assert row["thread_id"] == "t-1"
    assert row["updated_at"] == "2026-08-16T10:05:00"
    # Turn count comes from the stored messages: the usage table counts turns per
    # day and agent, never per thread, so this is the only place it exists.
    assert row["turns"] == 1


def test_a_thread_with_no_messages_reports_zero_turns_not_unknown():
    empty = StubThread("t-1")
    empty.values = {}
    insights._threads_override = StubThreads([empty])

    body = insights.record_threads(record_id="rec-1", limit=20, user=user())

    assert body["threads"][0]["turns"] == 0


def test_no_threads_is_an_empty_list_not_a_404():
    """An agent nobody has talked to yet is a normal answer, not an error."""
    insights._threads_override = StubThreads([])

    body = insights.record_threads(record_id="rec-1", limit=20, user=user())

    assert body["threads"] == []


def test_the_limit_is_clamped_rather_than_rejected():
    threads = StubThreads([StubThread("t-1")])
    insights._threads_override = threads

    insights.record_threads(record_id="rec-1", limit=5000, user=user())

    assert threads.calls[0]["limit"] == insights.MAX_RECORD_THREADS


def test_the_route_is_sync():
    """Every handler in this router blocks on boto3; see the module docstring."""
    import inspect

    assert not inspect.iscoroutinefunction(insights.record_threads)
