"""A turn's team lands on its own counter partition; a policy denial has its own ledger."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.usage_service import UsageService  # noqa: E402


class StubRepo:
    def __init__(self):
        self.calls = []
        self.events = {}

    def add(self, pk, sk, counters, flags=None):
        self.calls.append((pk, sk, dict(counters)))

    def put_if_absent(self, pk, sk, item):
        if (pk, sk) in self.events:
            return False
        self.events[(pk, sk)] = dict(item)
        return True

    def get(self, pk, sk):
        return self.events.get((pk, sk))

    def set_fields(self, pk, sk, fields):
        self.events.setdefault((pk, sk), {}).update(fields)

    def query(self, pk, start_date, end_date):
        return [{"pk": p, "sk": s, **c} for p, s, c in self.calls if p == pk and f"D#{start_date}" <= s <= f"D#{end_date}￿"]

    def query_prefix(self, pk, sk_prefix):
        return [dict(v, pk=p, sk=s) for (p, s), v in self.events.items() if p == pk and s.startswith(sk_prefix)]


def turn(svc, **kw):
    base = dict(agent_record_id="rec-1", owner_sub="sub-1", input_tokens=10, output_tokens=5, tool_calls={}, date="2026-10-10")
    base.update(kw)
    return svc.record_turn(**base)


def test_team_turn_writes_the_team_partition_and_the_event_field():
    repo = StubRepo()
    svc = UsageService(repository=repo)
    turn(svc, team="finance", turn_id="t1")
    pks = {pk for pk, _, _ in repo.calls}
    assert "TEAMS#2026-10" in pks
    sk = next(sk for pk, sk, _ in repo.calls if pk == "TEAMS#2026-10")
    assert sk == "D#2026-10-10#G#finance"
    assert repo.events[("TURNS#2026-10", "T#t1")]["team"] == "finance"
    assert svc.team_totals("2026-10-01", "2026-10-31")["finance"]["turns"] == 1


def test_teamless_turn_writes_no_team_partition_and_no_team_field():
    repo = StubRepo()
    turn(UsageService(repository=repo), turn_id="t2")
    assert not any(pk.startswith("TEAMS#") for pk, _, _ in repo.calls)
    assert "team" not in repo.events[("TURNS#2026-10", "T#t2")]


def test_policy_denial_lands_on_event_agent_team_partitions():
    repo = StubRepo()
    svc = UsageService(repository=repo)
    svc.record_policy_denial(agent_record_id="rec-1", owner_sub="sub-1", team="finance",
                             tool_name="bap-platform-tools___lookup_salary", thread_id="th", turn_id="t3",
                             date="2026-10-10", at="2026-10-10T01:02:03+00:00")
    assert any(pk == "POLICY_EVENTS#2026-10" and sk.startswith("E#2026-10-10T01:02:03") for pk, sk in repo.events)
    assert ("POLICY#2026-10", "D#2026-10-10#A#rec-1") in {(pk, sk) for pk, sk, _ in repo.calls}
    team_counters = next(c for pk, sk, c in repo.calls if pk == "POLICY_TEAMS#2026-10")
    assert team_counters["denials"] == 1 and team_counters["tool_bap-platform-tools___lookup_salary"] == 1
    totals = svc.policy_team_totals("2026-10-01", "2026-10-31")
    assert totals["finance"]["denials"] == 1
    assert svc.policy_totals("2026-10-01", "2026-10-31")["denials"] == 1
    assert svc.policy_events("2026-10-01", "2026-10-31")[0]["tool_name"].endswith("lookup_salary")


def test_unattributed_denial_uses_the_dash_team_key():
    repo = StubRepo()
    svc = UsageService(repository=repo)
    svc.record_policy_denial(agent_record_id="rec-1", owner_sub="", team=None, tool_name="x", date="2026-10-10")
    assert any(sk == "D#2026-10-10#G#-" for pk, sk, _ in repo.calls if pk == "POLICY_TEAMS#2026-10")
