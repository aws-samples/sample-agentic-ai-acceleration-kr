"""The backfill rebuilds history exactly where it can and honestly where it cannot.

Turns come from the stored threads (one per human message, timed by the AI
message id's epoch millis); models from the runtime/harness version history;
tokens stay at day granularity because that is all the old counters hold. Every
write is a conditional put or a SET, so running twice changes nothing.
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.backfill_ledger import (  # noqa: E402
    day_split,
    model_at,
    run,
    turns_from_thread,
)


def thread(**overrides):
    base = dict(
        thread_id="t", agent_record_id="rec-1", owner_sub="sub-1",
        created_at="2026-09-20T09:00:00", updated_at="2026-09-20T09:10:00",
        values={"messages": [
            {"id": "h1", "type": "human", "content": "q1"},
            {"id": "msg-1790152485051", "type": "ai", "content": "a1"},   # 2026-09-23T08:34:45Z
            {"id": "h2", "type": "human", "content": "q2"},
        ]},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_turns_from_thread_uses_next_ai_message_time():
    turns = turns_from_thread(thread())
    assert turns[0]["turn_id"] == "t:h1"
    assert turns[0]["ended_at"].startswith("2026-09-23T08:34:45")
    assert turns[0]["status"] == "completed"
    assert turns[0]["thread_id"] == "t" and turns[0]["owner_sub"] == "sub-1"
    assert turns[1]["turn_id"] == "t:h2"
    assert turns[1]["status"] == "unknown"
    # No AI message after it: the thread's last write is the best time known.
    assert turns[1]["ended_at"].startswith("2026-09-20T09:10:00")


def test_turns_without_any_ai_message_use_created_at():
    turns = turns_from_thread(thread(values={"messages": [{"id": "h1", "type": "human"}]}))
    assert turns[0]["ended_at"].startswith("2026-09-20T09:10:00")


def test_model_at_picks_latest_version_before_time():
    timeline = [("2026-08-21T05:10:47", "A"), ("2026-08-22T04:38:24", "B")]
    assert model_at(timeline, "2026-08-22T03:00:00") == "A"
    assert model_at(timeline, "2026-08-22T05:00:00") == "B"
    assert model_at(timeline, "2026-08-01T00:00:00") is None
    assert model_at([], "2026-08-22T05:00:00") is None


def test_day_split_counts_turns_either_side_of_a_switch():
    before, after = day_split(
        ["2026-08-22T03:00:00", "2026-08-22T05:00:00", "2026-08-22T06:00:00"], "2026-08-22T04:38:24"
    )
    assert (before, after) == (1, 2)


class Repo:
    def __init__(self, items=None):
        self.items = dict(items or {})

    def put_if_absent(self, pk, sk, item):
        if (pk, sk) in self.items:
            return False
        self.items[(pk, sk)] = {"pk": pk, "sk": sk, **item}
        return True

    def set_fields(self, pk, sk, fields):
        self.items.setdefault((pk, sk), {"pk": pk, "sk": sk}).update(fields)

    def get(self, pk, sk):
        return self.items.get((pk, sk))

    def delete(self, pk, sk):
        self.items.pop((pk, sk), None)

    def add(self, pk, sk, counters, flags=None):
        row = self.items.setdefault((pk, sk), {"pk": pk, "sk": sk})
        for key, value in counters.items():
            if value:
                row[key] = int(row.get(key, 0)) + value

    def query(self, pk, start, end):
        return [r for (p, s), r in self.items.items() if p == pk and f"D#{start}" <= s <= f"D#{end}￿"]

    def query_prefix(self, pk, prefix):
        return [r for (p, s), r in self.items.items() if p == pk and s.startswith(prefix)]


def world():
    """Two agents, three threads, legacy counters the live stream wrote:
    the agent ledger lost a turn, the user ledger did not."""
    repo = Repo({
        ("AGENTS#2026-09", "D#2026-09-20#A#rec-1"): {"pk": "AGENTS#2026-09", "sk": "D#2026-09-20#A#rec-1",
                                                      "turns": 1, "input_tokens": 1_000_000, "output_tokens": 0},
        ("USERS#2026-09", "D#2026-09-20#U#sub-1"): {"pk": "USERS#2026-09", "sk": "D#2026-09-20#U#sub-1",
                                                    "turns": 2, "input_tokens": 1_000_000},
        ("AGENT#rec-1#USERS#2026-09", "D#2026-09-20#U#sub-1"): {"pk": "AGENT#rec-1#USERS#2026-09",
                                                                 "sk": "D#2026-09-20#U#sub-1", "turns": 2,
                                                                 "input_tokens": 1_000_000},
    })
    threads = [
        thread(thread_id="t1", values={"messages": [
            {"id": "h1", "type": "human"}, {"id": "msg-1789894800000", "type": "ai"},   # 2026-09-20T09:00:00Z
            {"id": "h2", "type": "human"}, {"id": "msg-1789898400000", "type": "ai"},   # 2026-09-20T10:00:00Z
        ]}),
        thread(thread_id="t2", agent_record_id="rec-2", owner_sub="sub-2",
               created_at="2026-09-21T01:00:00", updated_at="2026-09-21T01:05:00",
               values={"messages": [{"id": "h9", "type": "human"}, {"id": "msg-1789952400000", "type": "ai"}]}),  # 09-21T01:00Z
    ]
    timelines = {"rec-1": [("2026-08-01T00:00:00", "global.anthropic.claude-sonnet-5")], "rec-2": []}
    return {"usage_repo": repo, "threads": threads, "timelines": timelines,
            "today": "2026-09-23", "human_messages": 3}


def snapshot(repo):
    return {k: dict(v) for k, v in repo.items.items()}


def test_backfill_writes_turn_events_and_corrects_the_counters():
    w = world()
    report = run(w["usage_repo"], w["threads"], w["timelines"], today=w["today"], apply=True)
    repo = w["usage_repo"]

    assert report["turn_events_created"] == 3
    event = repo.get("TURNS#2026-09", "T#t1:h1")
    assert event["source"] == "backfill" and event["measured"] is False
    assert event["model_id"] == "global.anthropic.claude-sonnet-5"
    assert "model_cost_micros" not in event  # tokens unknown per turn

    agents = repo.get("AGENTS#2026-09", "D#2026-09-20#A#rec-1")
    assert agents["turns"] == 2                       # 1 -> 2, from the threads
    # legacy: tokens present -> min(H, old turns) = 1, raised to the user ledger's 2:
    # the (agent, user) row holds the same tokens for both turns.
    assert agents["measured_turns"] == 2
    assert agents["model_cost_micros"] == 2_000_000   # 1M input × $2.00/1M
    assert agents["priced_turns"] == 2
    models = repo.get("AGENT_MODELS#2026-09", "D#2026-09-20#A#rec-1#M#global.anthropic.claude-sonnet-5")
    assert models["input_tokens"] == 1_000_000 and models["model_cost_micros"] == 2_000_000

    # rec-2 has a turn but no counters and no model: a counter row is created for
    # the turn count, unpriced.
    other = repo.get("AGENTS#2026-09", "D#2026-09-21#A#rec-2")
    assert other["turns"] == 1 and other["measured_turns"] == 0
    assert other["unpriced_turns"] == 1 and "model_cost_micros" not in other
    assert report["unpriced_agent_days"] == 1


def test_backfill_is_idempotent():
    w = world()
    run(w["usage_repo"], w["threads"], w["timelines"], today=w["today"], apply=True)
    first = snapshot(w["usage_repo"])
    report = run(w["usage_repo"], w["threads"], w["timelines"], today=w["today"], apply=True)
    assert snapshot(w["usage_repo"]) == first
    assert report["turn_events_created"] == 0
    total_turns = sum(int(r.get("turns", 0)) for (pk, _), r in w["usage_repo"].items.items() if pk.startswith("AGENTS#"))
    assert total_turns == w["human_messages"]


def test_dry_run_writes_nothing():
    w = world()
    before = snapshot(w["usage_repo"])
    report = run(w["usage_repo"], w["threads"], w["timelines"], today=w["today"], apply=False)
    assert snapshot(w["usage_repo"]) == before
    assert report["turn_events_created"] == 3


def test_today_is_left_to_the_live_stream():
    w = world()
    w["threads"][1].values["messages"][1]["id"] = "msg-1790200000000"  # 2026-09-23
    run(w["usage_repo"], w["threads"], w["timelines"], today="2026-09-23", apply=True)
    assert ("AGENTS#2026-09", "D#2026-09-23#A#rec-2") not in w["usage_repo"].items


def test_a_model_switch_inside_a_day_splits_tokens_approximately():
    w = world()
    w["timelines"]["rec-1"] = [
        ("2026-08-01T00:00:00", "global.anthropic.claude-sonnet-5"),
        ("2026-09-20T09:30:00", "global.anthropic.claude-haiku-4-5-20251001-v1:0"),
    ]
    run(w["usage_repo"], w["threads"], w["timelines"], today=w["today"], apply=True)
    repo = w["usage_repo"]
    sonnet = repo.get("AGENT_MODELS#2026-09", "D#2026-09-20#A#rec-1#M#global.anthropic.claude-sonnet-5")
    haiku = repo.get("AGENT_MODELS#2026-09", "D#2026-09-20#A#rec-1#M#global.anthropic.claude-haiku-4-5-20251001-v1:0")
    assert sonnet["input_tokens"] == 500_000 and haiku["input_tokens"] == 500_000
    assert sonnet["split"] == "approx" and haiku["split"] == "approx"
    assert sonnet["model_cost_micros"] == 1_000_000 and haiku["model_cost_micros"] == 500_000
    agents = repo.get("AGENTS#2026-09", "D#2026-09-20#A#rec-1")
    assert agents["model_cost_micros"] == 1_500_000
    assert agents["split"] == "approx"


def test_legacy_days_without_thread_evidence_are_made_consistent_from_the_agent_user_ledger():
    """A day whose threads were deleted has no human messages to count, so the
    three legacy items keep whatever the stream wrote — and they disagreed (agents
    19, users 17, agent-users 23 on the live table). The per-(agent, user) ledger
    is the most granular, so both roll-ups are SET from it."""
    repo = Repo({
        ("AGENTS#2026-08", "D#2026-08-22#A#rec-1"): {"pk": "AGENTS#2026-08", "sk": "D#2026-08-22#A#rec-1", "turns": 19, "input_tokens": 5},
        ("USERS#2026-08", "D#2026-08-22#U#sub-1"): {"pk": "USERS#2026-08", "sk": "D#2026-08-22#U#sub-1", "turns": 17},
        ("AGENT#rec-1#USERS#2026-08", "D#2026-08-22#U#sub-1"): {"pk": "AGENT#rec-1#USERS#2026-08", "sk": "D#2026-08-22#U#sub-1", "turns": 23, "input_tokens": 5},
    })
    run(repo, [], {"rec-1": []}, today="2026-09-23", apply=True)
    assert repo.get("AGENTS#2026-08", "D#2026-08-22#A#rec-1")["turns"] == 23
    assert repo.get("USERS#2026-08", "D#2026-08-22#U#sub-1")["turns"] == 23
    # measured_turns is stamped on the way through, capped at turns — and the
    # three ledgers agree on it: the (agent, user) row's tokens cover 23 turns,
    # so the agent row (legacy 19) and the user roll-up (no tokens, 0) both read 23.
    assert repo.get("AGENTS#2026-08", "D#2026-08-22#A#rec-1")["measured_turns"] == 23
    assert repo.get("USERS#2026-08", "D#2026-08-22#U#sub-1")["measured_turns"] == 23


def test_thread_evidence_never_lowers_a_count_the_granular_ledger_holds():
    """Live case (2026-08-22): one agent had 1 surviving thread but its
    per-(agent, user) items summed to 5 — a probe user's threads were deleted.
    Both are evidence and the truth is at least the larger, so every roll-up is
    `max(threads, Σ agent-user items)` and the three ledgers agree."""
    repo = Repo({
        ("AGENTS#2026-08", "D#2026-08-22#A#rec-1"): {"pk": "AGENTS#2026-08", "sk": "D#2026-08-22#A#rec-1", "turns": 1, "input_tokens": 100},
        ("USERS#2026-08", "D#2026-08-22#U#sub-1"): {"pk": "USERS#2026-08", "sk": "D#2026-08-22#U#sub-1", "turns": 1, "input_tokens": 100},
        ("USERS#2026-08", "D#2026-08-22#U#probe"): {"pk": "USERS#2026-08", "sk": "D#2026-08-22#U#probe", "turns": 4},
        ("AGENT#rec-1#USERS#2026-08", "D#2026-08-22#U#sub-1"): {"pk": "AGENT#rec-1#USERS#2026-08", "sk": "D#2026-08-22#U#sub-1", "turns": 1, "input_tokens": 100},
        ("AGENT#rec-1#USERS#2026-08", "D#2026-08-22#U#probe"): {"pk": "AGENT#rec-1#USERS#2026-08", "sk": "D#2026-08-22#U#probe", "turns": 4},
    })
    threads = [thread(thread_id="t1", created_at="2026-08-22T01:00:00", updated_at="2026-08-22T01:05:00",
                      values={"messages": [{"id": "h1", "type": "human"}, {"id": "msg-1787360400000", "type": "ai"}]})]  # 08-22T01:00Z
    report = run(repo, threads, {"rec-1": [("2026-08-01T00:00:00", "global.anthropic.claude-sonnet-5")]}, today="2026-09-23", apply=True)
    assert repo.get("AGENTS#2026-08", "D#2026-08-22#A#rec-1")["turns"] == 5
    assert repo.get("AGENTS#2026-08", "D#2026-08-22#A#rec-1")["priced_turns"] == 5
    assert repo.get("USERS#2026-08", "D#2026-08-22#U#sub-1")["turns"] == 1
    assert repo.get("USERS#2026-08", "D#2026-08-22#U#probe")["turns"] == 4
    assert repo.get("AGENT#rec-1#USERS#2026-08", "D#2026-08-22#U#sub-1")["turns"] == 1
    assert report["verification"]["agree"] is True


def test_time_prefixed_turn_events_are_merged_under_the_turn_id_key():
    """Events written before the key change: two rows for one turn (an unmeasured
    first flush and a measured second one). They fold into one `T#{turn_id}` item
    — tokens summed, measured if either was, cost recomputed — and the old rows go."""
    tid = "t-9:h-9"
    repo = Repo({
        ("TURNS#2026-09", f"T#2026-09-22T10:00:05+00:00#{tid}"): {"pk": "TURNS#2026-09", "sk": f"T#2026-09-22T10:00:05+00:00#{tid}",
            "turn_id": tid, "thread_id": "t-9", "agent_record_id": "rec-1", "owner_sub": "sub-1", "ended_at": "2026-09-22T10:00:05+00:00",
            "model_id": "global.anthropic.claude-sonnet-5", "routing": "global", "status": "completed", "measured": False, "source": "stream",
            "input_tokens": 0, "output_tokens": 0, "model_cost_micros": 0, "model_calls": 0},
        ("TURNS#2026-09", f"T#2026-09-22T10:00:07+00:00#{tid}"): {"pk": "TURNS#2026-09", "sk": f"T#2026-09-22T10:00:07+00:00#{tid}",
            "turn_id": tid, "thread_id": "t-9", "agent_record_id": "rec-1", "owner_sub": "sub-1", "ended_at": "2026-09-22T10:00:07+00:00",
            "model_id": "global.anthropic.claude-sonnet-5", "routing": "global", "status": "completed", "measured": True, "source": "stream",
            "input_tokens": 1000, "output_tokens": 100, "model_cost_micros": 3000, "model_calls": 1},
    })
    report = run(repo, [], {"rec-1": []}, today="2026-09-23", apply=True)
    assert report["turn_keys_migrated"] == 2
    merged = repo.get("TURNS#2026-09", f"T#{tid}")
    assert merged["input_tokens"] == 1000 and merged["output_tokens"] == 100
    assert merged["measured"] is True and merged["model_calls"] == 1
    assert merged["model_cost_micros"] == 3000
    assert merged["ended_at"] == "2026-09-22T10:00:07+00:00"
    assert [k for k in repo.items if k[0] == "TURNS#2026-09"] == [("TURNS#2026-09", f"T#{tid}")]


def test_stream_events_without_a_model_are_repriced_from_the_timeline():
    """A throttled model lookup wrote the turn unpriced; the version history knows
    what ran, so the event gets its model and cost and the counters move with it."""
    repo = Repo({
        ("TURNS#2026-09", "T#t-5:h-1"): {"pk": "TURNS#2026-09", "sk": "T#t-5:h-1", "turn_id": "t-5:h-1", "thread_id": "t-5",
            "agent_record_id": "rec-1", "owner_sub": "sub-1", "ended_at": "2026-09-22T10:00:00+00:00", "status": "completed",
            "measured": True, "source": "stream", "input_tokens": 1_000_000, "output_tokens": 0, "model_calls": 1},
        ("AGENTS#2026-09", "D#2026-09-22#A#rec-1"): {"pk": "AGENTS#2026-09", "sk": "D#2026-09-22#A#rec-1", "turns": 1,
            "measured_turns": 1, "input_tokens": 1_000_000, "unpriced_turns": 1},
        ("USERS#2026-09", "D#2026-09-22#U#sub-1"): {"pk": "USERS#2026-09", "sk": "D#2026-09-22#U#sub-1", "turns": 1,
            "measured_turns": 1, "input_tokens": 1_000_000, "unpriced_turns": 1},
        ("AGENT#rec-1#USERS#2026-09", "D#2026-09-22#U#sub-1"): {"pk": "AGENT#rec-1#USERS#2026-09", "sk": "D#2026-09-22#U#sub-1",
            "turns": 1, "measured_turns": 1, "input_tokens": 1_000_000, "unpriced_turns": 1},
    })
    report = run(repo, [], {"rec-1": [("2026-08-01T00:00:00", "global.anthropic.claude-sonnet-5")]}, today="2026-09-23", apply=True)
    event = repo.get("TURNS#2026-09", "T#t-5:h-1")
    assert event["model_id"] == "global.anthropic.claude-sonnet-5"
    assert event["model_cost_micros"] == 2_000_000
    assert report["stream_events_repriced"] == 1
    agents = repo.get("AGENTS#2026-09", "D#2026-09-22#A#rec-1")
    assert agents["model_cost_micros"] == 2_000_000 and agents["priced_turns"] == 1 and agents.get("unpriced_turns", 0) == 0


def test_model_timelines_cover_the_deployed_fallback_id_of_each_record():
    """A turn written under `deployed:<arn>` ran the record's own versions, so
    it must be priced from them rather than left unpriced for lack of history."""
    from scripts.backfill_ledger import model_timelines

    class Record:
        record_id = "rec-1"
        harness_arn = "arn:aws:bedrock-agentcore:us-east-1:1:harness/h-1"
        agent_runtime_arn = "arn:aws:bedrock-agentcore:us-east-1:1:runtime/r-1"

    class Control:
        def list_harness_versions(self, **kwargs):
            return {"harnessVersions": []}

        def get_harness(self, **kwargs):
            return {"harnessVersion": "1", "createdAt": "2026-09-01T00:00:00Z",
                    "harness": {"model": {"bedrockModelConfig": {"modelId": "global.anthropic.claude-sonnet-5"}}}}

    out = model_timelines(Control(), [Record()])
    assert out["deployed:" + Record.harness_arn] == out["rec-1"]
    assert out["deployed:" + Record.agent_runtime_arn] == out["rec-1"]


def test_a_day_is_priced_from_its_events_first_and_the_timeline_for_the_rest():
    """One of rec-1's two turns on 09-20 has a stream event: it ran Opus 5.5 by a
    per-thread override and reported 200k input tokens. That turn is unpriced (no
    rate) and its tokens never touch the Sonnet figure; the other 800k tokens the
    day counter holds belong to the turn history lost, priced by the timeline."""
    w = world()
    repo = w["usage_repo"]
    repo.items[("TURNS#2026-09", "T#t1:h1")] = {
        "pk": "TURNS#2026-09", "sk": "T#t1:h1", "turn_id": "t1:h1", "thread_id": "t1", "agent_record_id": "rec-1",
        "owner_sub": "sub-1", "started_at": "2026-09-20T08:59:00+00:00", "ended_at": "2026-09-20T09:00:00+00:00",
        "status": "completed", "measured": True, "source": "stream", "model_id": "global.anthropic.claude-opus-5-5",
        "input_tokens": 200_000, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0, "model_calls": 1,
    }
    run(repo, w["threads"], w["timelines"], today=w["today"], apply=True)
    opus = repo.get("AGENT_MODELS#2026-09", "D#2026-09-20#A#rec-1#M#global.anthropic.claude-opus-5-5")
    sonnet = repo.get("AGENT_MODELS#2026-09", "D#2026-09-20#A#rec-1#M#global.anthropic.claude-sonnet-5")
    assert opus["turns"] == 1 and opus["unpriced_turns"] == 1 and opus["input_tokens"] == 200_000
    assert "model_cost_micros" not in opus
    assert sonnet["turns"] == 1 and sonnet["priced_turns"] == 1 and sonnet["input_tokens"] == 800_000
    assert sonnet["model_cost_micros"] == 1_600_000 and "split" not in sonnet
    agents = repo.get("AGENTS#2026-09", "D#2026-09-20#A#rec-1")
    assert agents["turns"] == 2 and agents["priced_turns"] == 1 and agents["unpriced_turns"] == 1
    assert agents["model_cost_micros"] == 1_600_000
    # sub-1 is the agent's only user, so their row is the agent's row: the same
    # 800k Sonnet tokens priced, the 200k Opus tokens not. Split by turn count it
    # would have put 500k tokens on each model and priced the user at $1.00.
    agent_user = repo.get("AGENT#rec-1#USERS#2026-09", "D#2026-09-20#U#sub-1")
    assert agent_user["model_cost_micros"] == 1_600_000
    assert agent_user["priced_turns"] == 1 and agent_user["unpriced_turns"] == 1
    assert repo.get("USERS#2026-09", "D#2026-09-20#U#sub-1")["model_cost_micros"] == 1_600_000
    # ...and the same measured count on all three ledgers.
    assert agent_user["measured_turns"] == agents["measured_turns"] == 2
    assert repo.get("USERS#2026-09", "D#2026-09-20#U#sub-1")["measured_turns"] == 2


def test_user_rows_carry_the_agent_days_measured_count_shared_by_room():
    """The agent-day item says 4 of 4 turns measured (stamped by an earlier pass);
    its two users' rows still say 1 and 0 (what the stream reported live). The
    surplus 3 is shared by each row's room — user A has 1 turn of room, user B 2
    — so both rows end fully measured, and the two USERS roll-ups follow. Live on
    2026-09-20 the user table read 31 unmeasured turns beside an agent table that
    read 1 for the same day."""
    w = world()
    repo = w["usage_repo"]
    repo.items[("AGENTS#2026-09", "D#2026-09-20#A#rec-1")].update({"turns": 4, "measured_turns": 4})
    repo.items[("AGENT#rec-1#USERS#2026-09", "D#2026-09-20#U#sub-1")].update({"turns": 2, "measured_turns": 1})
    repo.items[("AGENT#rec-1#USERS#2026-09", "D#2026-09-20#U#sub-3")] = {
        "pk": "AGENT#rec-1#USERS#2026-09", "sk": "D#2026-09-20#U#sub-3", "turns": 2, "measured_turns": 0,
        "input_tokens": 500_000,
    }
    repo.items[("USERS#2026-09", "D#2026-09-20#U#sub-3")] = {
        "pk": "USERS#2026-09", "sk": "D#2026-09-20#U#sub-3", "turns": 2, "measured_turns": 0, "input_tokens": 500_000,
    }
    run(repo, w["threads"], w["timelines"], today=w["today"], apply=True)
    assert repo.get("AGENT#rec-1#USERS#2026-09", "D#2026-09-20#U#sub-1")["measured_turns"] == 2
    assert repo.get("AGENT#rec-1#USERS#2026-09", "D#2026-09-20#U#sub-3")["measured_turns"] == 2
    assert repo.get("USERS#2026-09", "D#2026-09-20#U#sub-1")["measured_turns"] == 2
    assert repo.get("USERS#2026-09", "D#2026-09-20#U#sub-3")["measured_turns"] == 2
    agents = repo.get("AGENTS#2026-09", "D#2026-09-20#A#rec-1")
    assert agents["turns"] == 4 and agents["measured_turns"] == 4
