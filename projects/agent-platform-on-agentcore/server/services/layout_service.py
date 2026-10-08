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
# layouts for the same reason.
WIDGET_IDS = (
    "kpi",
    "leaderboard",
    "cost_composition",
    "model_mix",
    "billed",
    "trend",
    "reuse",
    "users",
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

    return {"version": LAYOUT_VERSION, "widgets": widgets}


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
