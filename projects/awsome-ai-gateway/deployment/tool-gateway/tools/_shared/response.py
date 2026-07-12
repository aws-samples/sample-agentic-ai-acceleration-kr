"""Response normalization and utilities for search handlers."""

import time
from datetime import datetime
from typing import Any, Dict, List, Optional


def normalize_response(
    results: List[Dict[str, Any]],
    engine: str,
    latency_ms: int,
    answer: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Normalize search results to the contract schema.

    Args:
        results: List of result dicts with title, url, snippet, and optional score/published_at/favicon
        engine: Engine name (serper, exa, duckduckgo, perplexity, tavily, brave)
        latency_ms: Query execution time in milliseconds
        answer: Optional provider-generated answer/summary (e.g. anthropic, tavily)

    Returns:
        Response dict matching the SearchResponse schema
    """
    normalized_results = []
    for result in results:
        normalized_results.append({
            "title": result.get("title", ""),
            "url": result.get("url", ""),
            "snippet": result.get("snippet", ""),
            "score": result.get("score"),
            "published_at": result.get("published_at"),
            "favicon": result.get("favicon"),
        })

    response = {
        "results": normalized_results,
        "engine": engine,
        "latency_ms": latency_ms,
    }

    if answer:
        response["answer"] = answer

    return response


def measure_latency(func):
    """Decorator to measure function execution time and return latency_ms."""
    def wrapper(*args, **kwargs):
        start = time.time()
        result = func(*args, **kwargs)
        latency_ms = int((time.time() - start) * 1000)
        return result, latency_ms
    return wrapper


def rfc3339_timestamp(dt: Optional[datetime] = None) -> Optional[str]:
    """
    Convert datetime to RFC3339 string (ISO 8601 format).

    Args:
        dt: datetime object (defaults to None for optional fields)

    Returns:
        RFC3339-formatted string or None
    """
    if dt is None:
        return None
    return dt.isoformat() + "Z" if not dt.isoformat().endswith("Z") else dt.isoformat()
