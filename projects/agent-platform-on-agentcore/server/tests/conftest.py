"""Shared test setup.

The registry listing cache, the org usage cache and the Insights dashboard
cache are process-wide, as they are
in production, and tests reuse one registry id and one usage fake with different
data. Each test starts with both empty so one test's listing cannot answer the
next. Looked up through sys.modules so the fixture imports nothing a test would
not already have imported.
"""
import sys

import pytest


@pytest.fixture(autouse=True)
def _fresh_shared_caches():
    def clear():
        registry = sys.modules.get("services.registry_service")
        if registry is not None:
            registry.LISTINGS.invalidate()
            registry.CHATTABLE.invalidate()
        org = sys.modules.get("routes.org")
        if org is not None:
            org._USAGE_CACHE.invalidate()
        insights = sys.modules.get("routes.insights")
        if insights is not None:
            insights._DASHBOARDS.invalidate()

    clear()
    yield
    clear()
