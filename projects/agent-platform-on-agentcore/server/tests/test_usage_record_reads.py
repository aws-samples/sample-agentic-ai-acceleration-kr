"""One record's Usage page reads its own rows, not everyone's.

A record's counters live in the shared month partitions (`D#<day>#A#<record>`),
so the page used to read every agent's rows for the window — AGENTS# and
AGENT_MODELS# whole, 9k items at 300 agents — and keep its own 30. The rows
have exact keys per day, so they are read by key: one BatchGetItem for the day
rows, one small keyed Query per (day, id) for the model rows. Not a filter: a
`FilterExpression` over the sort key is rejected by DynamoDB (ValidationException,
live 2026-10-19). A repository without `get_many`/`query_prefix` (older stubs)
still gets the whole partition, and the service's own exact match keeps the
answer.

Also here: `_fronting_targets` reads MCP descriptors through the registry's bulk
read. One GetRegistryRecord per MCP record was 62 serial calls (15 s, with 4 s of
throttling sleeps) on every MCP record's Usage open at 62 MCP records.
"""
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.usage_service import UsageService  # noqa: E402

ARN = "arn:aws:bedrock-agentcore:us-west-2:1:runtime/shared-abc"


class KeyedRepo:
    """Answers exact keys (`get_many`) and sort-key prefixes (`query_prefix`), and
    records what was asked; whole-partition `query` is counted so a test can
    assert it never happened."""

    def __init__(self, items_by_pk):
        self.items_by_pk = items_by_pk
        self.get_many_calls = []
        self.prefix_calls = []
        self.queries = []

    def query(self, pk, start_date, end_date, fields=None, ranges=1):
        self.queries.append(pk)
        return self.items_by_pk.get(pk, [])

    def get_many(self, keys):
        self.get_many_calls.append(list(keys))
        wanted = set(keys)
        return [i for pk, items in self.items_by_pk.items() for i in items if (pk, i["sk"]) in wanted]

    def query_prefix(self, pk, sk_prefix):
        self.prefix_calls.append((pk, sk_prefix))
        return [i for i in self.items_by_pk.get(pk, []) if str(i["sk"]).startswith(sk_prefix)]


class PlainRepo:
    """The older shape: no projection, no filter."""

    def __init__(self, items_by_pk):
        self.items_by_pk = items_by_pk

    def query(self, pk, start_date, end_date):
        return self.items_by_pk.get(pk, [])


def row(day, tail, turns, **extra):
    return {"sk": f"D#{day}#{tail}", "turns": Decimal(turns), "measured_turns": Decimal(turns),
            "input_tokens": Decimal(turns * 10), **extra}


class Rec:
    def __init__(self, record_id, runtime_arn=None):
        self.record_id = record_id
        self.name = record_id
        self.agent_runtime_arn = runtime_arn
        self.harness_arn = None


ITEMS = {
    "AGENTS#2026-08": [
        row("2026-08-10", "A#rec-1", 3),
        row("2026-08-11", "A#rec-1", 2),
        row("2026-08-11", "A#rec-10", 7),          # `#A#rec-1` is a substring of this key
        row("2026-08-12", f"A#deployed:{ARN}", 4),  # rec-1's alias
        row("2026-08-12", "A#rec-2", 9),
    ],
    "AGENT_MODELS#2026-08": [
        row("2026-08-10", "A#rec-1#M#m-a", 3),
        row("2026-08-11", "A#rec-1#M#m-b", 2),
        row("2026-08-11", "A#rec-10#M#m-a", 7),
        row("2026-08-12", f"A#deployed:{ARN}#M#m-a", 4),
        row("2026-08-12", "A#rec-2#M#m-a", 9),
    ],
}


def service(repo):
    svc = UsageService(repository=repo)
    svc.set_record_aliases([Rec("rec-1", ARN), Rec("rec-10"), Rec("rec-2")])
    svc.begin_read()
    return svc


def test_record_totals_read_the_records_day_rows_by_key():
    repo = KeyedRepo(ITEMS)
    totals = service(repo).record_totals("rec-1", "2026-08-10", "2026-08-12")
    assert totals["turns"] == 3 + 2 + 4  # own rows plus the deployed alias, never rec-10
    assert repo.queries == []
    (keys,) = repo.get_many_calls
    assert set(keys) == {
        (f"AGENTS#2026-08", f"D#{day}#A#{lid}")
        for day in ("2026-08-10", "2026-08-11", "2026-08-12")
        for lid in ("rec-1", f"deployed:{ARN}")
    }


def test_record_totals_without_keyed_reads_still_match_exactly():
    totals = service(PlainRepo(ITEMS)).record_totals("rec-1", "2026-08-01", "2026-08-31")
    assert totals["turns"] == 9


