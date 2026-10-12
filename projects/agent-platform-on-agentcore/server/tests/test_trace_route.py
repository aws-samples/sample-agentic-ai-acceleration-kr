"""A span timeline is one person's conversation, not an org aggregate.

Everything else on /insights is deliberately open to any authenticated user. This
route is not: it exposes what happened inside a specific thread, so it follows
thread ownership — owner or admin.

The route is thread-scoped, not turn-scoped. It always was in substance: the
handler asks the trace service for the whole session's spans over the thread's
lifetime, and the old `/{turn_id}` segment was echoed back untouched. A URL that
names a turn it never filters on is a promise the data cannot keep.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException  # noqa: E402

import routes.insights as insights  # noqa: E402
from core.auth import AuthUser  # noqa: E402
from services.trace_service import TracesUnavailable  # noqa: E402


class StubThread:
    def __init__(self, owner_sub="sub-1"):
        self.owner_sub = owner_sub
        self.thread_id = "t-1"
        self.created_at = "2026-08-16T10:00:00"
        self.updated_at = "2026-08-16T10:05:00"


class StubThreads:
    def __init__(self, thread=None):
        self.thread = thread

    def get_thread(self, thread_id):
        return self.thread


class StubTraces:
    def __init__(self, result=None, unavailable=False):
        self.result = result or {"status": "ok", "spans": [], "log_group": "aws/spans"}
        self.unavailable = unavailable
        self.asked = []

    def session_id_for(self, thread_id):
        return f"sess-{thread_id}"

    def spans_for_session(self, session_id, start, end):
        if self.unavailable:
            raise TracesUnavailable("Transaction Search is off")
        self.asked.append(session_id)
        return self.result


def user(sub="sub-1", is_admin=False):
    """Create a real AuthUser instance for testing."""
    groups = ["admin"] if is_admin else []
    return AuthUser(sub=sub, username=sub, groups=groups)


def wire(threads, traces):
    insights._threads_override = threads
    insights._traces_override = traces


def teardown_function():
    insights._threads_override = None
    insights._traces_override = None


def test_the_owner_gets_the_timeline():
    traces = StubTraces({"status": "ok", "spans": [{"name": "model"}],
                         "log_group": "aws/spans"})
    wire(StubThreads(StubThread("sub-1")), traces)

    body = insights.thread_traces(thread_id="t-1", user=user(sub="sub-1"))

    assert body["status"] == "ok"
    assert traces.asked == ["sess-t-1"]


def test_someone_elses_thread_is_404_not_403():
    """A 403 would confirm the thread exists. Matching the thread routes."""
    wire(StubThreads(StubThread("sub-2")), StubTraces())

    try:
        insights.thread_traces(thread_id="t-1", user=user(sub="sub-1"))
    except HTTPException as exc:
        assert exc.status_code == 404
        return
    raise AssertionError("expected 404")


def test_an_admin_may_read_any_thread():
    wire(StubThreads(StubThread("sub-2")), StubTraces())

    body = insights.thread_traces(
        thread_id="t-1", user=user(sub="admin-1", is_admin=True)
    )

    assert body["status"] == "ok"


def test_a_missing_thread_is_404():
    wire(StubThreads(None), StubTraces())

    try:
        insights.thread_traces(thread_id="t-1", user=user())
    except HTTPException as exc:
        assert exc.status_code == 404
        return
    raise AssertionError("expected 404")


def test_a_timeout_is_a_200_the_user_can_retry():
    """Not a 5xx: SWR would take the retry out of the user's hands."""
    wire(
        StubThreads(StubThread("sub-1")),
        StubTraces({"status": "timeout", "spans": [], "log_group": None}),
    )

    body = insights.thread_traces(thread_id="t-1", user=user())

    assert body["status"] == "timeout"


def test_traces_unavailable_explains_the_prerequisite_in_a_200():
    wire(StubThreads(StubThread("sub-1")), StubTraces(unavailable=True))

    body = insights.thread_traces(thread_id="t-1", user=user())

    assert body["status"] == "unavailable"
    assert body["sources"]["traces"] is False
    assert "Transaction Search" in body["detail"]


def test_the_trace_route_is_sync():
    import inspect

    assert not inspect.iscoroutinefunction(insights.thread_traces)
