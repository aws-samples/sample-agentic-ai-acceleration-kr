"""Exa API handler for Lambda Gateway."""

import json
import time
from typing import Any, Dict

import requests

from _shared.identity import get_api_key
from _shared.response import normalize_response
from _shared.search_params import apply_exa
from _shared.otel import create_span
from _shared.caller_identity import extract_caller_identity


def extract_gateway_input(event: Dict[str, Any]) -> Dict[str, str]:
    """Extract query parameters from Gateway Lambda event or direct invocation."""
    if "input" in event and isinstance(event["input"], dict):
        return event["input"]
    return event


def lambda_handler(event, context):
    """Lambda handler for Exa web search (deterministic, no auto-prompt)."""
    start_time = time.time()
    import json as _json
    _ident = extract_caller_identity(event)
    print(_json.dumps({"event": "caller_identity", "engine": "exa", **_ident}))

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
                "engine": "exa",
                "latency_ms": int((time.time() - start_time) * 1000),
                "error": "Missing required parameter: query",
            }

        # Clamp num_results to contract limits
        num_results = max(1, min(num_results, 20))

        # Get API key from AgentCore Identity
        with create_span("get_exa_api_key"):
            api_key = get_api_key("exa")
            if not api_key:
                raise RuntimeError("Exa API key not available")

        # Query Exa API with auto-prompt disabled
        with create_span("query_exa"):
            headers = {"x-api-key": api_key, "Content-Type": "application/json"}
            payload = {
                "query": query,
                "numResults": num_results,
                "useAutoprompt": False,  # Deterministic results
            }
            apply_exa(payload, freshness, country)

            response = requests.post(
                "https://api.exa.ai/search",
                json=payload,
                headers=headers,
                timeout=10,
            )
            response.raise_for_status()
            data = response.json()

        # Parse results
        raw_results = data.get("results", [])
        results = []
        for item in raw_results:
            # Exa's /search response carries no relevance score, so we omit it
            # (it would always be null). publishedDate/favicon are always present.
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("text", ""),
                "published_at": item.get("publishedDate") or None,
                "favicon": item.get("favicon") or None,
            })

        latency_ms = int((time.time() - start_time) * 1000)
        return normalize_response(results, "exa", latency_ms)

    except requests.exceptions.RequestException as e:
        latency_ms = int((time.time() - start_time) * 1000)
        return {
            "results": [],
            "engine": "exa",
            "latency_ms": latency_ms,
            "error": f"Exa API error: {str(e)}",
        }
    except Exception as e:
        latency_ms = int((time.time() - start_time) * 1000)
        return {
            "results": [],
            "engine": "exa",
            "latency_ms": latency_ms,
            "error": f"Handler error: {str(e)}",
        }
