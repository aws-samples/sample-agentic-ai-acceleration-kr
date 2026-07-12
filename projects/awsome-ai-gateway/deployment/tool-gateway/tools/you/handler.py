"""You.com Search API handler for Lambda Gateway."""

import time
from typing import Any, Dict

import requests

from _shared.identity import get_api_key
from _shared.response import normalize_response
from _shared.search_params import apply_you
from _shared.otel import create_span
from _shared.caller_identity import extract_caller_identity


def extract_gateway_input(event: Dict[str, Any]) -> Dict[str, str]:
    """Extract query parameters from Gateway Lambda event or direct invocation."""
    if "input" in event and isinstance(event["input"], dict):
        return event["input"]
    return event


def lambda_handler(event, context):
    """Lambda handler for You.com web search."""
    start_time = time.time()
    import json as _json
    _ident = extract_caller_identity(event)
    print(_json.dumps({"event": "caller_identity", "engine": "you", **_ident}))

    try:
        # Extract input from event
        input_params = extract_gateway_input(event)
        query = input_params.get("query") or input_params.get("q")
        num_results = int(input_params.get("num_results", 10))
        country = input_params.get("country", "")
        freshness = input_params.get("freshness", "")

        if not query:
            return {
                "results": [],
                "engine": "you",
                "latency_ms": int((time.time() - start_time) * 1000),
                "error": "Missing required parameter: query",
            }

        # Clamp num_results to contract limits
        num_results = max(1, min(num_results, 20))

        # Get API key from AgentCore Identity
        with create_span("get_you_api_key"):
            api_key = get_api_key("you")
            if not api_key:
                raise RuntimeError("You.com API key not available")

        # Query You.com Search API
        with create_span("query_you"):
            headers = {"X-API-Key": api_key}
            params = {"query": query, "count": num_results}
            apply_you(params, freshness, country)

            response = requests.get(
                "https://ydc-index.io/v1/search",
                params=params,
                headers=headers,
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()

        # Parse results
        raw_results = (data.get("results") or {}).get("web", [])
        results = []
        for item in raw_results:
            snippets = item.get("snippets") or []
            snippet = snippets[0] if snippets else item.get("description", "")
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": snippet,
                "favicon": item.get("favicon_url") or None,
            })

        latency_ms = int((time.time() - start_time) * 1000)
        return normalize_response(results, "you", latency_ms)

    except requests.exceptions.RequestException as e:
        latency_ms = int((time.time() - start_time) * 1000)
        return {
            "results": [],
            "engine": "you",
            "latency_ms": latency_ms,
            "error": f"You.com API error: {str(e)}",
        }
    except Exception as e:
        latency_ms = int((time.time() - start_time) * 1000)
        return {
            "results": [],
            "engine": "you",
            "latency_ms": latency_ms,
            "error": f"Handler error: {str(e)}",
        }
