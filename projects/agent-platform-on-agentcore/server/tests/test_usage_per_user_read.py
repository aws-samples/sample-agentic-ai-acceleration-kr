"""`user_totals_for` (one person, exact-key BatchGet) answers what
`user_totals()[sub]` (every person's rows) answers, without reading the others.

/insights/me read the whole USERS# month partition to return one person: 3-4.5 s
at 1,000 people (2026-10-18). Pinned here across a month boundary, with failed and
interrupted turns, and for someone with no turns at all.
"""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.usage_service import UsageService  # noqa: E402


class SummingRepo:
    """ADDs accumulate per key, like DynamoDB; reads by range or by exact key."""

    def __init__(self):
        self.rows, self.events, self.batches = {}, {}, []

    def add(self, pk, sk, counters, flags=None):
        row = self.rows.setdefault((pk, sk), {})
        for name, value in counters.items():
            if value:
                row[name] = row.get(name, 0) + value

    def put_if_absent(self, pk, sk, item):
        if (pk, sk) in self.events:
            return False
        self.events[(pk, sk)] = dict(item)
        return True

    def get(self, pk, sk):
        return self.events.get((pk, sk))

    def set_fields(self, pk, sk, fields):
        self.events.setdefault((pk, sk), {}).update(fields)

    def query(self, pk, start, end):
        return [dict(v, pk=p, sk=s) for (p, s), v in self.rows.items()
                if p == pk and f"D#{start}" <= s <= f"D#{end}￿"]

    def query_prefix(self, pk, prefix):
        return []

    def get_many(self, keys):
        self.batches.append(list(keys))
        return [dict(self.rows[k], pk=k[0], sk=k[1]) for k in keys if k in self.rows]


def test_one_person_matches_the_everyone_read():
    rnd = random.Random(5)
    repo = SummingRepo()
    svc = UsageService(repository=repo)
    days = [f"2026-09-{d:02d}" for d in range(20, 31)] + [f"2026-10-{d:02d}" for d in range(1, 12)]
    for n in range(400):
        svc.record_turn(
            agent_record_id=rnd.choice(["rec-1", "rec-2"]), owner_sub=rnd.choice(["a", "b", "c"]),
            input_tokens=rnd.randint(0, 900), output_tokens=rnd.randint(0, 90), tool_calls={"x": rnd.randint(0, 2)},
            cache_read_tokens=rnd.randint(0, 50), date=rnd.choice(days), turn_id=f"t{n}",
            failed=rnd.random() < 0.05, interrupted=rnd.random() < 0.05, model_calls=1,
        )
    everyone = svc.user_totals("2026-09-25", "2026-10-08")
    for sub in ("a", "b", "c", "nobody"):
        assert svc.user_totals_for(sub, "2026-09-25", "2026-10-08") == everyone.get(sub, {})
    # One batch per call, spanning both months.
    assert {pk for pk, _ in repo.batches[0]} == {"USERS#2026-09", "USERS#2026-10"}


def test_one_persons_days_and_agents_match_the_everyone_reads():
    rnd = random.Random(7)
    repo = SummingRepo()
    svc = UsageService(repository=repo)
    days = [f"2026-09-{d:02d}" for d in range(25, 31)] + [f"2026-10-{d:02d}" for d in range(1, 6)]
    for n in range(200):
        svc.record_turn(
            agent_record_id=rnd.choice(["rec-1", "rec-2"]), owner_sub=rnd.choice(["a", "b"]),
            input_tokens=rnd.randint(1, 900), output_tokens=rnd.randint(1, 90), date=rnd.choice(days),
            tool_calls={}, turn_id=f"t{n}", failed=rnd.random() < 0.1, model_calls=1,
        )
    start, end = "2026-09-27", "2026-10-04"
    for sub in ("a", "nobody"):
        assert svc.user_daily_for(sub, start, end) == svc.user_daily(start, end).get(sub, {})
    repo.batches.clear()
    assert svc.user_agents_for("a", ["rec-1", "rec-2"], start, end) == svc.agent_user_matrix(["rec-1", "rec-2"], start, end)["a"]
    # Exact keys only: one batch for every (agent, day), no partition reads of everyone.
    assert len(repo.batches) == 1 and all(sk.endswith("#U#a") for _, sk in repo.batches[0])
