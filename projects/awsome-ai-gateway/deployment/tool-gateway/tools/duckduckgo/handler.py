"""DuckDuckGo handler for Lambda Gateway (no API key required)."""

import time
from typing import Any, Dict

try:
    # Package renamed from `duckduckgo-search` to `ddgs`; the old `d.js`
    # endpoint now returns "202 Ratelimit" so we must use the new client.
    from ddgs import DDGS
    DDGS_AVAILABLE = True
except ImportError:
    DDGS_AVAILABLE = False

from _shared.response import error_response, stamp
from _shared.search_params import ddg_kwargs
from _shared.otel import create_span
from _shared.caller_identity import extract_caller_identity


def extract_gateway_input(event: Dict[str, Any]) -> Dict[str, str]:
    """Extract query parameters from Gateway Lambda event or direct invocation."""
    if "input" in event and isinstance(event["input"], dict):
        return event["input"]
    return event


def lambda_handler(event, context):
    """Lambda handler for DuckDuckGo web search (no API key)."""
    start_time = time.time()
    import json as _json
    _ident = extract_caller_identity(event)
    print(_json.dumps({"event": "caller_identity", "engine": "duckduckgo", **_ident}))

    try:
        if not DDGS_AVAILABLE:
            raise RuntimeError("duckduckgo-search library not installed")

        # Extract input from event
        input_params = extract_gateway_input(event)
        query = input_params.get("query") or input_params.get("q")
        num_results = int(input_params.get("num_results", 10))
        country = input_params.get("country", "")
        freshness = input_params.get("freshness", "")

        if not query:
            return error_response(
                "duckduckgo", int((time.time() - start_time) * 1000),
                "Missing required parameter: query")

        # Clamp num_results to contract limits
        num_results = max(1, min(num_results, 20))

        # Query DuckDuckGo
        with create_span("query_duckduckgo"):
            ddgs = DDGS(timeout=10)
            raw_results = ddgs.text(query, max_results=num_results, **ddg_kwargs(freshness, country))

        # DDGS returns a bare list of {title, href, body}; wrap it (no provider
        # envelope exists) but keep the items' native keys verbatim.
        latency_ms = int((time.time() - start_time) * 1000)
        return stamp({"results": list(raw_results)}, "duckduckgo", latency_ms)

    except Exception as e:
        latency_ms = int((time.time() - start_time) * 1000)
        return error_response("duckduckgo", latency_ms, f"Handler error: {str(e)}")
