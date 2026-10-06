"""The turn event is the ledger's source of truth; counters derive from it.

One turn = one `TURNS#` item written with a conditional put, priced at write
time from the repository rate card. The four counter partitions the dashboard
reads are ADDed only after that put succeeded, so a turn that flushes twice is
counted once and a turn whose model is unknown is counted but never priced.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.usage_service import UsageService  # noqa: E402


class LedgerRepo:
    """In-memory table: put_if_absent honours existence, add accumulates, set overwrites."""

    def __init__(self):
        self.items = {}

    def put_if_absent(self, pk, sk, item):
        if (pk, sk) in self.items:
            return False
        self.items[(pk, sk)] = {"pk": pk, "sk": sk, **item}
        return True

    def add(self, pk, sk, counters, flags=None):
        row = self.items.setdefault((pk, sk), {"pk": pk, "sk": sk})
        for key, value in counters.items():
            if value:
                row[key] = row.get(key, 0) + value
        row.update(flags or {})

    def set_fields(self, pk, sk, fields):
        self.items.setdefault((pk, sk), {"pk": pk, "sk": sk}).update(fields)

    def get(self, pk, sk):
        return self.items.get((pk, sk))

    def query(self, pk, start, end):
        return [
            row for (p, s), row in self.items.items()
            if p == pk and f"D#{start}" <= s <= f"D#{end}￿"
        ]

    def query_prefix(self, pk, prefix):
        return [row for (p, s), row in self.items.items() if p == pk and s.startswith(prefix)]


def turn(svc, **overrides):
    base = dict(
        agent_record_id="rec-1", owner_sub="sub-1", input_tokens=1_000_000,
        output_tokens=0, tool_calls={}, date="2026-09-23", thread_id="t-1",
        turn_id="t-1:h-1", model_id="global.anthropic.claude-sonnet-5",
        started_at="2026-09-23T10:00:00Z", ended_at="2026-09-23T10:00:05Z",
    )
    base.update(overrides)
    return svc.record_turn(**base)


EVENT_KEY = ("TURNS#2026-09", "T#t-1:h-1")
AGENT_KEY = ("AGENTS#2026-09", "D#2026-09-23#A#rec-1")


def test_turn_event_is_written_with_cost_and_counters_derive_from_it():
    repo = LedgerRepo()
    svc = UsageService(repository=repo)

    out = turn(svc)

    assert out == {"event": "created", "cost_micros": 2_000_000}
    event = repo.get(*EVENT_KEY)
    assert event["model_cost_micros"] == 2_000_000
    assert event["routing"] == "global"
    assert event["status"] == "completed"
    assert event["measured"] is True
    agents = repo.get(*AGENT_KEY)
    assert agents["turns"] == 1
    assert agents["measured_turns"] == 1
    assert agents["model_cost_micros"] == 2_000_000
    assert agents["priced_turns"] == 1
    models = repo.get(
        "AGENT_MODELS#2026-09", "D#2026-09-23#A#rec-1#M#global.anthropic.claude-sonnet-5"
    )
    assert models["input_tokens"] == 1_000_000
    assert models["model_cost_micros"] == 2_000_000
    users = repo.get("USERS#2026-09", "D#2026-09-23#U#sub-1")
    assert users["turns"] == 1 and users["model_cost_micros"] == 2_000_000
    agent_users = repo.get("AGENT#rec-1#USERS#2026-09", "D#2026-09-23#U#sub-1")
    assert agent_users["model_cost_micros"] == 2_000_000


def test_second_flush_adds_tokens_but_not_turns():
    repo = LedgerRepo()
    svc = UsageService(repository=repo)
    turn(svc)

    out = turn(svc, input_tokens=0, output_tokens=100_000, turns=0)

    assert out["event"] == "updated"
    agents = repo.get(*AGENT_KEY)
    assert agents["turns"] == 1
    assert agents["output_tokens"] == 100_000
    assert agents["model_cost_micros"] == 3_000_000  # +$1.00 of output
    assert agents["priced_turns"] == 1
    event = repo.get(*EVENT_KEY)
    assert event["output_tokens"] == 100_000
    assert event["input_tokens"] == 1_000_000
    assert event["model_cost_micros"] == 3_000_000


def test_a_replayed_first_flush_with_turns_still_counts_one_turn():
    """Even if the caller passes turns=1 twice for one turn_id, the event's
    existence is what decides — the counter never sees a second turn."""
    repo = LedgerRepo()
    svc = UsageService(repository=repo)
    turn(svc)
    turn(svc, turns=1, thread_started=True)
    agents = repo.get(*AGENT_KEY)
    assert agents["turns"] == 1
    assert agents.get("threads_started", 0) == 0


def test_unknown_model_records_turn_without_cost():
    repo = LedgerRepo()
    svc = UsageService(repository=repo)

    out = turn(svc, model_id=None)

    assert out["cost_micros"] is None
    event = repo.get(*EVENT_KEY)
    assert "model_cost_micros" not in event
    agents = repo.get(*AGENT_KEY)
    assert agents["unpriced_turns"] == 1
    assert "model_cost_micros" not in agents
    assert "priced_turns" not in agents


def test_unregistered_model_is_kept_on_the_event_but_not_priced():
    repo = LedgerRepo()
    svc = UsageService(repository=repo)
    turn(svc, model_id="global.anthropic.claude-opus-5-5")
    event = repo.get(*EVENT_KEY)
    assert event["model_id"] == "global.anthropic.claude-opus-5-5"
    assert "model_cost_micros" not in event
    models = repo.get(
        "AGENT_MODELS#2026-09", "D#2026-09-23#A#rec-1#M#global.anthropic.claude-opus-5-5"
    )
    assert models["unpriced_turns"] == 1


def test_unmeasured_is_turns_minus_measured_only():
    repo = LedgerRepo()
    svc = UsageService(repository=repo)
    turn(svc, input_tokens=0, output_tokens=0)  # reported nothing
    turn(svc, turn_id="t-1:h-2", ended_at="2026-09-23T10:01:00Z")

    totals = svc.agent_totals("2026-09-23", "2026-09-23")["rec-1"]
    assert totals["turns"] == 2
    assert totals["unmeasured_turns"] == 1
    users = svc.user_totals("2026-09-23", "2026-09-23")["sub-1"]
    assert users["turns"] == 2
    assert users["unmeasured_turns"] == 1


def test_legacy_item_without_measured_turns_is_all_unmeasured():
    """No more heuristics: an item the backfill has not stamped is unmeasured
    until it is. A day carrying tokens but no `measured_turns` is a backfill
    defect to surface, not a fact to paper over."""
    repo = LedgerRepo()
    repo.items[AGENT_KEY] = {"pk": AGENT_KEY[0], "sk": AGENT_KEY[1], "turns": 3, "input_tokens": 10}
    svc = UsageService(repository=repo)
    assert svc.agent_totals("2026-09-23", "2026-09-23")["rec-1"]["unmeasured_turns"] == 3


def test_agent_models_groups_tokens_by_model():
    repo = LedgerRepo()
    svc = UsageService(repository=repo)
    turn(svc)
    turn(svc, turn_id="t-1:h-2", ended_at="2026-09-23T10:01:00Z",
         model_id="global.anthropic.claude-haiku-4-5-20251001-v1:0", input_tokens=10)
    by_model = svc.agent_models("2026-09-23", "2026-09-23")["rec-1"]
    assert set(by_model) == {
        "global.anthropic.claude-sonnet-5", "global.anthropic.claude-haiku-4-5-20251001-v1:0"
    }
    assert by_model["global.anthropic.claude-haiku-4-5-20251001-v1:0"]["model_cost_micros"] == 10


def test_turn_events_filter_by_thread_and_sort_by_time():
    repo = LedgerRepo()
    svc = UsageService(repository=repo)
    turn(svc, thread_id="t-2", turn_id="t-2:h-1", ended_at="2026-09-23T11:00:00Z")
    turn(svc)
    assert [e["thread_id"] for e in svc.turn_events("2026-09-23", "2026-09-23", thread_id="t-2")] == ["t-2"]
    assert [e["turn_id"] for e in svc.turn_events("2026-09-23", "2026-09-23")] == ["t-1:h-1", "t-2:h-1"]


def test_user_costs_sums_the_ledger_not_a_rate_table():
    repo = LedgerRepo()
    svc = UsageService(repository=repo)
    turn(svc)
    turn(svc, turn_id="t-1:h-2", ended_at="2026-09-23T10:01:00Z", model_id=None)
    costs = svc.user_costs("2026-09-23", "2026-09-23")["sub-1"]
    assert costs == {"model_cost_micros": 2_000_000, "priced_turns": 1, "unpriced_turns": 1}


def test_resolve_model_for_prefers_harness_and_caches():
    class Harness:
        def __init__(self):
            self.calls = 0

        def get_harness(self, harness_id):
            self.calls += 1
            assert harness_id == "h-1"
            return type("H", (), {"model_id": "global.anthropic.claude-sonnet-5"})()

    class Registry:
        def list_agent_runtimes(self):
            return [type("R", (), {"agent_runtime_arn": "arn:rt/x", "model_id": "us.anthropic.claude-haiku-4-5"})()]

    harness = Harness()
    svc = UsageService(repository=LedgerRepo(), registry=Registry(), harness=harness)
    arn = "arn:aws:bedrock-agentcore:us-east-1:1:harness/h-1"
    assert svc.resolve_model_for(harness_arn=arn, agent_runtime_arn="arn:rt/x") == "global.anthropic.claude-sonnet-5"
    assert svc.resolve_model_for(harness_arn=arn) == "global.anthropic.claude-sonnet-5"
    assert harness.calls == 1
    assert svc.resolve_model_for(agent_runtime_arn="arn:rt/x") == "us.anthropic.claude-haiku-4-5"
    assert svc.resolve_model_for() is None


def test_a_later_flush_with_a_different_ended_at_still_updates_the_same_event():
    """Measured live: the messageStop flush and the post-loop flush carry different
    `ended_at` values, and a sort key that embedded the time made them two events
    — the first unmeasured with zero tokens, the second measured — so the turn
    read as unmeasured with a cost. The key is the turn id alone."""
    repo = LedgerRepo()
    svc = UsageService(repository=repo)
    turn(svc, input_tokens=0, output_tokens=0, ended_at="2026-09-23T10:00:05Z")
    out = turn(svc, input_tokens=3893, output_tokens=88, turns=0, ended_at="2026-09-23T10:00:07Z")
    assert out["event"] == "updated"
    events = repo.query_prefix("TURNS#2026-09", "T#")
    assert len(events) == 1
    event = events[0]
    assert event["measured"] is True and event["ended_at"] == "2026-09-23T10:00:07Z"
    agents = repo.get(*AGENT_KEY)
    assert agents["turns"] == 1 and agents["measured_turns"] == 1 and agents["priced_turns"] == 1
    assert agents["model_cost_micros"] == 3893 * 2 + 88 * 10  # sonnet-5 global, micro-USD per token


def test_an_unmeasured_turn_with_a_known_model_is_neither_priced_nor_unpriced():
    """No tokens arrived, so there is no figure — not $0. `priced_turns` counts only
    turns whose cost is real; `unpriced_turns` stays "no rate"."""
    repo = LedgerRepo()
    svc = UsageService(repository=repo)
    turn(svc, input_tokens=0, output_tokens=0)
    agents = repo.get(*AGENT_KEY)
    assert agents["turns"] == 1
    assert agents.get("priced_turns", 0) == 0
    assert agents.get("unpriced_turns", 0) == 0
    assert agents.get("model_cost_micros", 0) == 0
    # The second flush brings the tokens: now it is priced.
    turn(svc, input_tokens=1_000_000, output_tokens=0, turns=0)
    agents = repo.get(*AGENT_KEY)
    assert agents["priced_turns"] == 1 and agents["measured_turns"] == 1
    assert agents["model_cost_micros"] == 2_000_000


def test_an_ending_that_arrives_on_the_second_flush_reaches_the_counters():
    repo = LedgerRepo()
    svc = UsageService(repository=repo)
    turn(svc)
    turn(svc, turns=0, interrupted=True, input_tokens=0)
    agents = repo.get(*AGENT_KEY)
    assert agents["interrupted_turns"] == 1
    assert repo.get(*EVENT_KEY)["status"] == "interrupted"
    # A repeat of the same ending is not counted twice.
    turn(svc, turns=0, interrupted=True, input_tokens=0)
    assert repo.get(*AGENT_KEY)["interrupted_turns"] == 1


def test_an_inference_profile_arn_is_resolved_to_its_foundation_model():
    class Bedrock:
        def get_inference_profile(self, inferenceProfileIdentifier):
            assert inferenceProfileIdentifier.endswith("application-inference-profile/abc")
            return {"models": [{"modelArn": "arn:aws:bedrock:us-east-1::foundation-model/global.anthropic.claude-sonnet-5"}]}

    class Harness:
        def get_harness(self, harness_id):
            return type("H", (), {"model_id": "arn:aws:bedrock:us-east-1:1:application-inference-profile/abc"})()

    svc = UsageService(repository=LedgerRepo(), harness=Harness(), bedrock=Bedrock())
    assert svc.resolve_model_for(harness_arn="arn:aws:bedrock-agentcore:us-east-1:1:harness/h-1") == "global.anthropic.claude-sonnet-5"


def test_a_failed_model_lookup_is_not_remembered_for_five_minutes():
    class FlakyHarness:
        def __init__(self):
            self.calls = 0

        def get_harness(self, harness_id):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("throttled")
            return type("H", (), {"model_id": "global.anthropic.claude-sonnet-5"})()

    harness = FlakyHarness()
    svc = UsageService(repository=LedgerRepo(), harness=harness)
    arn = "arn:aws:bedrock-agentcore:us-east-1:1:harness/h-1"
    assert svc.resolve_model_for(harness_arn=arn) is None
    assert svc.resolve_model_for(harness_arn=arn) == "global.anthropic.claude-sonnet-5"
    assert harness.calls == 2


def test_the_month_partition_follows_started_at_so_a_midnight_turn_stays_one_event():
    repo = LedgerRepo()
    svc = UsageService(repository=repo)
    turn(svc, date=None, started_at="2026-09-30T23:59:50Z", ended_at="2026-09-30T23:59:59Z", input_tokens=0)
    turn(svc, date=None, started_at="2026-09-30T23:59:50Z", ended_at="2026-10-01T00:00:02Z", turns=0)
    assert repo.query_prefix("TURNS#2026-09", "T#") and not repo.query_prefix("TURNS#2026-10", "T#")
    agents = repo.get("AGENTS#2026-09", "D#2026-09-30#A#rec-1")
    assert agents["turns"] == 1 and agents["input_tokens"] == 1_000_000


def test_a_reported_zero_is_measured_and_a_block_is_its_own_status():
    """A turn the guardrail stopped before the model ran reports usage of zero.
    That zero is a figure: the turn is measured, its cost is $0, and its status
    says why — it must not sit in the unmeasured count with pre-ledger history."""
    repo = LedgerRepo()
    svc = UsageService(repo)
    turn(svc, input_tokens=0, usage_reported=True, blocked=True)
    event = repo.get("TURNS#2026-09", "T#t-1:h-1")
    assert event["measured"] is True and event["status"] == "blocked"
    assert event["model_cost_micros"] == 0
    day = repo.get("AGENTS#2026-09", "D#2026-09-23#A#rec-1")
    assert day["turns"] == 1 and day["measured_turns"] == 1 and day["priced_turns"] == 1
    assert svc.agent_totals("2026-09-23", "2026-09-23")["rec-1"]["unmeasured_turns"] == 0


def test_a_zero_report_that_arrives_on_the_second_flush_still_measures_the_turn():
    """The blocked turn's metadata comes after messageStop, so the first flush
    wrote an unmeasured event; the second flush carries the report."""
    repo = LedgerRepo()
    svc = UsageService(repo)
    turn(svc, input_tokens=0)
    assert repo.get("TURNS#2026-09", "T#t-1:h-1")["measured"] is False
    turn(svc, input_tokens=0, turns=0, usage_reported=True, blocked=True)
    event = repo.get("TURNS#2026-09", "T#t-1:h-1")
    assert event["measured"] is True and event["status"] == "blocked"
    day = repo.get("AGENTS#2026-09", "D#2026-09-23#A#rec-1")
    assert day["turns"] == 1 and day["measured_turns"] == 1


def test_an_unreported_zero_stays_unmeasured():
    repo = LedgerRepo()
    svc = UsageService(repo)
    turn(svc, input_tokens=0)
    assert repo.get("TURNS#2026-09", "T#t-1:h-1")["measured"] is False


class _Record:
    def __init__(self, record_id, harness_arn=None, agent_runtime_arn=None):
        self.record_id = record_id
        self.harness_arn = harness_arn
        self.agent_runtime_arn = agent_runtime_arn


def test_a_deployed_fallback_id_folds_into_the_record_that_owns_the_arn():
    """A turn served through the `deployed:<arn>` fallback is the same agent as
    the registry record for that ARN, and every read says so — one leaderboard
    row, one detail page, one set of turn events."""
    repo = LedgerRepo()
    svc = UsageService(repo)
    arn = "arn:aws:bedrock-agentcore:us-east-1:1:harness/web_harness-x"
    turn(svc, agent_record_id="rec-1", turn_id="t-1:h-1", tool_calls={"a": 1})
    turn(svc, agent_record_id=f"deployed:{arn}", owner_sub="sub-2", thread_id="t-2",
         turn_id="t-2:h-1", tool_calls={"b": 2})
    svc.set_record_aliases([_Record("rec-1", harness_arn=arn)])
    totals = svc.agent_totals("2026-09-23", "2026-09-23")
    assert set(totals) == {"rec-1"} and totals["rec-1"]["turns"] == 2
    assert svc.distinct_users("rec-1", "2026-09-23", "2026-09-23") == 2
    assert svc.tool_totals("rec-1", "2026-09-23", "2026-09-23") == {"a": 1, "b": 2}
    assert len(svc.turn_events("2026-09-23", "2026-09-23", agent_record_id="rec-1")) == 2
    assert list(svc.agent_models("2026-09-23", "2026-09-23")) == ["rec-1"]
    assert svc.daily_totals("2026-09-23", "2026-09-23", record_id="rec-1")[0]["turns"] == 2


def test_reprice_events_prices_an_unpriced_turn_once_its_rate_is_learned_and_moves_the_counters():
    from data.model_rates import set_learned_rates

    repo = LedgerRepo()
    svc = UsageService(repo)
    try:
        turn(svc, model_id="global.anthropic.claude-opus-5-5", input_tokens=1_000_000, output_tokens=100_000)
        event = repo.get("TURNS#2026-09", "T#t-1:h-1")
        day = repo.get("AGENTS#2026-09", "D#2026-09-23#A#rec-1")
        assert "model_cost_micros" not in event and day["unpriced_turns"] == 1 and day.get("priced_turns", 0) == 0
        # The bill teaches the rate two days later, effective from before the turn.
        repo.set_fields("RATES#learned", "R#claude-opus-5-5|global|input|2026-09-22",
                        {"family": "claude-opus-5-5", "routing": "global", "tier": "input",
                         "effective_from": "2026-09-22", "usd_per_1m": "7.5"})
        repo.set_fields("RATES#learned", "R#claude-opus-5-5|global|output|2026-09-22",
                        {"family": "claude-opus-5-5", "routing": "global", "tier": "output",
                         "effective_from": "2026-09-22", "usd_per_1m": "37.5"})
        report = svc.reprice_events("2026-09-01", "2026-09-30")
        assert report == {"repriced": 1, "newly_priced": 1, "still_unpriced": 0, "unpriced": 0}
        event = repo.get("TURNS#2026-09", "T#t-1:h-1")
        assert event["model_cost_micros"] == 7_500_000 + 3_750_000
        assert event["rate_card_version"].endswith("+ce:2026-09-22")
        day = repo.get("AGENTS#2026-09", "D#2026-09-23#A#rec-1")
        assert day["model_cost_micros"] == 11_250_000 and day["priced_turns"] == 1 and day["unpriced_turns"] == 0
        user = repo.get("USERS#2026-09", "D#2026-09-23#U#sub-1")
        assert user["model_cost_micros"] == 11_250_000
        by_model = repo.get("AGENT_MODELS#2026-09", "D#2026-09-23#A#rec-1#M#global.anthropic.claude-opus-5-5")
        assert by_model["model_cost_micros"] == 11_250_000 and by_model["priced_turns"] == 1
        # Nothing changes on a second pass.
        assert svc.reprice_events("2026-09-01", "2026-09-30") == {"repriced": 0, "newly_priced": 0, "still_unpriced": 0, "unpriced": 0}
    finally:
        set_learned_rates([])


def test_reprice_events_moves_a_priced_turn_to_a_price_change_effective_on_its_day():
    from data.model_rates import set_learned_rates

    repo = LedgerRepo()
    svc = UsageService(repo)
    try:
        turn(svc, input_tokens=0, output_tokens=1_000_000)  # Sonnet 5 output at the card's $10.00
        assert repo.get("TURNS#2026-09", "T#t-1:h-1")["model_cost_micros"] == 10_000_000
        repo.set_fields("RATES#learned", "R#claude-sonnet-5|global|output|2026-09-23",
                        {"family": "claude-sonnet-5", "routing": "global", "tier": "output",
                         "effective_from": "2026-09-23", "usd_per_1m": "8"})
        report = svc.reprice_events("2026-09-23", "2026-09-23")
        assert report["repriced"] == 1 and report["newly_priced"] == 0
        assert repo.get("TURNS#2026-09", "T#t-1:h-1")["model_cost_micros"] == 8_000_000
        day = repo.get("AGENTS#2026-09", "D#2026-09-23#A#rec-1")
        assert day["model_cost_micros"] == 8_000_000 and day["priced_turns"] == 1
    finally:
        set_learned_rates([])


def test_a_turn_using_a_tier_the_learned_rate_lacks_stays_unpriced():
    from data.model_rates import set_learned_rates

    repo = LedgerRepo()
    svc = UsageService(repo)
    try:
        repo.set_fields("RATES#learned", "R#claude-opus-5-5|global|output|2026-09-01",
                        {"family": "claude-opus-5-5", "routing": "global", "tier": "output",
                         "effective_from": "2026-09-01", "usd_per_1m": "37.5"})
        svc.load_learned_rates()
        turn(svc, model_id="global.anthropic.claude-opus-5-5", input_tokens=10, output_tokens=5)
        event = repo.get("TURNS#2026-09", "T#t-1:h-1")
        day = repo.get("AGENTS#2026-09", "D#2026-09-23#A#rec-1")
        assert "model_cost_micros" not in event and event["measured"] is True
        assert day["unpriced_turns"] == 1 and day.get("priced_turns", 0) == 0
    finally:
        set_learned_rates([])
