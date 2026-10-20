"""Runtime cost per person needs each session's owner. Read from the threads by
id — the cost is the number of sessions — not from every turn event of the
window (70k events for three sessions at 1,000 people, 2026-10-18). A thread
deleted since still has turn events that name its owner."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routes.insights as insights  # noqa: E402


class Threads:
    def __init__(self, owners):
        self.owners, self.asked = owners, []

    def owners_of(self, ids):
        self.asked.append(list(ids))
        return {i: self.owners[i] for i in ids if i in self.owners}


class Usage:
    def __init__(self, events):
        self.events, self.calls = events, []

    def turn_events(self, start, end, thread_ids=None, fields=None):
        self.calls.append(sorted(thread_ids or []))
        return [e for e in self.events if e["thread_id"] in (thread_ids or [])]


def teardown_function():
    insights._threads_override = None


def test_owners_come_from_the_threads_and_deleted_ones_from_their_events():
    insights._threads_override = Threads({"t1": "alice", "t2": "bob"})
    usage = Usage([{"thread_id": "t3", "owner_sub": "carol"}, {"thread_id": "t9", "owner_sub": "x"}])
    window = {"start_date": "2026-10-01", "end_date": "2026-10-18"}

    owners = insights._session_owners(["t1", "t2", "t3"], usage, window)

    assert owners == {"t1": "alice", "t2": "bob", "t3": "carol"}
    assert usage.calls == [["t3"]]  # only the deleted thread's events were read


def test_no_event_read_when_every_thread_is_found():
    insights._threads_override = Threads({"t1": "alice"})
    usage = Usage([])
    insights._session_owners(["t1"], usage, {"start_date": "a", "end_date": "b"})
    assert usage.calls == []
