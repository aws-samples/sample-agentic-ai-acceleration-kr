"""No route handler may be `async def` without awaiting anything.

Such a handler runs on the event loop, and every handler here calls blocking boto3,
so one slow AWS call stalls every request in the process. Under load this is what
made sign-in fall to 3 logins/s with 42% 504s (200 concurrent users, 2026-10-18):
`/api/auth/login` built a Cognito client and called it on the loop. A plain `def`
handler is dispatched to FastAPI's threadpool instead.

Handlers that genuinely await (the SSE streams) stay async; the check is purely
structural so a new router cannot reintroduce the stall unnoticed.
"""
import ast
from pathlib import Path

ROUTES = Path(__file__).resolve().parent.parent / "routes"
VERBS = {"get", "post", "put", "patch", "delete", "head", "options", "api_route"}
# Trivial handlers that touch no I/O at all.
ALLOWED = {("health.py", "root")}


def _route_handlers(tree):
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for deco in node.decorator_list:
            if isinstance(deco, ast.Call) and getattr(deco.func, "attr", "") in VERBS:
                yield node
                break


def _awaits(node) -> bool:
    return any(isinstance(n, (ast.Await, ast.AsyncFor, ast.AsyncWith)) for n in ast.walk(node))


def test_async_route_handlers_actually_await():
    offenders = []
    for path in sorted(ROUTES.glob("*.py")):
        tree = ast.parse(path.read_text())
        for handler in _route_handlers(tree):
            if (path.name, handler.name) in ALLOWED:
                continue
            if not _awaits(handler):
                offenders.append(f"{path.name}:{handler.lineno} {handler.name}")
    assert offenders == [], (
        "async route handlers that never await run blocking calls on the event loop; "
        "make them plain `def`: " + ", ".join(offenders)
    )
