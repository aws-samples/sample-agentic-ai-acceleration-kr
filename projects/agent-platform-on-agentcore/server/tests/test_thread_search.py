"""The scan fallback of the thread listing (`ThreadRepository._scan_search`).

The listing itself is a Query on a list index now (test_thread_list_index.py).
The scan remains for created_at sorting and for a table that predates the
indexes; these pin that it stays projected and returns the same page of full
threads the original scan did.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from repositories.thread_repository import ThreadRepository  # noqa: E402

TABLE = "threads"


def _item(n, owner="me"):
    return {"thread_id": f"t{n:02d}", "created_at": f"2026-10-01T00:00:{n:02d}",
            "updated_at": f"2026-10-11T00:00:{(n * 7) % 60:02d}", "status": "idle",
            "values": json.dumps({"messages": [{"type": "human", "content": f"hi {n}"}]}),
            "metadata": "{}", "owner_sub": owner}


class FakeTable:
    """Scan pages of `page` items, honouring the projection and an owner filter."""

    def __init__(self, items, page=4):
        self.items, self.page, self.projections = items, page, []

    def scan(self, ProjectionExpression=None, FilterExpression=None, ExclusiveStartKey=None):
        self.projections.append(ProjectionExpression)
        start = int(ExclusiveStartKey["n"]) if ExclusiveStartKey else 0
        chunk = self.items[start:start + self.page]
        if FilterExpression is not None:
            owner = FilterExpression.get_expression()["values"][1]
            chunk = [i for i in chunk if i["owner_sub"] == owner]
        fields = [f.strip() for f in ProjectionExpression.split(",")]
        out = {"Items": [{k: i[k] for k in fields if k in i} for i in chunk]}
        if start + self.page < len(self.items):
            out["LastEvaluatedKey"] = {"n": str(start + self.page)}
        return out


class FakeResource:
    def __init__(self, items, unprocessed_once=()):
        self.by_id = {i["thread_id"]: i for i in items}
        self.unprocessed = set(unprocessed_once)
        self.batches = []

    def batch_get_item(self, RequestItems):
        keys = [k["thread_id"] for k in RequestItems[TABLE]["Keys"]]
        self.batches.append(keys)
        held = [k for k in keys if k in self.unprocessed]
        self.unprocessed -= set(held)
        out = {"Responses": {TABLE: [self.by_id[k] for k in keys if k in self.by_id and k not in held]}}
        if held:
            out["UnprocessedKeys"] = {TABLE: {"Keys": [{"thread_id": k} for k in held]}}
        return out


def _repo(items, **kw):
    repo = ThreadRepository.__new__(ThreadRepository)
    repo.table_name = TABLE
    repo.table = FakeTable(items)
    repo.dynamodb = FakeResource(items, **kw)
    return repo


def _old_page(items, owner, limit, offset):
    """What the unprojected scan returned: every full item, sorted, sliced."""
    mine = [ThreadRepository._to_thread(i) for i in items if owner is None or i["owner_sub"] == owner]
    mine.sort(key=lambda t: t.updated_at, reverse=True)
    return [t.model_dump() for t in mine[offset:offset + limit]]


ITEMS = [_item(n, owner="me" if n % 3 else "other") for n in range(1, 31)]


def test_the_scan_never_reads_transcripts():
    repo = _repo(ITEMS)
    repo._scan_search(None, limit=5)
    assert repo.table.projections and all("values" not in p for p in repo.table.projections)


def test_the_page_is_the_one_the_full_scan_returned():
    for owner, limit, offset in [(None, 5, 0), (None, 7, 10), ("me", 4, 2), ("other", 20, 0)]:
        repo = _repo(ITEMS)
        got = [t.model_dump() for t in repo._scan_search(owner, limit=limit, offset=offset)]
        assert got == _old_page(ITEMS, owner, limit, offset)
        # Only the page is fetched in full.
        assert sum(len(b) for b in repo.dynamodb.batches) == len(got)


def test_unprocessed_keys_are_retried_and_order_kept():
    repo = _repo(ITEMS, unprocessed_once={"t03", "t10"})
    got = [t.thread_id for t in repo._scan_search(None, limit=30)]
    assert got == [t["thread_id"] for t in _old_page(ITEMS, None, 30, 0)]
    assert len(repo.dynamodb.batches) == 2


def test_a_thread_deleted_after_the_scan_is_left_out():
    repo = _repo(ITEMS)
    first = _old_page(ITEMS, None, 3, 0)[0]["thread_id"]
    del repo.dynamodb.by_id[first]
    got = [t.thread_id for t in repo._scan_search(None, limit=3)]
    assert first not in got and len(got) == 2
