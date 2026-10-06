"""Reading the rollup back without inventing numbers.

Three things here are easy to get wrong and expensive to get wrong:

1. `turns` is a *request* count that was written across up to three flushes, so
   it must be summed from the attribute and never derived from how many items a
   query returned.
2. Distinct users is the one figure that *is* an item count, because `ADD`
   cannot dedupe — the design puts one item per (day, user) precisely so that
   counting items answers it.
3. A day with no token attribute is unmeasured, not free. Summing it as zero and
   summing a real zero must not produce the same answer.
"""
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.usage_service import UsageService, month_shards  # noqa: E402


class StubRepo:
    """Serves canned items per partition and records what was asked for."""

    def __init__(self, items_by_pk=None):
        self.items_by_pk = items_by_pk or {}
        self.queries = []

    def query(self, pk, start_date, end_date):
        self.queries.append((pk, start_date, end_date))
        return self.items_by_pk.get(pk, [])

    def add(self, *args, **kwargs):
        raise AssertionError("reads must not write")


def service_with(items_by_pk):
    return UsageService(repository=StubRepo(items_by_pk))


def test_a_window_inside_one_month_touches_one_shard():
    assert month_shards("2026-08-01", "2026-08-31") == ["2026-08"]


def test_a_window_across_a_month_boundary_touches_both():
    assert month_shards("2026-07-28", "2026-08-03") == ["2026-07", "2026-08"]


def test_a_thirty_day_window_never_touches_more_than_two():
    assert len(month_shards("2026-07-18", "2026-08-16")) == 2


def test_agent_totals_sum_the_turn_attribute_not_the_item_count():
    """Three items for one agent, seven turns. Counting items would say 3."""
    service = service_with({
        "AGENTS#2026-08": [
            {"sk": "D#2026-08-14#A#rec-1", "turns": Decimal(2),
             "input_tokens": Decimal(100), "output_tokens": Decimal(10)},
            {"sk": "D#2026-08-15#A#rec-1", "turns": Decimal(4),
             "input_tokens": Decimal(200), "output_tokens": Decimal(20)},
            {"sk": "D#2026-08-16#A#rec-1", "turns": Decimal(1),
             "input_tokens": Decimal(300), "output_tokens": Decimal(30)},
        ]
    })

    totals = service.agent_totals("2026-08-14", "2026-08-16")

    assert totals["rec-1"]["turns"] == 7
    assert totals["rec-1"]["input_tokens"] == 600
    assert totals["rec-1"]["output_tokens"] == 60


def test_a_day_with_no_token_attribute_is_counted_as_unmeasured():
    """Backfilled days have turns but no tokens. Their turns must be visible as
    unmeasured, and their absent tokens must not read as zero."""
    service = service_with({
        "AGENTS#2026-08": [
            {"sk": "D#2026-08-10#A#rec-1", "turns": Decimal(3), "measured_turns": Decimal(0)},
            {"sk": "D#2026-08-15#A#rec-1", "turns": Decimal(2), "measured_turns": Decimal(2),
             "input_tokens": Decimal(500), "output_tokens": Decimal(50)},
        ]
    })

    totals = service.agent_totals("2026-08-10", "2026-08-15")["rec-1"]

    assert totals["turns"] == 5
    assert totals["input_tokens"] == 500
    assert totals["unmeasured_turns"] == 3


def test_a_measured_zero_is_not_unmeasured():
    """`add` drops zero counters, so a stored 0 is vanishingly rare — but if the
    attribute is present it was measured, and the two must stay distinct."""
    service = service_with({
        "AGENTS#2026-08": [
            {"sk": "D#2026-08-15#A#rec-1", "turns": Decimal(1), "measured_turns": Decimal(1),
             "input_tokens": Decimal(0), "output_tokens": Decimal(0)},
        ]
    })

    assert service.agent_totals("2026-08-15", "2026-08-15")["rec-1"][
        "unmeasured_turns"
    ] == 0


def test_several_agents_are_split_by_the_sort_key_suffix():
    service = service_with({
        "AGENTS#2026-08": [
            {"sk": "D#2026-08-15#A#rec-1", "turns": Decimal(2)},
            {"sk": "D#2026-08-15#A#rec-2", "turns": Decimal(5)},
        ]
    })

    totals = service.agent_totals("2026-08-15", "2026-08-15")

    assert set(totals) == {"rec-1", "rec-2"}
    assert totals["rec-2"]["turns"] == 5


