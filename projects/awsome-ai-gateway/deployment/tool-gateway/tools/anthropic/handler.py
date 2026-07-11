"""Anthropic Claude built-in web search handler for Lambda Gateway.

Wraps the Claude Messages API server-side ``web_search`` tool. Bedrock Claude
lacks the built-in web_search tool, so this target lets the Playground compare
Claude's first-party web search against the other providers. The Anthropic API
key comes from the ANTHROPIC_API_KEY env var (AgentCore Identity fallback).

Note: Claude's web_search returns result url/title/page_age but the page body is
encrypted_content (only decryptable inside Claude's context), so there is no
plaintext snippet — it is left empty and page_age is surfaced as published_at
instead. The model's synthesized text is returned as the ``answer`` field.
"""

import os
import time
from typing import Any, Dict

import requests

from _shared.identity import get_api_key
from _shared.response import normalize_response
from _shared.otel import create_span
from _shared.caller_identity import extract_caller_identity

ANTHROPIC_VERSION = "2023-06-01"
# Cheapest model that supports the web_search tool; override via env.
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")
WEB_SEARCH_TOOL_TYPE = os.environ.get("ANTHROPIC_WEB_SEARCH_TYPE", "web_search_20250305")
MAX_TOKENS = int(os.environ.get("ANTHROPIC_MAX_TOKENS", "1024"))


def extract_gateway_input(event: Dict[str, Any]) -> Dict[str, str]:
    """Extract query parameters from Gateway Lambda event or direct invocation."""
    if "input" in event and isinstance(event["input"], dict):
        return event["input"]
    return event


def lambda_handler(event, context):
    """Lambda handler for Anthropic Claude built-in web search."""
    start_time = time.time()
    import json as _json
    _ident = extract_caller_identity(event)
    print(_json.dumps({"event": "caller_identity", "engine": "anthropic", **_ident}))

    try:
        # Extract input from event
        input_params = extract_gateway_input(event)
        query = input_params.get("query") or input_params.get("q")
        num_results = int(input_params.get("num_results", 10))

        if not query:
            return {
                "results": [],
                "engine": "anthropic",
                "latency_ms": int((time.time() - start_time) * 1000),
                "error": "Missing required parameter: query",
            }

        # Clamp num_results to contract limits
        num_results = max(1, min(num_results, 20))

        # Get API key from AgentCore Identity
        with create_span("get_anthropic_api_key"):
            api_key = get_api_key("anthropic")
            if not api_key:
                raise RuntimeError("Anthropic API key not available")

        # Query Anthropic Messages API with the server-side web_search tool
        with create_span("query_anthropic"):
            headers = {
                "x-api-key": api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            }
            payload = {
                "model": MODEL,
                "max_tokens": MAX_TOKENS,
                "tools": [
                    {"type": WEB_SEARCH_TOOL_TYPE, "name": "web_search", "max_uses": 1}
                ],
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            f"Use the web_search tool to find current web results for: {query}\n"
                            "Run a single search, then briefly summarize the findings."
                        ),
                    }
                ],
            }

            response = requests.post(
                "https://api.anthropic.com/v1/messages",
                json=payload,
                headers=headers,
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()

        # Parse content blocks: text -> answer, web_search_tool_result -> results
        content = data.get("content") or []
        results = []
        answer_parts = []
        for block in content:
            btype = block.get("type")
            if btype == "text":
                text = block.get("text", "")
                if text:
                    answer_parts.append(text)
            elif btype == "web_search_tool_result":
                hits = block.get("content")
                # An error during search comes back as a dict, not a list.
                if not isinstance(hits, list):
                    continue
                for hit in hits:
                    if hit.get("type") != "web_search_result":
                        continue
                    # Claude's web_search has no plaintext snippet (page body is
                    # encrypted_content), but it does expose page_age as the
                    # publish date. Surface it as published_at, not snippet.
                    results.append({
                        "title": hit.get("title", ""),
                        "url": hit.get("url", ""),
                        "snippet": "",
                        "published_at": hit.get("page_age") or None,
                    })

        results = results[:num_results]
        answer = "\n".join(answer_parts).strip() or None

        latency_ms = int((time.time() - start_time) * 1000)
        return normalize_response(results, "anthropic", latency_ms, answer=answer)

    except requests.exceptions.RequestException as e:
        latency_ms = int((time.time() - start_time) * 1000)
        return {
            "results": [],
            "engine": "anthropic",
            "latency_ms": latency_ms,
            "error": f"Anthropic API error: {str(e)}",
        }
    except Exception as e:
        latency_ms = int((time.time() - start_time) * 1000)
        return {
            "results": [],
            "engine": "anthropic",
            "latency_ms": latency_ms,
            "error": f"Handler error: {str(e)}",
        }
