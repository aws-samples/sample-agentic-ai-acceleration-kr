"""Tavily Search API handler for Lambda Gateway.

This is the Lambda-backed Tavily target. It coexists with the hosted Tavily MCP
*server* target (engine name ``tavily``); this one is named ``tavily_lambda`` to
avoid a gateway target-name collision. It reuses the same TAVILY_API_KEY.

Provides AI-powered web search with a provider-generated ``answer``.
"""

import time
from typing import Any, Dict

import requests

from _shared.identity import get_api_key
from _shared.response import normalize_response
from _shared.search_params import apply_tavily
from _shared.otel import create_span
from _shared.caller_identity import extract_caller_identity


def extract_gateway_input(event: Dict[str, Any]) -> Dict[str, str]:
    """Extract query parameters from Gateway Lambda event or direct invocation."""
    if "input" in event and isinstance(event["input"], dict):
        return event["input"]
    return event


def lambda_handler(event, context):
    """Lambda handler for Tavily web search."""
    start_time = time.time()
    import json as _json
    _ident = extract_caller_identity(event)
    print(_json.dumps({"event": "caller_identity", "engine": "tavily_lambda", **_ident}))

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
                "engine": "tavily_lambda",
                "latency_ms": int((time.time() - start_time) * 1000),
                "error": "Missing required parameter: query",
            }

        # Clamp num_results to contract limits
        num_results = max(1, min(num_results, 20))

        # Get API key from AgentCore Identity (reuses TAVILY_API_KEY)
        with create_span("get_tavily_api_key"):
            api_key = get_api_key("tavily_lambda")
            if not api_key:
                raise RuntimeError("Tavily API key not available")

        # Query Tavily Search API
        with create_span("query_tavily"):
            payload = {
                "api_key": api_key,
                "query": query,
                "search_depth": input_params.get("search_depth", "basic"),
                "topic": input_params.get("topic", "general"),
                "max_results": num_results,
                "include_answer": True,
                "include_favicon": True,
            }
            apply_tavily(payload, freshness, country)

            response = requests.post(
                "https://api.tavily.com/search",
                json=payload,
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()

        # Parse results
        raw_results = data.get("results", [])
        results = []
        for item in raw_results:
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("content", ""),
                "score": item.get("score"),
                "published_at": item.get("published_date") or None,
                "favicon": item.get("favicon") or None,
            })

        answer = data.get("answer") or None

        latency_ms = int((time.time() - start_time) * 1000)
        return normalize_response(results, "tavily_lambda", latency_ms, answer=answer)

    except requests.exceptions.RequestException as e:
        latency_ms = int((time.time() - start_time) * 1000)
        return {
            "results": [],
            "engine": "tavily_lambda",
            "latency_ms": latency_ms,
            "error": f"Tavily API error: {str(e)}",
        }
    except Exception as e:
        latency_ms = int((time.time() - start_time) * 1000)
        return {
            "results": [],
            "engine": "tavily_lambda",
            "latency_ms": latency_ms,
            "error": f"Handler error: {str(e)}",
        }
