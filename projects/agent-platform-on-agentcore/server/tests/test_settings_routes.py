"""`/api/settings/nav`: which sidebar menus a plain user sees."""
import os
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routes.settings as settings  # noqa: E402
from core.auth import AuthUser, require_admin  # noqa: E402
from services.nav_service import MENU_KEYS, NavService, reconcile  # noqa: E402


class Prefs:
    def __init__(self, fail=False):
        self.items = {}
        self.fail = fail

    def get(self, sub, name):
        if self.fail:
            raise RuntimeError("table down")
        return self.items.get((sub, name))

    def put(self, sub, name, value):
        if self.fail:
            raise RuntimeError("table down")
        self.items[(sub, name)] = value


def user(is_admin=False):
    return AuthUser(sub="sub-1", username="someone", groups=["admin"] if is_admin else [])


def teardown_function():
    settings._nav_override = None


def test_reconcile_keeps_only_known_menus_in_sidebar_order():
    assert reconcile({"hidden": ["harness", "insights", "knowledge", 3, "knowledge"]}) == ["knowledge", "harness"]
    assert reconcile({"hidden": "harness"}) == []
    assert reconcile(None) == []
    assert "chats" not in MENU_KEYS and "insights" not in MENU_KEYS and "settings" not in MENU_KEYS


def test_put_then_get_round_trips_under_the_platform_key():
    prefs = Prefs()
    settings._nav_override = NavService(prefs)
    out = settings.put_nav_visibility(settings.NavVisibilityRequest(hidden=["registry", "bogus"]), user=user(True))
    assert out == {"hidden": ["registry"], "menus": list(MENU_KEYS), "persisted": True}
    assert prefs.items[("platform", "nav-visibility")]["hidden"] == ["registry"]
    assert prefs.items[("platform", "nav-visibility")]["updated_by"] == "someone"
    assert settings.get_nav_visibility(user())["hidden"] == ["registry"]


def test_unconfigured_or_failing_storage_hides_nothing_and_says_so():
    settings._nav_override = NavService(None)
    assert settings.get_nav_visibility(user()) == {"hidden": [], "menus": list(MENU_KEYS), "persisted": False}
    settings._nav_override = NavService(Prefs(fail=True))
    assert settings.get_nav_visibility(user())["persisted"] is False
    assert settings.put_nav_visibility(settings.NavVisibilityRequest(hidden=["harness"]), user=user(True))["persisted"] is False


def test_write_is_admin_only():
    """The write depends on `require_admin` (403 for a plain user); the read on `current_user`."""
    import inspect
    with pytest.raises(HTTPException) as caught:
        require_admin(user(False))
    assert caught.value.status_code == 403
    write_deps = [p.default.dependency for p in inspect.signature(settings.put_nav_visibility).parameters.values() if hasattr(p.default, "dependency")]
    read_deps = [p.default.dependency for p in inspect.signature(settings.get_nav_visibility).parameters.values() if hasattr(p.default, "dependency")]
    assert require_admin in write_deps and require_admin not in read_deps
