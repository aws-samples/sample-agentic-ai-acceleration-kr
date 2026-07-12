"""Anthropic Claude built-in web search handler for Lambda Gateway.

Wraps the Claude Messages API server-side ``web_search`` tool. Bedrock Claude
lacks the built-in web_search tool, so this target lets the Playground compare
Claude's first-party web search against the other providers. The Anthropic API
key comes from the ANTHROPIC_API_KEY env var (AgentCore Identity fallback).

Note: Claude's web_search returns result url/title/page_age but the page body is
encrypted_content (only decryptable inside Claude's context), so there is no
plaintext snippet. We return Claude's native Messages response as-is — its
content blocks carry both the synthesized text and the web_search_tool_result
citations — so the model downstream sees the full, unflattened payload.
"""

import os
import time
from typing import Any, Dict

import requests

from _shared.identity import get_api_key
from _shared.response import error_response, stamp
from _shared.otel import create_span
from _shared.caller_identity import extract_caller_identity
from _shared.search_params import normalize_country, normalize_domains

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


def _build_web_search_tool(country, include_domains, exclude_domains):
    """Build the Anthropic web_search tool config with optional localization/domains.

    Anthropic's web_search tool has no freshness/recency parameter, so it is not
    accepted here. allowed_domains and blocked_domains are mutually exclusive;
    include (allowed) wins when both are provided.
    """
    tool = {"type": WEB_SEARCH_TOOL_TYPE, "name": "web_search", "max_uses": 1}
    if country:
        tool["user_location"] = {"type": "approximate", "country": country}
    if include_domains:
        tool["allowed_domains"] = include_domains
    elif exclude_domains:
        tool["blocked_domains"] = exclude_domains
    return tool


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
        country = normalize_country(input_params.get("country", ""))
        include_domains = normalize_domains(input_params.get("include_domains"))
        exclude_domains = normalize_domains(input_params.get("exclude_domains"))

        if not query:
            return error_response(
                "anthropic", int((time.time() - start_time) * 1000),
                "Missing required parameter: query")

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
                "tools": [_build_web_search_tool(country, include_domains, exclude_domains)],
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

        # Return Claude's native Messages response (content[] with text and
        # web_search_tool_result blocks) as-is.
        latency_ms = int((time.time() - start_time) * 1000)
        return stamp(data, "anthropic", latency_ms)

    except requests.exceptions.RequestException as e:
        latency_ms = int((time.time() - start_time) * 1000)
        return error_response("anthropic", latency_ms, f"Anthropic API error: {str(e)}")
    except Exception as e:
        latency_ms = int((time.time() - start_time) * 1000)
        return error_response("anthropic", latency_ms, f"Handler error: {str(e)}")
