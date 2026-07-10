# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Lock down middleware execution order — this is load-bearing for FR-3.6 and FR-4.x.

Dependencies that REQUIRE this exact runtime order:
- DowngradeMiddleware reads state["budget_status"].threshold_pct (set by BudgetMiddleware)
- RateLimitMiddleware reads state["budget_status"].throttle_active / throttle_rpm_pct
  (set by BudgetMiddleware) — silently no-op if BudgetMiddleware runs after.

If this test fails, do NOT just update the expected list — verify that the new order
preserves data dependencies. The previous bug (RateLimit before Budget) went undetected
for weeks until FR-3.6 wiring forced an audit.
"""

from app.main import app

EXPECTED_RUNTIME_ORDER = [
    "OTelMiddleware",
    "ClientIdentificationMiddleware",
    "AuthMiddleware",
    "ClientAuthorizationMiddleware",
    "BudgetMiddleware",
    "DowngradeMiddleware",
    "RateLimitMiddleware",
    "HeaderInjectorMiddleware",
]


def test_middleware_runtime_order():
    """app.user_middleware lists middleware in execution order (outermost first).

    The list contains starlette's BaseHTTPMiddleware wrapper at index 0
    plus all user-registered middleware in execution order.
    """
    names = [m.cls.__name__ for m in app.user_middleware]

    # Filter out any starlette internals (BaseHTTPMiddleware wrapper from FastAPI)
    user_named = [n for n in names if n in EXPECTED_RUNTIME_ORDER]

    assert user_named == EXPECTED_RUNTIME_ORDER, (
        f"Middleware order changed. Expected {EXPECTED_RUNTIME_ORDER}, got {user_named}.\n"
        f"Full app.user_middleware: {names}\n"
        "If you intentionally reordered, ensure data dependencies still hold:\n"
        " - DowngradeMiddleware needs Budget to run first\n"
        " - RateLimitMiddleware needs Budget to run first"
    )
