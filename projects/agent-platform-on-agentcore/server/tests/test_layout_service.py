"""Dashboard layout: stored preference reconciled against the widget registry.

Layouts outlive deploys. Every case here is a version of the same rule — a stored
layout is a *hint*, and the registry is the authority. A stored layout that could
blank someone's dashboard because a widget was renamed is the failure this file
exists to prevent.
"""
from services.layout_service import (
    DEFAULT_LAYOUT,
    LAYOUT_VERSION,
    WIDGET_IDS,
    LayoutService,
    reconcile,
)

SUB = "d4e0a1f2-1111-2222-3333-444455556666"


class StubPrefs:
    def __init__(self, stored=None):
        self.stored = dict(stored or {})
        self.puts = []

    def get(self, sub, name):
        return self.stored.get((sub, name))

    def put(self, sub, name, value):
        self.puts.append((sub, name, value))
        self.stored[(sub, name)] = value


def test_the_default_layout_covers_every_registered_widget():
    assert [w["id"] for w in DEFAULT_LAYOUT["widgets"]] == list(WIDGET_IDS)
    assert DEFAULT_LAYOUT["version"] == LAYOUT_VERSION
    assert all(w["visible"] for w in DEFAULT_LAYOUT["widgets"])


def test_no_stored_layout_yields_the_default():
    svc = LayoutService(repository=StubPrefs())
    result = svc.get(SUB)

    assert result["widgets"] == DEFAULT_LAYOUT["widgets"]
    assert result["persisted"] is True   # the table exists; nothing saved yet


def test_a_stored_order_is_preserved():
    stored = {"version": 1, "widgets": [
        {"id": "billed", "span": "full", "visible": True},
        {"id": "kpi", "span": "half", "visible": False},
    ]}
    svc = LayoutService(repository=StubPrefs({(SUB, "insights-layout"): stored}))
    widgets = svc.get(SUB)["widgets"]

    assert widgets[0] == {"id": "billed", "span": "full", "visible": True}
    assert widgets[1] == {"id": "kpi", "span": "half", "visible": False}


def test_a_widget_shipped_after_the_layout_was_saved_is_appended_visible():
    """Otherwise a new card is invisible to every existing user, forever, and
    nobody would ever report it as a bug."""
    stored = {"version": 1, "widgets": [{"id": "kpi", "span": "full", "visible": True}]}
    widgets = reconcile(stored)["widgets"]

    assert widgets[0]["id"] == "kpi"
    appended = {w["id"] for w in widgets[1:]}
    assert appended == set(WIDGET_IDS) - {"kpi"}
    assert all(w["visible"] for w in widgets[1:])


def test_an_unknown_widget_id_is_dropped_not_fatal():
    stored = {"version": 1, "widgets": [
        {"id": "a-widget-we-deleted", "span": "full", "visible": True},
        {"id": "kpi", "span": "full", "visible": True},
    ]}
    widgets = reconcile(stored)["widgets"]

    assert "a-widget-we-deleted" not in {w["id"] for w in widgets}
    assert widgets[0]["id"] == "kpi"


def test_an_unknown_version_falls_back_to_the_default():
    stored = {"version": 99, "widgets": [{"id": "kpi", "span": "half", "visible": False}]}
    assert reconcile(stored)["widgets"] == DEFAULT_LAYOUT["widgets"]


def test_a_bad_span_falls_back_to_full_rather_than_rendering_nothing():
    stored = {"version": 1, "widgets": [{"id": "kpi", "span": "quarter", "visible": True}]}
    assert reconcile(stored)["widgets"][0]["span"] == "full"


def test_garbage_reconciles_to_the_default():
    for junk in (None, {}, {"widgets": "not a list"}, {"version": 1, "widgets": [1, 2]}):
        assert reconcile(junk)["widgets"] == DEFAULT_LAYOUT["widgets"]


def test_put_stores_the_reconciled_layout_not_the_raw_body():
    """The client is not the authority. Storing its body verbatim would let a
    typo persist and come back on every load."""
    prefs = StubPrefs()
    svc = LayoutService(repository=prefs)
    svc.put(SUB, {"version": 1, "widgets": [
        {"id": "kpi", "span": "quarter", "visible": True},
        {"id": "nope", "span": "full", "visible": True},
    ]})

    sub, name, value = prefs.puts[0]
    assert (sub, name) == (SUB, "insights-layout")
    assert value["widgets"][0]["span"] == "full"
    assert "nope" not in {w["id"] for w in value["widgets"]}


def test_without_a_table_the_default_is_served_and_writes_are_dropped():
    """A missing PREFS_TABLE must not break the page. Local development runs
    without it, and `--reload` ignores .env anyway."""
    svc = LayoutService(repository=None)

    assert svc.configured is False
    assert svc.get(SUB)["widgets"] == DEFAULT_LAYOUT["widgets"]
    assert svc.get(SUB)["persisted"] is False

    result = svc.put(SUB, {"version": 1, "widgets": []})
    assert result["persisted"] is False
    assert result["widgets"] == DEFAULT_LAYOUT["widgets"]


def test_a_storage_failure_still_returns_a_usable_layout():
    """A layout is a preference. Losing it must never cost someone the page."""
    class BrokenPrefs:
        def get(self, sub, name):
            raise RuntimeError("ProvisionedThroughputExceededException")

        def put(self, sub, name, value):
            raise RuntimeError("ProvisionedThroughputExceededException")

    svc = LayoutService(repository=BrokenPrefs())
    assert svc.get(SUB)["widgets"] == DEFAULT_LAYOUT["widgets"]
    assert svc.put(SUB, DEFAULT_LAYOUT)["persisted"] is False
