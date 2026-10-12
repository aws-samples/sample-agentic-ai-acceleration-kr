"""Dashboard layout: stored preference reconciled against the widget registry.

Layouts outlive deploys. A stored layout is a hint and the registry is the
authority — by design. Three reconciliation rules protect against failures that
would blank someone's dashboard:

  • An id the registry no longer has is dropped, so a widget rename does not
    corrupt a saved layout.
  • A registered id the layout lacks is appended visible, so a new widget
    shipped after a layout was saved is not permanently invisible to everyone.
  • Anything unrecognised — wrong version, bad span, malformed shape — falls
    back to the default, because there is no version of this feature where a
    broken preference should be more important than seeing the page.

Storage errors are swallowed. A layout is a preference; losing it must never
cost someone the page. Writes return persisted: False on error, reads return the
default layout.
"""
import logging
from typing import Any, Dict, Optional

LAYOUT_VERSION = 1
LAYOUT_NAME = "insights-layout"
# Keep in step with web/src/app/insights/layoutModel.mjs::WIDGET_IDS and with
# widgetRegistry.tsx. A registered id the stored layout lacks is appended visible,
# so adding one here is what makes it appear for people who already saved a layout —
# which is the whole reason `users` and `guardrail` needed no version bump. An id
# dropped from here (the old `quality` widget, now folded into the leaderboard
# drill-down; `rates`, now the Settings page) is silently removed from stored
# layouts for the same reason. `teams` (per-team usage and policy denials) added 2026-10; `policy`
# (gateway decisions and denials, split out of `teams`) on 2026-10-10.
# `billed` folded into `cost_composition` on 2026-10-10; see `_merge_billed`.
# The order is the default layout, read top to bottom by an organisation: overview,
# spend and who spent it (team before person), what is used, then what policy and
# guardrails stopped. Stored layouts keep their own order; only defaults move.
WIDGET_IDS = (
    "kpi",
    "trend",
    "cost_composition",
    "teams",
    "users",
    "leaderboard",
    "model_mix",
    "reuse",
    "policy",
    "guardrail",
)
SPANS = ("half", "full")

DEFAULT_LAYOUT = {
    "version": LAYOUT_VERSION,
    "widgets": [
        {"id": widget_id, "span": "full", "visible": True}
        for widget_id in WIDGET_IDS
    ],
}

logger = logging.getLogger(__name__)


def _default_widgets():
    """Return a deep copy of default widgets."""
    return [dict(widget) for widget in DEFAULT_LAYOUT["widgets"]]


def reconcile(stored):
    """A stored layout reconciled against the registry. Never raises.

    A layout is a hint and the registry is the authority. Three rules, each
    protecting against a way a stored layout could cost someone their dashboard:

    * an id the registry no longer has is dropped, so a renamed widget does not
      break a saved layout;
    * a registered id the layout lacks is **appended visible**, so a newly
      shipped widget is not permanently invisible to everyone who ever saved;
    * anything unrecognised — version, span, shape — falls back rather than
      failing, because there is no version of this feature where a broken
      preference should be more important than seeing the page.
    """
    if not isinstance(stored, dict):
        return {"version": LAYOUT_VERSION, "widgets": _default_widgets()}
    if stored.get("version") != LAYOUT_VERSION:
        return {"version": LAYOUT_VERSION, "widgets": _default_widgets()}

    raw = stored.get("widgets")
    if not isinstance(raw, list):
        return {"version": LAYOUT_VERSION, "widgets": _default_widgets()}

    widgets = []
    seen = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        widget_id = entry.get("id")
        if widget_id not in WIDGET_IDS or widget_id in seen:
            continue
        seen.add(widget_id)
        span = entry.get("span")
        widgets.append({
            "id": widget_id,
            "span": span if span in SPANS else "full",
            "visible": bool(entry.get("visible", True)),
        })

    if not widgets:
        return {"version": LAYOUT_VERSION, "widgets": _default_widgets()}

    for widget_id in WIDGET_IDS:
        if widget_id not in seen:
            widgets.append({"id": widget_id, "span": "full", "visible": True})

    return {"version": LAYOUT_VERSION, "widgets": _merge_billed(raw, widgets)}


def _merge_billed(raw, widgets):
    """A layout saved while `billed` was its own widget, moved onto the merged one.

    `billed` (AgentCore 청구 비용) was folded into `cost_composition` on 2026-10-10,
    the same day the default order was regrouped for an organisation reading top
    to bottom. A layout that still lists `billed` was saved before both, in the old
    order, so it is laid out in WIDGET_IDS order instead — anchoring the merged
    widget to a neighbour does not work, because reconcile appends missing ids and
    the neighbour may be last. Each widget keeps its stored span and visibility;
    the merged one is full width, visible if either old widget was. The next save
    drops `billed`, after which the reader's own order is respected.
    """
    stored = {entry.get("id"): entry for entry in raw if isinstance(entry, dict)}
    billed = stored.get("billed")
    if billed is None:
        return widgets
    cost = next(w for w in widgets if w["id"] == "cost_composition")
    merged = {
        "id": "cost_composition",
        "span": "full",
        "visible": bool(billed.get("visible", True))
        or ("cost_composition" in stored and cost["visible"]),
    }
    by_id = {w["id"]: w for w in widgets}
    by_id["cost_composition"] = merged
    return [by_id[widget_id] for widget_id in WIDGET_IDS]


class LayoutService:
    """Dashboard layout service: get and put reconciled layouts."""

    def __init__(self, repository=None):
        """Initialize with an optional preferences repository.

        If repository is None, the service operates in degraded mode: reads
        return the default layout with persisted: False, writes are dropped.
        """
        self._repository = repository

    @property
    def configured(self) -> bool:
        """True if a repository is configured."""
        return self._repository is not None

    def get(self, sub: str) -> Dict[str, Any]:
        """Retrieve and reconcile a layout for a user.

        Returns the stored layout reconciled against the registry, or the
        default if none is stored. Always succeeds; storage errors return
        the default layout with persisted: False.
        """
        if not self.configured:
            return {
                "version": LAYOUT_VERSION,
                "widgets": _default_widgets(),
                "persisted": False,
            }

        try:
            stored = self._repository.get(sub, LAYOUT_NAME)
            reconciled = reconcile(stored)
            reconciled["persisted"] = True
            return reconciled
        except Exception as e:
            logger.warning(
                f"Failed to retrieve layout for user {sub}: {e}"
            )
            return {
                "version": LAYOUT_VERSION,
                "widgets": _default_widgets(),
                "persisted": False,
            }

    def put(self, sub: str, layout: Dict[str, Any]) -> Dict[str, Any]:
        """Store and reconcile a layout for a user.

        The raw body is never stored — it is reconciled against the registry
        first. Returns the reconciled layout that was actually stored, or the
        default with persisted: False on error.
        """
        if not self.configured:
            return {
                "version": LAYOUT_VERSION,
                "widgets": _default_widgets(),
                "persisted": False,
            }

        try:
            reconciled = reconcile(layout)
            self._repository.put(sub, LAYOUT_NAME, reconciled)
            reconciled["persisted"] = True
            return reconciled
        except Exception as e:
            logger.warning(
                f"Failed to store layout for user {sub}: {e}"
            )
            return {
                "version": LAYOUT_VERSION,
                "widgets": _default_widgets(),
                "persisted": False,
            }
