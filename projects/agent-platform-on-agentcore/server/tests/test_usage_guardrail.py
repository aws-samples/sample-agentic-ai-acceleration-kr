import os, sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Patch month_shard before importing UsageService, since conftest mocks it.
def mock_month_shard(day):
    """Convert YYYY-MM-DD to YYYY-MM."""
    return day[:7]

with patch('services.usage_service.month_shard', side_effect=mock_month_shard):
    from services.usage_service import UsageService

class Repo:
    def __init__(self): self.rows = []; self.items = {}
    def add(self, pk, sk, counters):
        self.rows.append({"pk": pk, "sk": sk, **{k: int(v) for k, v in counters.items()}})
    def query(self, pk, s, e):
        return [r for r in self.rows if r["pk"] == pk and s <= r["sk"].split("#")[1] <= e]
    def put_if_absent(self, pk, sk, item):
        if (pk, sk) in self.items: return False
        self.items[(pk, sk)] = dict(item); return True
    def query_prefix(self, pk, sk_prefix):
        return [dict(v) for (p, k), v in self.items.items() if p == pk and k.startswith(sk_prefix)]

def test_records_and_rolls_up_a_block():
    with patch('services.usage_service.month_shard', side_effect=mock_month_shard):
        repo = Repo(); svc = UsageService(repository=repo)
        svc.record_guardrail_event("rec-1", "sub-1", "BLOCKED", "input",
                                   ["pii", "content"], date="2026-08-21")
        agg = svc.guardrail_totals("2026-08-01", "2026-08-31")["rec-1"]
        assert agg["interventions"] == 1
        assert agg["blocked_input"] == 1 and agg["blocked_output"] == 0
        assert agg["policy_pii"] == 1 and agg["policy_content"] == 1 and agg["policy_word"] == 0
        usr = svc.guardrail_user_totals("2026-08-01", "2026-08-31")["sub-1"]
        assert usr["blocked_input"] == 1


def test_filter_types_and_confidences_become_named_counters():
    """Which filter fired and how sure Bedrock was — the two facts a policy tuner
    needs that the policy family alone cannot give. Labels only, never text."""
    with patch('services.usage_service.month_shard', side_effect=mock_month_shard):
        repo = Repo(); svc = UsageService(repository=repo)
        svc.record_guardrail_event("rec-1", "sub-1", "BLOCKED", "input", ["content"],
                                   filter_types=["INSULTS"], confidences=["HIGH"], date="2026-08-21")
        svc.record_guardrail_event("rec-1", "sub-1", "BLOCKED", "input", ["content"],
                                   filter_types=["PROMPT_ATTACK", "INSULTS"], confidences=["LOW", "HIGH"],
                                   date="2026-08-22")
        agg = svc.guardrail_totals("2026-08-01", "2026-08-31")["rec-1"]
        assert agg["interventions"] == 2
        assert agg["by_filter"] == {"INSULTS": 2, "PROMPT_ATTACK": 1}
        assert agg["by_confidence"] == {"HIGH": 2, "LOW": 1}
        usr = svc.guardrail_user_totals("2026-08-01", "2026-08-31")["sub-1"]
        assert usr["by_filter"] == {"INSULTS": 2, "PROMPT_ATTACK": 1}


def test_events_without_filter_detail_still_count():
    with patch('services.usage_service.month_shard', side_effect=mock_month_shard):
        repo = Repo(); svc = UsageService(repository=repo)
        svc.record_guardrail_event("rec-1", "sub-1", "BLOCKED", "input", ["content"], date="2026-08-21")
        agg = svc.guardrail_totals("2026-08-01", "2026-08-31")["rec-1"]
        assert agg["interventions"] == 1
        assert agg["by_filter"] == {} and agg["by_confidence"] == {}


def test_a_scanned_turn_is_counted_apart_from_interventions():
    """Zero interventions on a guarded agent and zero on an unguarded one look the
    same until the scan itself is counted. This is what tells them apart."""
    with patch('services.usage_service.month_shard', side_effect=mock_month_shard):
        repo = Repo(); svc = UsageService(repository=repo)
        svc.record_guardrail_scan("rec-1", "sub-1", date="2026-08-21")
        svc.record_guardrail_scan("rec-1", "sub-1", date="2026-08-21")
        agg = svc.guardrail_totals("2026-08-01", "2026-08-31")["rec-1"]
        assert agg["scanned_turns"] == 2
        assert agg["interventions"] == 0
        assert svc.guardrail_user_totals("2026-08-01", "2026-08-31")["sub-1"]["scanned_turns"] == 2


def test_daily_guardrail_series_is_keyed_by_day():
    with patch('services.usage_service.month_shard', side_effect=mock_month_shard):
        repo = Repo(); svc = UsageService(repository=repo)
        svc.record_guardrail_scan("rec-1", "sub-1", date="2026-08-21")
        svc.record_guardrail_event("rec-1", "sub-1", "BLOCKED", "input", ["content"], date="2026-08-21")
        svc.record_guardrail_event("rec-2", "sub-2", "ANONYMIZED", "output", ["pii"], date="2026-08-23")
        daily = svc.guardrail_daily_totals("2026-08-20", "2026-08-24")
        assert daily == {
            "2026-08-21": {"interventions": 1, "scanned_turns": 1},
            "2026-08-23": {"interventions": 1, "scanned_turns": 0},
        }


