"""Historical threads can give up turns and tool calls, but never tokens.

Usage was never stored, so `values.messages` holds content, tool_calls, charts and
verifications and nothing else. A backfill that wrote 0 tokens would make every
pre-adoption agent look free — the strongest possible version of the wrong answer.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.backfill_usage import backfill_thread  # noqa: E402


class StubThread:
    def __init__(self):
        self.thread_id = "t-1"
        self.owner_sub = "sub-1"
        self.agent_record_id = "rec-1"
        self.created_at = "2026-08-01T10:00:00"
        self.values = {"messages": [
            {"id": "u-1", "type": "human", "content": "hi"},
            {"id": "m-1", "type": "ai", "content": "hello",
             "tool_calls": [{"name": "WebSearch", "status": "completed"}]},
            {"id": "m-2", "type": "ai", "content": "more"},
        ]}


class RecordingUsage:
    configured = True

    def __init__(self):
        self.turns = []

    def record_turn(self, **kwargs):
        self.turns.append(kwargs)


def test_backfill_counts_one_turn_per_assistant_message():
    usage = RecordingUsage()

    assert backfill_thread(StubThread(), usage) == 2
    assert len(usage.turns) == 2


def test_backfill_never_writes_token_counts():
    usage = RecordingUsage()

    backfill_thread(StubThread(), usage)

    for turn in usage.turns:
        assert turn["input_tokens"] == 0
        assert turn["output_tokens"] == 0
    # Task 3 drops zero counters, so a 0 here never reaches the table — the
    # attribute stays absent and the read side renders "unknown".


def test_backfill_recovers_the_tool_calls_that_were_stored():
    usage = RecordingUsage()

    backfill_thread(StubThread(), usage)

    called = [t["tool_calls"] for t in usage.turns]
    assert {"WebSearch": 1} in called


def test_backfill_marks_only_the_first_turn_as_starting_the_thread():
    usage = RecordingUsage()

    backfill_thread(StubThread(), usage)

    assert [t["thread_started"] for t in usage.turns] == [True, False]


def test_backfill_uses_the_thread_date_not_today():
    usage = RecordingUsage()

    backfill_thread(StubThread(), usage)

    assert all(t["date"] == "2026-08-01" for t in usage.turns)


def test_a_thread_at_or_after_the_cutoff_is_skipped_entirely():
    """The cutoff is what makes a non-idempotent script safe to run once.

    Live collection already counted 2026-08-15. Backfilling it too would double
    that day, and `ADD` has no undo.
    """
    usage = RecordingUsage()
    thread = StubThread()
    thread.created_at = "2026-08-15T09:00:00"

    assert backfill_thread(thread, usage, before="2026-08-15") == 0
    assert usage.turns == []


def test_a_thread_before_the_cutoff_is_backfilled():
    usage = RecordingUsage()
    thread = StubThread()
    thread.created_at = "2026-08-14T23:59:59"

    assert backfill_thread(thread, usage, before="2026-08-15") == 2


def test_no_cutoff_keeps_the_old_behaviour():
    usage = RecordingUsage()

    assert backfill_thread(StubThread(), usage) == 2
