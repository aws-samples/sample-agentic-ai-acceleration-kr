"""The thread listing is one Query on a list index, carrying only the row.

It used to Scan the whole table: at 88k threads (589MB) a sidebar load read ~75k
RCU and took 6-9s, the server CPU sat at 100% deserialising transcripts, and 50
concurrent users got 57% 504s (bst load test, 2026-10-18). These pin which index
answers which listing, that no transcript is read for a page that has summaries,
and that a write carries what the indexes need (and nothing DynamoDB rejects).
"""
import json
import os
import sys

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.thread import Thread  # noqa: E402
from repositories import thread_repository as tr  # noqa: E402
from repositories.thread_repository import ThreadRepository, summary_of  # noqa: E402

TABLE = "threads"


def _values(n):
    return {"messages": [
        {"type": "human", "content": f"question {n}"},
        {"type": "ai", "content": [{"type": "text", "text": f"answer {n}"}]},
        {"type": "human", "content": "again"},
        {"type": "ai", "content": "second answer"},
    ]}


def _item(n, owner="me", agent="rec-a", legacy=False):
    item = {"thread_id": f"t{n:02d}", "created_at": f"2026-10-01T00:00:{n:02d}",
            "updated_at": f"2026-10-11T00:{n:02d}:00", "status": "busy" if n % 4 == 0 else "idle",
            "values": json.dumps(_values(n)), "metadata": json.dumps({"k": n % 2}),
            "owner_sub": owner, "agent_record_id": agent, "agent_name": "A", "scope": "all"}
    if not legacy:
        item["summary"] = json.dumps(summary_of(_values(n)))
    return item


class FakeIndexTable:
    """Query over in-memory items for the three list indexes, paged by Limit."""

    def __init__(self, items, missing_indexes=False):
        self.items, self.queries, self.missing = items, [], missing_indexes
        self.puts = []

    def query(self, IndexName, KeyConditionExpression, ScanIndexForward, Limit,
              FilterExpression=None, ExclusiveStartKey=None):
        if self.missing:
            raise ClientError({"Error": {"Code": "ValidationException", "Message": "no index"}}, "Query")
        self.queries.append(IndexName)
        attr = {tr.OWNER_INDEX: "owner_sub", tr.AGENT_INDEX: "agent_record_id", tr.SCOPE_INDEX: "scope"}[IndexName]
        value = KeyConditionExpression.get_expression()["values"][1]
        rows = sorted((i for i in self.items if i.get(attr) == value),
                      key=lambda i: i["updated_at"], reverse=not ScanIndexForward)
        start = int(ExclusiveStartKey["n"]) if ExclusiveStartKey else 0
        chunk = rows[start:start + Limit]
        if FilterExpression is not None:
            chunk = [i for i in chunk if _matches(FilterExpression, i)]
        # The index projection: no transcript.
        out = {"Items": [{k: v for k, v in i.items() if k != "values"} for i in chunk]}
        if start + Limit < len(rows):
            out["LastEvaluatedKey"] = {"n": str(start + Limit)}
        return out

    def put_item(self, Item):
        self.puts.append(Item)


def _matches(cond, item):
    expr = cond.get_expression()
    if expr["operator"] == "AND":
        return all(_matches(c, item) for c in expr["values"])
    name, value = expr["values"]
    return item.get(name.name) == value


class FakeResource:
    def __init__(self, items):
        self.by_id = {i["thread_id"]: i for i in items}
        self.batches = []

    def batch_get_item(self, RequestItems):
        keys = [k["thread_id"] for k in RequestItems[TABLE]["Keys"]]
        self.batches.append(keys)
        return {"Responses": {TABLE: [self.by_id[k] for k in keys if k in self.by_id]}}


def _repo(items, **kw):
    repo = ThreadRepository.__new__(ThreadRepository)
    repo.table_name = TABLE
    repo.table = FakeIndexTable(items, **kw)
    repo.dynamodb = FakeResource(items)
    return repo


ITEMS = ([_item(n) for n in range(1, 21)] + [_item(n, owner="other", agent="rec-b") for n in range(21, 31)])


def test_a_users_sidebar_queries_the_owner_index_and_reads_no_transcript():
    repo = _repo(ITEMS)
    page = repo.search("me", limit=5)
    assert repo.table.queries == [tr.OWNER_INDEX]
    assert repo.dynamodb.batches == []
    assert [t.thread_id for t in page] == ["t20", "t19", "t18", "t17", "t16"]
    assert page[0].values == {"messages": [{"type": "human", "content": "question 20"},
                                           {"type": "ai", "content": "answer 20"}], "turns": 2}


def test_admin_lists_every_thread_through_the_scope_index():
    repo = _repo(ITEMS)
    page = repo.search(None, limit=3, offset=1)
    assert repo.table.queries == [tr.SCOPE_INDEX]
    assert [t.thread_id for t in page] == ["t29", "t28", "t27"]