def test_guardrail_sum_merges_flat_counters_and_breakdowns():
    from services.usage_service import guardrail_sum
    a = {"interventions": 2, "blocked_input": 2, "scanned_turns": 5,
         "by_filter": {"INSULTS": 2}, "by_confidence": {"HIGH": 2}}
    b = {"interventions": 1, "blocked_output": 1, "scanned_turns": 3,
         "by_filter": {"INSULTS": 1, "HATE": 1}, "by_confidence": {"LOW": 1}}
    total = guardrail_sum([a, b])
    assert total["interventions"] == 3 and total["blocked_input"] == 2 and total["blocked_output"] == 1
    assert total["scanned_turns"] == 8
    assert total["by_filter"] == {"INSULTS": 3, "HATE": 1}
    assert total["by_confidence"] == {"HIGH": 2, "LOW": 1}
    assert total["anonymized_input"] == 0
    assert guardrail_sum([])["interventions"] == 0


def test_agent_turns_since_counts_only_days_from_the_given_one():
    """Scan counting started on a day; turns before it say nothing about whether
    the agent was guarded, so the unguarded verdict must only weigh turns since."""
    with patch('services.usage_service.month_shard', side_effect=mock_month_shard):
        repo = Repo(); svc = UsageService(repository=repo)
        repo.add("AGENTS#2026-08", "D#2026-08-20#A#rec-1", {"turns": 5})
        repo.add("AGENTS#2026-08", "D#2026-08-22#A#rec-1", {"turns": 2})
        repo.add("AGENTS#2026-08", "D#2026-08-22#A#rec-2", {"turns": 1})
        since = svc.agent_turns_since("2026-08-01", "2026-08-31", "2026-08-21")
        assert since == {"rec-1": 2, "rec-2": 1}


# --- the event behind the counter: which turn, whose, in which conversation ----


def test_an_intervention_leaves_an_event_naming_the_turn_and_its_owner():
    """A count says the guardrail acted; only an event can say on what. The row
    carries the labels the counters carry plus the thread and turn, so an admin
    can open the conversation that tripped it. Still no matched text."""
    with patch('services.usage_service.month_shard', side_effect=mock_month_shard):
        repo = Repo(); svc = UsageService(repository=repo)
        svc.record_guardrail_event("rec-1", "sub-1", "BLOCKED", "input", ["content"],
                                   filter_types=["INSULTS"], confidences=["HIGH"],
                                   thread_id="t-1", turn_id="t-1:m-9",
                                   at="2026-08-21T10:00:00Z", date="2026-08-21")
        [event] = svc.guardrail_events("2026-08-01", "2026-08-31")
        assert event["thread_id"] == "t-1" and event["turn_id"] == "t-1:m-9"
        assert event["agent_record_id"] == "rec-1" and event["owner_sub"] == "sub-1"
        assert event["action"] == "BLOCKED" and event["stage"] == "input"
        assert event["filter_types"] == ["INSULTS"] and event["confidences"] == ["HIGH"]
        assert event["at"] == "2026-08-21T10:00:00Z"


def test_events_read_newest_first_within_the_window_only():
    with patch('services.usage_service.month_shard', side_effect=mock_month_shard):
        repo = Repo(); svc = UsageService(repository=repo)
        for day, hour in (("2026-08-20", "09"), ("2026-08-22", "11"), ("2026-08-22", "08"), ("2026-09-01", "07")):
            svc.record_guardrail_event("rec-1", "sub-1", "BLOCKED", "input", ["content"],
                                       thread_id=f"t-{day}-{hour}", at=f"{day}T{hour}:00:00Z", date=day)
        events = svc.guardrail_events("2026-08-21", "2026-08-31")
        assert [e["thread_id"] for e in events] == ["t-2026-08-22-11", "t-2026-08-22-08"]


def test_two_interventions_in_one_turn_are_two_events():
    """Input and output can each intervene on the same turn; the key must not
    collapse them onto one row."""
    with patch('services.usage_service.month_shard', side_effect=mock_month_shard):
        repo = Repo(); svc = UsageService(repository=repo)
        svc.record_guardrail_event("rec-1", "sub-1", "BLOCKED", "input", ["content"],
                                   thread_id="t-1", turn_id="t-1:m-1", at="2026-08-21T10:00:00Z", date="2026-08-21")
        svc.record_guardrail_event("rec-1", "sub-1", "ANONYMIZED", "output", ["pii"],
                                   thread_id="t-1", turn_id="t-1:m-1", at="2026-08-21T10:00:00Z", date="2026-08-21")
        assert len(svc.guardrail_events("2026-08-01", "2026-08-31")) == 2


def test_event_limit_keeps_the_newest():
    with patch('services.usage_service.month_shard', side_effect=mock_month_shard):
        repo = Repo(); svc = UsageService(repository=repo)
        for hour in ("08", "09", "10"):
            svc.record_guardrail_event("rec-1", "sub-1", "BLOCKED", "input", ["content"],
                                       thread_id=f"t-{hour}", at=f"2026-08-21T{hour}:00:00Z", date="2026-08-21")
        assert [e["thread_id"] for e in svc.guardrail_events("2026-08-01", "2026-08-31", limit=2)] == ["t-10", "t-09"]
