"""One turn must land on every counter the dashboard will ask about.

The service is deliberately tolerant: an unconfigured table or a missing agent id
must not raise, because this runs inside the chat stream and losing a turn's
answer to protect a counter is the wrong trade.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.usage_service import UsageService  # noqa: E402


class StubRepo:
    """Records counter ADDs; accepts the turn event so the counters follow."""

    def __init__(self):
        self.calls = []
        self.events = {}

    def add(self, pk, sk, counters, flags=None):
        self.calls.append((pk, sk, dict(counters), dict(flags or {})))

    def put_if_absent(self, pk, sk, item):
        if (pk, sk) in self.events:
            return False
        self.events[(pk, sk)] = dict(item)
        return True

    def get(self, pk, sk):
        return self.events.get((pk, sk))

    def set_fields(self, pk, sk, fields):
        self.events.setdefault((pk, sk), {}).update(fields)


def keys(repo):
    return {(pk, sk) for pk, sk, _, _ in repo.calls}


def counters_for(repo, pk, sk):
    for call_pk, call_sk, counters, _ in repo.calls:
        if (call_pk, call_sk) == (pk, sk):
            return counters
    raise AssertionError(f"no write to {pk} / {sk}: {keys(repo)}")


def service_with(repo):
    return UsageService(repository=repo)


def test_a_turn_writes_all_four_entities():
    repo = StubRepo()
    service_with(repo).record_turn(
        agent_record_id="rec-1",
        owner_sub="sub-1",
        input_tokens=100,
        output_tokens=20,
        tool_calls={"WebSearch": 2},
        date="2026-08-15",
    )

    assert keys(repo) == {
        ("AGENTS#2026-08", "D#2026-08-15#A#rec-1"),
        ("USERS#2026-08", "D#2026-08-15#U#sub-1"),
        ("AGENT#rec-1#USERS#2026-08", "D#2026-08-15#U#sub-1"),
        ("AGENT#rec-1#TOOLS#2026-08", "D#2026-08-15#T#WebSearch"),
    }


def test_agent_counters_carry_tokens_turns_and_tool_calls():
    repo = StubRepo()
    service_with(repo).record_turn(
        agent_record_id="rec-1",
        owner_sub="sub-1",
        input_tokens=100,
        output_tokens=20,
        tool_calls={"WebSearch": 2, "Calculate": 1},
        date="2026-08-15",
    )

    counters = counters_for(repo, "AGENTS#2026-08", "D#2026-08-15#A#rec-1")
    assert counters["input_tokens"] == 100
    assert counters["output_tokens"] == 20
    assert counters["turns"] == 1
    assert counters["tool_calls"] == 3


def test_an_interrupted_turn_is_counted_separately():
    """A turn routinely abandoned is a finding, not noise."""
    repo = StubRepo()
    service_with(repo).record_turn(
        agent_record_id="rec-1", owner_sub="sub-1",
        input_tokens=10, output_tokens=1, tool_calls={},
        interrupted=True, date="2026-08-15",
    )

    counters = counters_for(repo, "AGENTS#2026-08", "D#2026-08-15#A#rec-1")
    assert counters["interrupted_turns"] == 1
    assert counters["turns"] == 1


def test_each_tool_gets_its_own_counter():
    repo = StubRepo()
    service_with(repo).record_turn(
        agent_record_id="rec-1", owner_sub="sub-1",
        input_tokens=10, output_tokens=1,
        tool_calls={"WebSearch": 2, "Calculate": 1}, date="2026-08-15",
    )

    assert ("AGENT#rec-1#TOOLS#2026-08", "D#2026-08-15#T#WebSearch") in keys(repo)
    assert ("AGENT#rec-1#TOOLS#2026-08", "D#2026-08-15#T#Calculate") in keys(repo)
    assert counters_for(
        repo, "AGENT#rec-1#TOOLS#2026-08", "D#2026-08-15#T#WebSearch"
    )["tool_calls"] == 2


def test_an_unconfigured_table_is_silent_not_fatal():
    UsageService(repository=None).record_turn(
        agent_record_id="rec-1", owner_sub="sub-1",
        input_tokens=10, output_tokens=1, tool_calls={}, date="2026-08-15",
    )


def test_a_repository_failure_never_reaches_the_caller():
    """This runs inside the chat stream. A lost counter beats a lost answer."""

    class Exploding:
        def add(self, *args, **kwargs):
            raise RuntimeError("throttled")

    UsageService(repository=Exploding()).record_turn(
        agent_record_id="rec-1", owner_sub="sub-1",
        input_tokens=10, output_tokens=1, tool_calls={}, date="2026-08-15",
    )


def test_a_turn_with_no_agent_id_is_dropped():
    """Nothing to attribute it to; a counter under "" is worse than no counter."""
    repo = StubRepo()
    service_with(repo).record_turn(
        agent_record_id="", owner_sub="sub-1",
        input_tokens=10, output_tokens=1, tool_calls={}, date="2026-08-15",
    )

    assert repo.calls == []


# Test for the agent-user counter token carries requirement
class RecordingRepo:
    def __init__(self):
        self.writes = []
        self.events = {}

    def add(self, pk, sk, counters):
        self.writes.append((pk, sk, dict(counters)))

    def put_if_absent(self, pk, sk, item):
        if (pk, sk) in self.events:
            return False
        self.events[(pk, sk)] = dict(item)
        return True

    def get(self, pk, sk):
        return self.events.get((pk, sk))

    def set_fields(self, pk, sk, fields):
        self.events.setdefault((pk, sk), {}).update(fields)

    def query(self, pk, s, e):
        return []


def test_agent_user_counter_carries_tokens():
    repo = RecordingRepo()
    svc = UsageService(repository=repo)
    svc.record_turn(
        agent_record_id="rec-1", owner_sub="sub-1",
        input_tokens=100, output_tokens=20,
        cache_read_tokens=5, cache_write_tokens=1,
        tool_calls={}, turns=1, date="2026-08-21",
    )
    au = next(c for pk, sk, c in repo.writes if pk.startswith("AGENT#rec-1#USERS#"))
    assert au["input_tokens"] == 100 and au["output_tokens"] == 20
    assert au["cache_read_tokens"] == 5 and au["cache_write_tokens"] == 1
    assert au["measured_turns"] == 1 and au["turns"] == 1