def test_an_agents_threads_use_the_agent_index_and_keep_the_owner_scope():
    repo = _repo(ITEMS)
    assert [t.thread_id for t in repo.search("me", limit=50, agent_record_id="rec-b")] == []
    assert len(repo.search(None, limit=50, agent_record_id="rec-b")) == 10
    assert set(repo.table.queries) == {tr.AGENT_INDEX}


def test_a_filtered_page_is_filled_across_query_pages():
    repo = _repo(ITEMS)
    busy = repo.search("me", limit=5, status="busy")
    assert [t.thread_id for t in busy] == ["t20", "t16", "t12", "t08", "t04"]
    odd = repo.search("me", limit=3, metadata={"k": 1})
    assert [t.thread_id for t in odd] == ["t19", "t17", "t15"]


def test_rows_written_before_summaries_are_read_in_full():
    items = [_item(n, legacy=n in (19, 17)) for n in range(1, 21)]
    repo = _repo(items)
    page = repo.search("me", limit=4)
    assert repo.dynamodb.batches == [["t19", "t17"]]
    assert page[1].values["messages"][0]["content"] == "question 19"
    assert page[1].values["turns"] == 2


def test_a_table_without_the_indexes_falls_back_to_the_scan(monkeypatch):
    repo = _repo(ITEMS, missing_indexes=True)
    called = {}
    monkeypatch.setattr(repo, "_scan_search", lambda *a, **kw: called.setdefault("scan", []) or [])
    repo.search("me", limit=5)
    assert "scan" in called


def test_created_at_sorting_still_uses_the_scan(monkeypatch):
    repo = _repo(ITEMS)
    monkeypatch.setattr(repo, "_scan_search", lambda *a, **kw: ["scanned"])
    assert repo.search("me", sort_by="created_at") == ["scanned"]
    assert repo.table.queries == []


@pytest.mark.parametrize("owner,agent", [("", ""), ("me", ""), ("", "rec-a")])
def test_writes_carry_index_fields_and_never_an_empty_key(owner, agent):
    repo = _repo([])
    thread = Thread(thread_id="t1", created_at="c", updated_at="u", values=_values(1),
                    owner_sub=owner, agent_record_id=agent)
    repo.create(thread)
    repo.update("t1", thread)
    for item in repo.table.puts:
        assert item["scope"] == "all"
        assert json.loads(item["summary"])["turns"] == 2
        for key in ("owner_sub", "agent_record_id"):
            assert item.get(key, None) != ""


def test_a_long_first_message_is_clipped_in_the_summary():
    summary = summary_of({"messages": [{"type": "human", "content": "x" * 5000}]})
    assert len(summary["messages"][0]["content"]) == tr.SUMMARY_CHARS
    assert summary["turns"] == 0


# --- the 400 KB item cap ------------------------------------------------------

def _huge(pairs, ai_bytes, tool_bytes=0):
    msgs = []
    for i in range(pairs):
        ai = {"type": "ai", "content": "a" * ai_bytes, "id": f"a{i}"}
        if tool_bytes:
            ai["tool_calls"] = [{"id": f"t{i}", "name": "fetch_url", "args": {}, "result": "r" * tool_bytes}]
        msgs += [{"type": "human", "content": f"q{i}", "id": f"h{i}"}, ai]
    return {"messages": msgs}


def test_a_transcript_that_fits_is_stored_unchanged():
    values = _huge(3, 100)
    assert json.loads(tr.fit_values(values)) == values


def test_old_tool_results_are_clipped_before_any_message_is_dropped():
    values = _huge(30, 200, tool_bytes=20_000)  # ~600 KB, mostly tool output
    stored = json.loads(tr.fit_values(values))
    assert len(stored["messages"]) == 60 and "trimmed_messages" not in stored
    assert len(stored["messages"][1]["tool_calls"][0]["result"]) < 2_100
    assert len(stored["messages"][-1]["tool_calls"][0]["result"]) == 20_000  # recent kept whole
    assert len(json.dumps(stored).encode()) <= tr.VALUES_MAX_BYTES


def test_the_oldest_exchanges_go_when_clipping_is_not_enough():
    values = _huge(40, 15_000)  # ~600 KB of answers
    stored = json.loads(tr.fit_values(values))
    assert len(json.dumps(stored).encode()) <= tr.VALUES_MAX_BYTES
    assert stored["messages"][0]["type"] == "human"
    assert stored["trimmed_messages"] == 80 - len(stored["messages"])
    assert stored["messages"][-1]["id"] == "a39"


def test_every_save_of_a_huge_thread_fits_the_item_cap():
    repo = _repo([])
    thread = Thread(thread_id="t1", created_at="c", updated_at="u", values=_huge(40, 15_000), owner_sub="me")
    repo.update("t1", thread)
    assert len(repo.table.puts[-1]["values"].encode()) <= tr.VALUES_MAX_BYTES
    assert json.loads(repo.table.puts[-1]["summary"])["turns"] == 40  # the true count, not the kept one
