"""Per-user preferences: one item, whole-value writes.

Deliberately a separate table from usage. The usage repository is ADD-only so
concurrent turns cannot lose each other's increments, and a layout is a
`put_item` of a whole document — mixing the two would put a whole-value overwrite
next to counters that must never be overwritten.
"""
from repositories.prefs_repository import PrefsRepository

SUB = "d4e0a1f2-1111-2222-3333-444455556666"


class StubTable:
    """Writes land in the dict the reads come from — the pattern
    `test_interrupted_turn_is_persisted.py` uses."""

    def __init__(self):
        self.items = {}
        self.puts = []
        self.gets = []

    def load(self):
        return None

    def put_item(self, **params):
        self.puts.append(params)
        item = params["Item"]
        self.items[(item["pk"], item["sk"])] = item

    def get_item(self, **params):
        self.gets.append(params)
        key = (params["Key"]["pk"], params["Key"]["sk"])
        item = self.items.get(key)
        return {"Item": item} if item else {}


def repo():
    instance = PrefsRepository.__new__(PrefsRepository)
    instance.table_name = "prefs"
    instance.table = StubTable()
    return instance


def test_a_value_round_trips():
    r = repo()
    r.put(SUB, "insights-layout", {"version": 1, "widgets": []})

    assert r.get(SUB, "insights-layout") == {"version": 1, "widgets": []}


def test_the_key_is_namespaced_by_sub():
    r = repo()
    r.put(SUB, "insights-layout", {"version": 1})

    item = r.table.puts[0]["Item"]
    assert item["pk"] == f"PREFS#{SUB}"
    assert item["sk"] == "insights-layout"
    # The value is nested, not spread over the item: a stored key must never be
    # able to collide with `pk`/`sk` or with a future reserved attribute.
    assert item["value"] == {"version": 1}


def test_two_users_do_not_see_each_others_preferences():
    r = repo()
    r.put(SUB, "insights-layout", {"version": 1, "widgets": ["mine"]})
    r.put("someone-else", "insights-layout", {"version": 1, "widgets": ["theirs"]})

    assert r.get(SUB, "insights-layout")["widgets"] == ["mine"]


def test_a_missing_preference_reads_as_none():
    assert repo().get(SUB, "insights-layout") is None


def test_a_read_is_a_keyed_get_never_a_scan():
    r = repo()
    r.get(SUB, "insights-layout")

    assert r.table.gets[0]["Key"] == {"pk": f"PREFS#{SUB}", "sk": "insights-layout"}
    assert not hasattr(r.table, "scanned")