def test_an_agent_id_containing_a_hash_is_not_truncated():
    """The suffix is everything after the first `#A#`, not up to the next `#`."""
    service = service_with({
        "AGENTS#2026-08": [
            {"sk": "D#2026-08-15#A#odd#id", "turns": Decimal(1)},
        ]
    })

    assert "odd#id" in service.agent_totals("2026-08-15", "2026-08-15")


def test_distinct_users_is_the_number_of_user_items_not_their_turn_sum():
    """Two users, nine turns between them. The answer is 2."""
    service = service_with({
        "AGENT#rec-1#USERS#2026-08": [
            {"sk": "D#2026-08-15#U#sub-a", "turns": Decimal(7)},
            {"sk": "D#2026-08-16#U#sub-a", "turns": Decimal(1)},
            {"sk": "D#2026-08-16#U#sub-b", "turns": Decimal(1)},
        ]
    })

    assert service.distinct_users("rec-1", "2026-08-15", "2026-08-16") == 2


def test_tool_totals_are_keyed_by_tool_name():
    service = service_with({
        "AGENT#rec-1#TOOLS#2026-08": [
            {"sk": "D#2026-08-15#T#web_search", "tool_calls": Decimal(2)},
            {"sk": "D#2026-08-16#T#web_search", "tool_calls": Decimal(3)},
            {"sk": "D#2026-08-16#T#create_artifact", "tool_calls": Decimal(1)},
        ]
    })

    tools = service.tool_totals("rec-1", "2026-08-15", "2026-08-16")

    assert tools == {"web_search": 5, "create_artifact": 1}


def test_daily_totals_are_ordered_and_flag_unmeasured_days():
    service = service_with({
        "AGENTS#2026-08": [
            {"sk": "D#2026-08-16#A#rec-1", "turns": Decimal(1),
             "input_tokens": Decimal(9)},
            {"sk": "D#2026-08-14#A#rec-1", "turns": Decimal(2)},
        ]
    })

    daily = service.daily_totals("2026-08-14", "2026-08-16")

    assert [point["date"] for point in daily] == [
        "2026-08-14", "2026-08-15", "2026-08-16"
    ]
    assert daily[0]["tokens_known"] is False
    assert daily[2]["tokens_known"] is True


def test_the_series_carries_every_day_in_the_window_including_idle_ones():
    """A day with no items is a day with no turns, and it has to occupy the axis.

    Returning only the days that have rows made every consumer that counts rows
    wrong about time: the half-window delta split by row count, so a window with
    four rows compared one day against one day five days apart and labelled it
    "앞 1일 대비", and the trend line drew a five-day gap as one step.
    """
    service = service_with({
        "AGENTS#2026-08": [
            {"sk": "D#2026-08-11#A#rec-1", "turns": Decimal(10)},
            {"sk": "D#2026-08-16#A#rec-1", "turns": Decimal(4)},
        ]
    })

    daily = service.daily_totals("2026-08-11", "2026-08-16")

    assert [point["date"] for point in daily] == [
        "2026-08-11", "2026-08-12", "2026-08-13",
        "2026-08-14", "2026-08-15", "2026-08-16",
    ]
    idle = daily[1]
    assert idle["turns"] == 0
    assert idle["filled"] is True, "an idle day must be distinguishable from a read one"
    # Zero turns is zero tokens, exactly: the two cannot disagree. So the token
    # fields stay known and the delta can still be computed across an idle day.
    assert idle["tokens_known"] is True
    assert daily[0]["filled"] is False


def test_daily_totals_can_be_scoped_to_one_agent():
    service = service_with({
        "AGENTS#2026-08": [
            {"sk": "D#2026-08-15#A#rec-1", "turns": Decimal(2)},
            {"sk": "D#2026-08-15#A#rec-2", "turns": Decimal(40)},
        ]
    })

    daily = service.daily_totals("2026-08-15", "2026-08-15", record_id="rec-1")

    assert len(daily) == 1
    assert daily[0]["turns"] == 2


