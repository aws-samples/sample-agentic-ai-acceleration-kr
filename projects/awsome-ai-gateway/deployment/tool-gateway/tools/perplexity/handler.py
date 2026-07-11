"""Perplexity Sonar API handler for Lambda Gateway."""

import time
from typing import Any, Dict
import requests
from _shared.identity import get_api_key
from _shared.response import normalize_response
from _shared.search_params import apply_perplexity
from _shared.otel import create_span
from _shared.caller_identity import extract_caller_identity


def extract_gateway_input(event: Dict[str, Any]) -> Dict[str, str]:
    """Extract query parameters from Gateway Lambda event or direct invocation."""
    if "input" in event and isinstance(event["input"], dict):
        return event["input"]
    return event


def lambda_handler(event, context):
    """Lambda handler for Perplexity Sonar (medium-online model)."""
    start_time = time.time()
    import json as _json
    _ident = extract_caller_identity(event)
    print(_json.dumps({"event": "caller_identity", "engine": "perplexity", **_ident}))

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
                "engine": "perplexity",
                "latency_ms": int((time.time() - start_time) * 1000),
                "error": "Missing required parameter: query",
            }

        # Clamp num_results to contract limits
        num_results = max(1, min(num_results, 20))

        # Get API key from AgentCore Identity
        with create_span("get_perplexity_api_key"):
            api_key = get_api_key("perplexity")
            if not api_key:
                raise RuntimeError("Perplexity API key not available")

        # Query Perplexity API
        with create_span("query_perplexity"):
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": "sonar-medium-online",
                "messages": [
                    {
                        "role": "user",
                        "content": query,
                    }
                ],
                "max_tokens": 500,
            }
            apply_perplexity(payload, freshness, country)

            response = requests.post(
                "https://api.perplexity.ai/chat/completions",
                json=payload,
                headers=headers,
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()

        # Extract citations from response (Perplexity provides citations in the message)
        results = []
        if "citations" in data:
            for idx, citation in enumerate(data.get("citations", [])[:num_results]):
                results.append({
                    "title": f"Result {idx + 1}",
                    "url": citation,
                    "snippet": "Online search result from Perplexity",
                })

        latency_ms = int((time.time() - start_time) * 1000)
        return normalize_response(results, "perplexity", latency_ms)

    except requests.exceptions.RequestException as e:
        latency_ms = int((time.time() - start_time) * 1000)
        return {
            "results": [],
            "engine": "perplexity",
            "latency_ms": latency_ms,
            "error": f"Perplexity API error: {str(e)}",
        }
    except Exception as e:
        latency_ms = int((time.time() - start_time) * 1000)
        return {
            "results": [],
            "engine": "perplexity",
            "latency_ms": latency_ms,
            "error": f"Handler error: {str(e)}",
        }
