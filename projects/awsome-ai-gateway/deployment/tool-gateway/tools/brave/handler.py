"""Brave Search API handler for Lambda Gateway."""

import json
import time
from typing import Any, Dict

import requests

from _shared.identity import get_api_key
from _shared.response import error_response, stamp
from _shared.search_params import apply_brave
from _shared.otel import create_span
from _shared.caller_identity import extract_caller_identity


def extract_gateway_input(event: Dict[str, Any]) -> Dict[str, str]:
    """Extract query parameters from Gateway Lambda event or direct invocation."""
    if "input" in event and isinstance(event["input"], dict):
        return event["input"]
    return event


def lambda_handler(event, context):
    """Lambda handler for Brave independent web search."""
    start_time = time.time()
    import json as _json
    _ident = extract_caller_identity(event)
    print(_json.dumps({"event": "caller_identity", "engine": "brave", **_ident}))

    try:
        # Extract input from event
        input_params = extract_gateway_input(event)
        query = input_params.get("query") or input_params.get("q")
        num_results = int(input_params.get("num_results", 10))
        country = input_params.get("country", "")
        freshness = input_params.get("freshness", "")

        if not query:
            return error_response(
                "brave", int((time.time() - start_time) * 1000),
                "Missing required parameter: query")

        # Clamp num_results to contract limits
        num_results = max(1, min(num_results, 20))

        # Get API key from AgentCore Identity
        with create_span("get_brave_api_key"):
            api_key = get_api_key("brave")
            if not api_key:
                raise RuntimeError("Brave API key not available")

        # Query Brave Search API
        with create_span("query_brave"):
            headers = {
                "X-Subscription-Token": api_key,
                "Accept": "application/json",
            }
            params = {
                "q": query,
                "count": num_results,
            }
            apply_brave(params, freshness, country)

            response = requests.get(
                "https://api.search.brave.com/res/v1/web/search",
                params=params,
                headers=headers,
                timeout=10,
            )
            response.raise_for_status()
            data = response.json()

        # Return Brave's native payload (web/news/videos/infobox/…) as-is.
        latency_ms = int((time.time() - start_time) * 1000)
        return stamp(data, "brave", latency_ms)

    except requests.exceptions.RequestException as e:
        latency_ms = int((time.time() - start_time) * 1000)
        return error_response("brave", latency_ms, f"Brave API error: {str(e)}")
    except Exception as e:
        latency_ms = int((time.time() - start_time) * 1000)
        return error_response("brave", latency_ms, f"Handler error: {str(e)}")