def test_reads_query_every_shard_the_window_spans():
    repo = StubRepo({})
    UsageService(repository=repo).agent_totals("2026-07-30", "2026-08-02")

    assert repo.queries == [
        ("AGENTS#2026-07", "2026-07-30", "2026-08-02"),
        ("AGENTS#2026-08", "2026-07-30", "2026-08-02"),
    ], "a window crossing a month must read both partitions"


def test_a_shard_that_could_not_be_read_is_reported_as_incomplete():
    """A window spans up to two partitions, and one of them can fail on its own.

    `_items` logs and carries on, which is right — half a leaderboard beats a 500 —
    but the response then carried `sources.usage: true` beside a total that was
    silently short. Every other figure on this page degrades to "모름"; this one
    lied by omission.
    """
    class HalfBrokenRepo(StubRepo):
        def query(self, pk, start_date, end_date):
            if pk.endswith("2026-07"):
                raise RuntimeError("ProvisionedThroughputExceededException")
            return super().query(pk, start_date, end_date)

    repo = HalfBrokenRepo({
        "AGENTS#2026-08": [{"sk": "D#2026-08-01#A#rec-1", "turns": Decimal(3)}]
    })
    service = UsageService(repository=repo)

    service.begin_read()
    totals = service.agent_totals("2026-07-30", "2026-08-01")

    assert totals["rec-1"]["turns"] == 3, "what was readable is still served"
    assert service.reads_complete() is False


def test_a_fresh_read_starts_complete_again():
    """The flag lives on the service, which outlives the request, so a failure in
    one read must not mark every later one incomplete."""
    service = service_with({"AGENTS#2026-08": []})

    service.begin_read()
    assert service.reads_complete() is True

    service.repository.items_by_pk = None  # any read now raises AttributeError
    service.agent_totals("2026-08-01", "2026-08-01")
    assert service.reads_complete() is False

    service.begin_read()
    assert service.reads_complete() is True


def test_an_unconfigured_table_reads_as_empty_not_as_an_exception():
    service = UsageService(repository=None)

    assert service.agent_totals("2026-08-15", "2026-08-15") == {}
    assert service.distinct_users("rec-1", "2026-08-15", "2026-08-15") == 0
    assert service.daily_totals("2026-08-15", "2026-08-15") == []


def test_daily_totals_skips_malformed_sort_keys():
    """Sort keys without #A# or not starting with D# must not contribute."""
    service = service_with({
        "AGENTS#2026-08": [
            {"sk": "INVALID#2026-08-15#A#rec-1", "turns": Decimal(1)},
            {"sk": "D#2026-08-15", "turns": Decimal(2)},
            {"sk": "D#2026-08-15#A#rec-1", "turns": Decimal(3)},
        ]
    })

    daily = service.daily_totals("2026-08-15", "2026-08-15")

    assert len(daily) == 1
    assert daily[0]["date"] == "2026-08-15"
    assert daily[0]["turns"] == 3


def _read_module_source(module) -> str:
    """Read the source file of a module without hardcoding paths."""
    import inspect
    try:
        source = inspect.getsource(module)
        return source
    except (OSError, TypeError):
        raise AssertionError(f"Could not read source for {module.__name__}")


def _no_import_of(source: str, target_module: str) -> bool:
    """Check if source imports target_module. Matches import and from-import.

    Returns True if the module is clean (no import), False if coupling found.
    Ignores docstrings and comments.
    """
    import re
    lines = source.split('\n')
    in_docstring = False
    docstring_char = None

    for line in lines:
        stripped = line.strip()

        # Track multi-line docstrings and skip them
        if '"""' in stripped or "'''" in stripped:
            quote = '"""' if '"""' in stripped else "'''"
            in_docstring = not in_docstring if docstring_char == quote else (
                docstring_char == quote
            )
            docstring_char = quote if in_docstring else None
            continue
        if in_docstring:
            continue

        # Skip comments
        if stripped.startswith('#'):
            continue

        # Check for import statements: both `import X` and `from X import Y`
        # Match: import telemetry_service, from telemetry_service, from services.telemetry_service
        if re.search(rf'\b(import|from)\s+.*\b{re.escape(target_module)}\b', stripped):
            return False

    return True


