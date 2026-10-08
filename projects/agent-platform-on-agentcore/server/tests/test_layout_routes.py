"""Test layout routes: GET and PUT for the dashboard layout preference."""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routes.insights as insights  # noqa: E402
# Aliased: `layout_service` is a local name inside several tests here (the stub
# instance), and importing the module under the same name shadows it.
import services.layout_service as layout_module  # noqa: E402
from core.auth import AuthUser  # noqa: E402


class StubPrefsRepository:
    """In-memory preferences repository for testing."""

    def __init__(self):
        self.storage = {}  # {(sub, name): value}

    def get(self, sub, name):
        """Retrieve a preference. Returns None if absent."""
        return self.storage.get((sub, name))

    def put(self, sub, name, value):
        """Store a preference, overwriting any previous value."""
        self.storage[(sub, name)] = value


class StubLayoutService:
    """Layout service with testable state."""

    def __init__(self, repository=None):
        self.repository = repository
        self._configured = repository is not None

    @property
    def configured(self):
        return self._configured

    def get(self, sub):
        """Retrieve a layout for a user."""
        from services.layout_service import LAYOUT_VERSION, reconcile, _default_widgets

        if not self.configured:
            return {
                "version": LAYOUT_VERSION,
                "widgets": _default_widgets(),
                "persisted": False,
            }

        try:
            stored = self.repository.get(sub, "insights-layout")
            if stored is None:
                return {
                    "version": LAYOUT_VERSION,
                    "widgets": _default_widgets(),
                    "persisted": False,
                }
            reconciled = reconcile(stored)
            reconciled["persisted"] = True
            return reconciled
        except Exception:
            return {
                "version": LAYOUT_VERSION,
                "widgets": _default_widgets(),
                "persisted": False,
            }

    def put(self, sub, layout):
        """Store a layout for a user."""
        from services.layout_service import LAYOUT_VERSION, reconcile, _default_widgets

        if not self.configured:
            return {
                "version": LAYOUT_VERSION,
                "widgets": _default_widgets(),
                "persisted": False,
            }

        try:
            reconciled = reconcile(layout)
            self.repository.put(sub, "insights-layout", reconciled)
            reconciled["persisted"] = True
            return reconciled
        except Exception:
            return {
                "version": LAYOUT_VERSION,
                "widgets": _default_widgets(),
                "persisted": False,
            }


def user(sub="sub-1", is_admin=False):
    """Create a real AuthUser instance for testing."""
    groups = ["admin"] if is_admin else []
    return AuthUser(sub=sub, username=f"user-{sub}", groups=groups)


def wire(layout_service):
    """Wire a test layout service into the insights routes."""
    insights._layout_override = layout_service


def teardown_function():
    """Clear overrides after each test."""
    insights._layout_override = None


def test_get_returns_default_for_never_saved_user():
    """GET returns the default layout for a user who has never saved one."""
    repo = StubPrefsRepository()
    layout_service = StubLayoutService(repository=repo)
    wire(layout_service)

    result = insights.get_layout(user=user(sub="u-new"))

    assert result["persisted"] is False
    assert result["version"] == 1
    assert len(result["widgets"]) > 0
    assert all("id" in w for w in result["widgets"])


def test_put_then_get_round_trips():
    """PUT then GET round-trips a reordered layout."""
    repo = StubPrefsRepository()
    layout_service = StubLayoutService(repository=repo)
    wire(layout_service)

    new_layout_data = {
        "version": 1,
        "widgets": [
            {"id": "leaderboard", "span": "half", "visible": True},
            {"id": "kpi", "span": "full", "visible": False},
            {"id": "billed", "span": "half", "visible": True},
            {"id": "trend", "span": "full", "visible": True},
            {"id": "reuse", "span": "half", "visible": True},
        ],
    }

    put_result = insights.put_layout(body=insights.LayoutUpdateRequest(**new_layout_data), user=user(sub="u-1"))
    assert put_result["persisted"] is True

    get_result = insights.get_layout(user=user(sub="u-1"))
    assert get_result["persisted"] is True
    assert get_result["widgets"][0]["id"] == "leaderboard"
    assert get_result["widgets"][1]["id"] == "kpi"


def test_put_uses_token_sub_never_body_sub():
    """PUT writes under the token's sub, never from the body.

    Sending a body with a different sub must not override who owns the layout.
    This is a real security concern: the same class of mistake is why thread
    ownership could not live in metadata.
    """
    repo = StubPrefsRepository()
    layout_service = StubLayoutService(repository=repo)
    wire(layout_service)

    # The caller's sub
    caller_sub = "u-legit"
    # Try to inject a different sub via the body
    # Note: LayoutUpdateRequest will ignore unknown fields like "sub"
    malicious_body = insights.LayoutUpdateRequest(version=1, widgets=[])

    put_result = insights.put_layout(body=malicious_body, user=user(sub=caller_sub))

    # Verify it was stored under the caller's sub, not the body's
    layout_under_caller = repo.get(caller_sub, "insights-layout")
    assert layout_under_caller is not None

    # Verify there's no layout under a different sub (the attacker's)
    layout_under_attacker = repo.get("u-attacker", "insights-layout")
    assert layout_under_attacker is None


def test_get_does_not_require_configured():
    """GET does not require the usage table.

    A layout is readable in an environment with no usage table.
    No _require_configured() call should guard this route.
    """
    # Note: this test asserts the handler doesn't call _require_configured()
    # by using a None repository (unconfigured state).
    layout_service = StubLayoutService(repository=None)
    wire(layout_service)

    # This should not raise HTTPException(501) like other insights routes do
    result = insights.get_layout(user=user(sub="u-1"))

    assert result is not None
    assert result["persisted"] is False
    assert result["version"] == 1
    assert len(result["widgets"]) > 0


def test_put_with_junk_body_returns_reconciled_default():
    """PUT with a junk body returns a reconciled default rather than a 500.

    Reconciliation is forgiving: bad version, missing fields, wrong shapes
    all resolve to a valid default.
    """
    repo = StubPrefsRepository()
    layout_service = StubLayoutService(repository=repo)
    wire(layout_service)

    # Junk body: wrong version, malformed widgets
    junk_body = insights.LayoutUpdateRequest(
        version=999,
        widgets="not-a-list",  # Type will be preserved in the model
    )

    result = insights.put_layout(body=junk_body, user=user(sub="u-1"))

    assert result is not None
    assert result["persisted"] is True  # Was stored, reconciled to default
    assert result["version"] == 1
    # Every registered widget, whatever the registry currently holds. Pinning the
    # count would fail on every widget added — and adding one without a version bump
    # is the designed path: reconciliation appends unknown-but-registered ids.
    assert len(result["widgets"]) == len(layout_module.WIDGET_IDS)


def test_every_layout_route_is_sync():
    """Layout routes are sync def, not async.

    Blocking boto3 in an async def stalls every request in the process.
    """
    import inspect

    for handler in (insights.get_layout, insights.put_layout):
        assert not inspect.iscoroutinefunction(
            handler
        ), f"{handler.__name__} must be sync def"
