"""Firecrawl Search API handler for Lambda Gateway."""

import time
from typing import Any, Dict

import requests

from _shared.identity import get_api_key
from _shared.response import error_response, stamp
from _shared.search_params import apply_firecrawl
from _shared.otel import create_span
from _shared.caller_identity import extract_caller_identity


def extract_gateway_input(event: Dict[str, Any]) -> Dict[str, str]:
    """Extract query parameters from Gateway Lambda event or direct invocation."""
    if "input" in event and isinstance(event["input"], dict):
        return event["input"]
    return event


def lambda_handler(event, context):
    """Lambda handler for Firecrawl web search."""
    start_time = time.time()
    import json as _json
    _ident = extract_caller_identity(event)
    print(_json.dumps({"event": "caller_identity", "engine": "firecrawl", **_ident}))

    try:
        # Extract input from event
        input_params = extract_gateway_input(event)
        query = input_params.get("query") or input_params.get("q")
        num_results = int(input_params.get("num_results", 10))
        country = input_params.get("country", "")
        freshness = input_params.get("freshness", "")
        include_domains = input_params.get("include_domains")
        exclude_domains = input_params.get("exclude_domains")

        if not query:
            return error_response(
                "firecrawl", int((time.time() - start_time) * 1000),
                "Missing required parameter: query")

        # Clamp num_results to contract limits
        num_results = max(1, min(num_results, 20))

        # Get API key from AgentCore Identity
        with create_span("get_firecrawl_api_key"):
            api_key = get_api_key("firecrawl")
            if not api_key:
                raise RuntimeError("Firecrawl API key not available")

        # Query Firecrawl Search API
        with create_span("query_firecrawl"):
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            payload = {"query": query, "limit": num_results}
            apply_firecrawl(payload, freshness, country,
                            include_domains=include_domains, exclude_domains=exclude_domains)

            response = requests.post(
                "https://api.firecrawl.dev/v1/search",
                json=payload,
                headers=headers,
                timeout=45,
            )
            response.raise_for_status()
            data = response.json()

        # Return Firecrawl's native payload (data[] with markdown/metadata/…) as-is.
        latency_ms = int((time.time() - start_time) * 1000)
        return stamp(data, "firecrawl", latency_ms)

    except requests.exceptions.RequestException as e:
        latency_ms = int((time.time() - start_time) * 1000)
        return error_response("firecrawl", latency_ms, f"Firecrawl API error: {str(e)}")
    except Exception as e:
        latency_ms = int((time.time() - start_time) * 1000)
        return error_response("firecrawl", latency_ms, f"Handler error: {str(e)}")