def test_usage_service_does_not_import_telemetry_service():
    """usage_service and telemetry_service must remain decoupled.

    CloudWatch (telemetry_service) fails independently and the merge belongs
    in routes/insights.py. Coupling them lets a CloudWatch outage blank figures
    the platform holds itself — the whole point of the two-source design.
    """
    from services import usage_service

    source = _read_module_source(usage_service)
    assert _no_import_of(source, 'telemetry_service'), (
        "usage_service must not import telemetry_service. "
        "CloudWatch outages must not blank usage figures. "
        "Merge them in routes/insights.py, not here."
    )


def test_telemetry_service_does_not_import_usage_service():
    """telemetry_service and usage_service must remain decoupled.

    CloudWatch (telemetry_service) fails independently and the merge belongs
    in routes/insights.py. The two sources' independence is the design's core
    safety property.
    """
    from services import telemetry_service

    source = _read_module_source(telemetry_service)
    assert _no_import_of(source, 'usage_service'), (
        "telemetry_service must not import usage_service. "
        "CloudWatch outages must not blank usage figures. "
        "Merge them in routes/insights.py, not here."
    )


def test_unmeasured_turns_are_counted_per_turn_not_per_day():
    """A day where some turns reported tokens and some did not.

    The stored item is one whole day for one agent, accumulated with `ADD`, so the
    old test — "does this item carry a token attribute" — was necessarily
    day-granular: one measured turn created the attribute and every unmeasured turn
    sharing that day disappeared from `unmeasured_turns`. The "+" floor marker and
    the backfill notice both switched off while the total was still a floor. Two real
    paths reach it: the backfill's `--before` boundary day, and a harness turn whose
    metadata never arrived landing beside one whose did.
    """
    service = UsageService(
        repository=StubRepo({
            "AGENTS#2026-08": [
                {
                    "sk": "D#2026-08-15#A#rec-1",
                    "turns": Decimal(10),
                    "measured_turns": Decimal(4),
                    "input_tokens": Decimal(1000),
                    "output_tokens": Decimal(200),
                },
            ],
        })
    )

    totals = service.agent_totals("2026-08-01", "2026-08-31")["rec-1"]

    assert totals["turns"] == 10
    assert totals["unmeasured_turns"] == 6, (
        "six of the ten turns reported no tokens, so the token total is a floor "
        "even though the day carries a token attribute"
    )


def test_an_unstamped_item_is_wholly_unmeasured():
    """One rule, no heuristics: `turns - measured_turns`.

    An item the backfill has not stamped with `measured_turns` reads as fully
    unmeasured even when it carries tokens. The day-granular guess that used to
    stand in ("tokens present, so measured") made three routes disagree about the
    same data; surfacing the gap is how a missed backfill gets noticed.
    """
    service = UsageService(
        repository=StubRepo({
            "AGENTS#2026-08": [
                {"sk": "D#2026-08-14#A#rec-1", "turns": Decimal(3)},
                {"sk": "D#2026-08-15#A#rec-1", "turns": Decimal(5),
                 "input_tokens": Decimal(900)},
            ],
        })
    )

    totals = service.agent_totals("2026-08-01", "2026-08-31")["rec-1"]

    assert totals["turns"] == 8
    assert totals["unmeasured_turns"] == 8

def test_a_fully_measured_day_reports_no_floor():
    service = UsageService(
        repository=StubRepo({
            "AGENTS#2026-08": [
                {"sk": "D#2026-08-15#A#rec-1", "turns": Decimal(6),
                 "measured_turns": Decimal(6), "input_tokens": Decimal(10)},
            ],
        })
    )

    totals = service.agent_totals("2026-08-01", "2026-08-31")["rec-1"]

    assert totals["unmeasured_turns"] == 0


def test_the_cache_tiers_come_back_as_their_own_counters():
    """They are read separately because they are billed separately."""
    service = UsageService(
        repository=StubRepo({
            "AGENTS#2026-08": [
                {"sk": "D#2026-08-15#A#rec-1", "turns": Decimal(2),
                 "measured_turns": Decimal(2),
                 "input_tokens": Decimal(300), "output_tokens": Decimal(50),
                 "cache_read_tokens": Decimal(40000),
                 "cache_write_tokens": Decimal(2000),
                 "failed_turns": Decimal(1)},
            ],
        })
    )

    totals = service.agent_totals("2026-08-01", "2026-08-31")["rec-1"]

    assert totals["input_tokens"] == 300
    assert totals["cache_read_tokens"] == 40000
    assert totals["cache_write_tokens"] == 2000
    assert totals["failed_turns"] == 1
