"""Which navigation menus a plain user sees — one platform-wide setting.

An admin decides from the Settings page which of the non-admin menus appear in
the sidebar for the `user` role. The setting is a *visibility* preference: every
route it can hide is one a plain user may still call, and the server-side role
checks are untouched by it. Admins always see every menu, so a setting that hid
the Settings door could not lock anyone out.

Stored as one document in the preferences table under a fixed platform key
(`PREFS#platform` / `nav-visibility`), beside the per-user layouts and with the
same rules: the stored value is a hint and `MENU_KEYS` is the authority — an
unknown key is dropped, a malformed document reads as "nothing hidden", and a
storage failure never costs anyone the sidebar.
"""
import logging
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

# The menus a plain user can see, in sidebar order. Chats is not here: it is the
# home route and the reason the app exists. The admin-only menus (Insights,
# Settings) are not here either — a plain user never sees them regardless.
MENU_KEYS = ("knowledge", "registry", "harness")
PLATFORM_SUB = "platform"
NAV_NAME = "nav-visibility"


def reconcile(stored: Any) -> List[str]:
    """The hidden keys a stored document actually means, in `MENU_KEYS` order."""
    if not isinstance(stored, dict):
        return []
    raw = stored.get("hidden")
    if not isinstance(raw, list):
        return []
    hidden = {key for key in raw if isinstance(key, str) and key in MENU_KEYS}
    return [key for key in MENU_KEYS if key in hidden]


class NavService:
    """Read and write the platform-wide menu visibility."""

    def __init__(self, repository=None):
        self._repository = repository

    @property
    def configured(self) -> bool:
        return self._repository is not None

    def get(self) -> Dict[str, Any]:
        """`{"hidden": [...], "menus": MENU_KEYS, "persisted": bool}`; never raises."""
        if not self.configured:
            return {"hidden": [], "menus": list(MENU_KEYS), "persisted": False}
        try:
            stored = self._repository.get(PLATFORM_SUB, NAV_NAME)
            return {"hidden": reconcile(stored), "menus": list(MENU_KEYS), "persisted": True}
        except Exception:
            logger.warning("Failed to read nav visibility", exc_info=True)
            return {"hidden": [], "menus": list(MENU_KEYS), "persisted": False}

    def put(self, hidden: Optional[Iterable[Any]], *, by: str = "") -> Dict[str, Any]:
        """Store the reconciled key list. Returns what was stored."""
        keys = reconcile({"hidden": list(hidden or [])})
        if not self.configured:
            return {"hidden": keys, "menus": list(MENU_KEYS), "persisted": False}
        try:
            self._repository.put(PLATFORM_SUB, NAV_NAME, {"hidden": keys, "updated_by": by})
            return {"hidden": keys, "menus": list(MENU_KEYS), "persisted": True}
        except Exception:
            logger.warning("Failed to store nav visibility", exc_info=True)
            return {"hidden": keys, "menus": list(MENU_KEYS), "persisted": False}
