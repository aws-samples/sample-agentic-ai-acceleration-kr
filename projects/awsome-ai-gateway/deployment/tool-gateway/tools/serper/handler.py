"""Serper (Google Search) handler for Lambda Gateway."""

import json
import time
from typing import Any, Dict

import requests

from _shared.identity import get_api_key
from _shared.response import normalize_response
from _shared.search_params import apply_serper
from _shared.otel import create_span
from _shared.caller_identity import extract_caller_identity


def extract_gateway_input(event: Dict[str, Any]) -> Dict[str, str]:
    """Extract query parameters from Gateway Lambda event or direct invocation."""
    if "input" in event and isinstance(event["input"], dict):
        return event["input"]
    return event


def lambda_handler(event, context):
    """Lambda handler for Serper (Google Search) queries."""
    start_time = time.time()
    # Stamp caller identity into the log stream so the audit view can join
    # "who" (from the inbound JWT) with "what" (the tool call). The gateway
    # does not forward claims downstream, so we recover them from the event.
    import json as _json
    _ident = extract_caller_identity(event)
    print(_json.dumps({"event": "caller_identity", "engine": "serper", **_ident}))

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
                "engine": "serper",
                "latency_ms": int((time.time() - start_time) * 1000),
                "error": "Missing required parameter: query",
            }

        # Clamp num_results to contract limits
        num_results = max(1, min(num_results, 20))

        # Get API key from AgentCore Identity
        with create_span("get_serper_api_key"):
            api_key = get_api_key("serper")
            if not api_key:
                raise RuntimeError("Serper API key not available")

        # Query Serper API
        with create_span("query_serper"):
            headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}
            payload = {
                "q": query,
                "num": num_results,
            }
            apply_serper(payload, freshness, country)

            response = requests.post(
                "https://google.serper.dev/search",
                json=payload,
                headers=headers,
                timeout=10,
            )
            response.raise_for_status()
            data = response.json()

        # Parse results
        raw_results = data.get("organic", [])
        results = []
        for item in raw_results:
            results.append({
                "title": item.get("title", ""),
                "url": item.get("link", ""),
                "snippet": item.get("snippet", ""),
                "published_at": item.get("date") or None,
            })

        latency_ms = int((time.time() - start_time) * 1000)
        return normalize_response(results, "serper", latency_ms)

    except requests.exceptions.RequestException as e:
        latency_ms = int((time.time() - start_time) * 1000)
        return {
            "results": [],
            "engine": "serper",
            "latency_ms": latency_ms,
            "error": f"Serper API error: {str(e)}",
        }
    except Exception as e:
        latency_ms = int((time.time() - start_time) * 1000)
        return {
            "results": [],
            "engine": "serper",
            "latency_ms": latency_ms,
            "error": f"Handler error: {str(e)}",
        }