def test_record_models_read_one_keyed_prefix_per_day_and_id():
    repo = KeyedRepo(ITEMS)
    models = service(repo).record_models("rec-1", "2026-08-10", "2026-08-12")
    assert {m: c["turns"] for m, c in models.items()} == {"m-a": 7, "m-b": 2}
    assert repo.queries == []
    assert set(repo.prefix_calls) == {
        ("AGENT_MODELS#2026-08", f"D#{day}#A#{lid}#M#")
        for day in ("2026-08-10", "2026-08-11", "2026-08-12")
        for lid in ("rec-1", f"deployed:{ARN}")
    }


def test_record_models_without_keyed_reads_match_the_bulk_read():
    svc = service(PlainRepo(ITEMS))
    assert svc.record_models("rec-1", "2026-08-01", "2026-08-31") == \
        svc.agent_models("2026-08-01", "2026-08-31")["rec-1"]


def test_daily_totals_for_one_record_come_from_its_rows_and_stay_dense():
    repo = KeyedRepo(ITEMS)
    daily = service(repo).daily_totals("2026-08-10", "2026-08-13", record_id="rec-1")
    assert [(d["date"], d["turns"]) for d in daily] == [
        ("2026-08-10", 3), ("2026-08-11", 2), ("2026-08-12", 4), ("2026-08-13", 0),
    ]
    assert repo.queries == []


def test_the_records_rows_are_read_once_per_request():
    """Totals, models and the trend share one keyed read."""
    repo = KeyedRepo(ITEMS)
    svc = service(repo)
    svc.record_totals("rec-1", "2026-08-10", "2026-08-12")
    svc.record_models("rec-1", "2026-08-10", "2026-08-12")
    svc.daily_totals("2026-08-10", "2026-08-12", record_id="rec-1")
    assert len(repo.get_many_calls) == 1
    assert len(repo.prefix_calls) == 6
    # And the leaderboard's whole-partition read is untouched by it.
    assert len(svc.agent_totals("2026-08-01", "2026-08-31")) == 3
    assert repo.queries == ["AGENTS#2026-08"]


def test_a_failed_keyed_read_is_recorded_not_invented():
    class Failing(KeyedRepo):
        def get_many(self, keys):
            raise RuntimeError("throttled")

    svc = service(Failing(ITEMS))
    assert svc.record_totals("rec-1", "2026-08-10", "2026-08-12")["turns"] == 0
    assert svc.reads_complete() is False


# ── _fronting_targets reads descriptors in bulk ────────────────────────────

class Summary:
    def __init__(self, record_id, status="APPROVED"):
        self.record_id = record_id
        self.name = record_id
        self.status = status
        self.descriptor_type = "MCP"
        self.descriptor_content = None


class BulkRegistry:
    def __init__(self, summaries, contents):
        self._summaries = summaries
        self._contents = contents
        self.bulk_calls = 0

    def list_records(self, descriptor_type=None, **kwargs):
        return list(self._summaries)

    def record_details(self, summaries):
        self.bulk_calls += 1
        out = {}
        for s in summaries:
            if s.record_id in self._contents:
                s.descriptor_content = self._contents[s.record_id]
                out[s.record_id] = s
        return out

    def get_record(self, record_id):
        raise AssertionError("MCP descriptors must be read in bulk, not one Get each")


class Harness:
    def __init__(self, endpoints):
        self._endpoints = endpoints
        self.asked = []

    def gateway_target_endpoints(self, gateway_arn):
        self.asked.append(gateway_arn)
        return self._endpoints.get(gateway_arn, {})


def test_fronting_targets_read_mcp_descriptors_in_one_bulk_call():
    gw1 = "arn:aws:bedrock-agentcore:us-west-2:1:gateway/g1"
    gw2 = "arn:aws:bedrock-agentcore:us-west-2:1:gateway/g2"
    url = "https://runtime.example/invocations"
    registry = BulkRegistry(
        [Summary("gw-rec-1"), Summary("gw-rec-2"), Summary("plain-mcp"), Summary("unreadable")],
        {
            "gw-rec-1": {"server": {"gatewayArn": gw1}},
            "gw-rec-2": {"server": {"gatewayArn": gw2}},
            "plain-mcp": {"server": {"remotes": [{"url": url}]}},
        },
    )
    harness = Harness({gw1: {"platform-status": url, "other": "https://x"}, gw2: {"ps2": url}})
    svc = UsageService(repository=PlainRepo({}), registry=registry, harness=harness)
    assert svc._fronting_targets(url) == {"platform-status", "ps2"}
    assert registry.bulk_calls == 1
    assert sorted(harness.asked) == [gw1, gw2]


def test_fronting_targets_fall_back_to_get_when_the_registry_has_no_bulk_read():
    gw = "arn:aws:bedrock-agentcore:us-west-2:1:gateway/g1"
    url = "https://runtime.example/invocations"

    class GetOnly:
        def list_records(self, descriptor_type=None, **kwargs):
            return [Summary("gw-rec-1")]

        def get_record(self, record_id):
            s = Summary(record_id)
            s.descriptor_content = {"server": {"gatewayArn": gw}}
            return s

    svc = UsageService(repository=PlainRepo({}), registry=GetOnly(),
                       harness=Harness({gw: {"t": url}}))
    assert svc._fronting_targets(url) == {"t"}
