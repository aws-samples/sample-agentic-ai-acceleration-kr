"""Translate normalized search inputs into each engine's native parameters.

The unified MCP tool schema exposes two localization/recency knobs that most
engines support, but every provider names them differently and accepts a
different value format:

    freshness: one of "day" | "week" | "month" | "year"
    country:   ISO 3166-1 alpha-2 code, e.g. "US", "KR"

Each ``apply_*`` helper mutates (and returns) the provider request dict/params
in that provider's own dialect. Unsupported combinations are silently skipped so
a caller can always pass the normalized values through without branching.

Anthropic's web_search tool exposes neither knob, so it has no helper here.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from .constants import FRESHNESS_VALUES

# Normalized freshness -> number of days back, used by engines that only accept
# an absolute published-date range (Exa).
_FRESHNESS_DAYS = {"day": 1, "week": 7, "month": 31, "year": 365}

# Brave: freshness=pd|pw|pm|py (https://api-dashboard.search.brave.com)
_BRAVE_FRESHNESS = {"day": "pd", "week": "pw", "month": "pm", "year": "py"}

# Serper / Firecrawl (Google "tbs"): qdr:d|w|m|y
_GOOGLE_TBS = {"day": "qdr:d", "week": "qdr:w", "month": "qdr:m", "year": "qdr:y"}

# DuckDuckGo timelimit: d|w|m|y
_DDG_TIMELIMIT = {"day": "d", "week": "w", "month": "m", "year": "y"}

# Tavily country expects the full lowercase country name, not an ISO code.
# Only the codes we realistically localize for are mapped; unknown codes are
# dropped (Tavily would 422 on an unrecognized value).
_TAVILY_COUNTRY = {
    "US": "united states", "KR": "south korea", "JP": "japan",
    "GB": "united kingdom", "UK": "united kingdom", "DE": "germany",
    "FR": "france", "CA": "canada", "AU": "australia", "IN": "india",
    "CN": "china", "BR": "brazil", "ES": "spain", "IT": "italy",
}

# DuckDuckGo region is "<country>-<lang>"; map the country half for the codes
# we support. Falls back to worldwide ("wt-wt") when unknown.
_DDG_REGION = {
    "US": "us-en", "KR": "kr-ko", "JP": "jp-ja", "GB": "uk-en", "UK": "uk-en",
    "DE": "de-de", "FR": "fr-fr", "CA": "ca-en", "AU": "au-en", "IN": "in-en",
    "CN": "cn-zh", "BR": "br-pt", "ES": "es-es", "IT": "it-it",
}

# SearXNG has no country knob; it filters by UI `language`. Map the country code
# to a best-effort language for the codes we localize for. Unknown codes are
# dropped so SearXNG falls back to its configured default.
_SEARXNG_LANGUAGE = {
    "US": "en", "GB": "en", "UK": "en", "CA": "en", "AU": "en", "IN": "en",
    "KR": "ko", "JP": "ja", "CN": "zh", "DE": "de", "FR": "fr", "ES": "es",
    "IT": "it", "BR": "pt",
}


def normalize_freshness(value: Optional[str]) -> Optional[str]:
    """Return the freshness value if valid, else None."""
    if not value:
        return None
    v = str(value).strip().lower()
    return v if v in FRESHNESS_VALUES else None


def normalize_country(value: Optional[str]) -> Optional[str]:
    """Return an uppercased 2-letter country code if plausible, else None."""
    if not value:
        return None
    v = str(value).strip().upper()
    return v if len(v) == 2 and v.isalpha() else None


def apply_brave(params: Dict[str, Any], freshness: Optional[str], country: Optional[str]) -> Dict[str, Any]:
    f = normalize_freshness(freshness)
    if f:
        params["freshness"] = _BRAVE_FRESHNESS[f]
    c = normalize_country(country)
    if c:
        params["country"] = c
    return params


def apply_serper(payload: Dict[str, Any], freshness: Optional[str], country: Optional[str]) -> Dict[str, Any]:
    f = normalize_freshness(freshness)
    if f:
        payload["tbs"] = _GOOGLE_TBS[f]
    c = normalize_country(country)
    if c:
        payload["gl"] = c.lower()
    return payload


def apply_firecrawl(payload: Dict[str, Any], freshness: Optional[str], country: Optional[str]) -> Dict[str, Any]:
    f = normalize_freshness(freshness)
    if f:
        payload["tbs"] = _GOOGLE_TBS[f]
    c = normalize_country(country)
    if c:
        payload["country"] = c
    return payload


def apply_exa(payload: Dict[str, Any], freshness: Optional[str], country: Optional[str],
              now: Optional[datetime] = None) -> Dict[str, Any]:
    """Exa has no freshness enum; it filters by absolute publish date range."""
    f = normalize_freshness(freshness)
    if f:
        ref = now or datetime.now(timezone.utc)
        start = ref - timedelta(days=_FRESHNESS_DAYS[f])
        payload["startPublishedDate"] = start.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    c = normalize_country(country)
    if c:
        payload["userLocation"] = c
    return payload


def apply_tavily(payload: Dict[str, Any], freshness: Optional[str], country: Optional[str]) -> Dict[str, Any]:
    f = normalize_freshness(freshness)
    if f:
        payload["time_range"] = f
    c = normalize_country(country)
    if c and c in _TAVILY_COUNTRY:
        payload["country"] = _TAVILY_COUNTRY[c]
    return payload


def apply_you(params: Dict[str, Any], freshness: Optional[str], country: Optional[str]) -> Dict[str, Any]:
    f = normalize_freshness(freshness)
    if f:
        params["freshness"] = f  # You.com accepts day|week|month|year directly
    c = normalize_country(country)
    if c:
        params["country"] = c
    return params


def apply_perplexity(payload: Dict[str, Any], freshness: Optional[str], country: Optional[str]) -> Dict[str, Any]:
    f = normalize_freshness(freshness)
    if f:
        payload["search_recency_filter"] = f  # top-level: hour|day|week|month|year
    c = normalize_country(country)
    if c:
        # Perplexity takes country nested under web_search_options, not top-level.
        opts = payload.setdefault("web_search_options", {})
        opts.setdefault("user_location", {})["country"] = c
    return payload


def apply_searxng(params: Dict[str, Any], freshness: Optional[str], country: Optional[str]) -> Dict[str, Any]:
    f = normalize_freshness(freshness)
    if f:
        params["time_range"] = f  # SearXNG accepts day|week|month|year directly
    c = normalize_country(country)
    if c and c in _SEARXNG_LANGUAGE:
        params["language"] = _SEARXNG_LANGUAGE[c]
    return params


def ddg_kwargs(freshness: Optional[str], country: Optional[str]) -> Dict[str, Any]:
    """Build the keyword args for DDGS().text() (timelimit + region)."""
    kwargs: Dict[str, Any] = {}
    f = normalize_freshness(freshness)
    if f:
        kwargs["timelimit"] = _DDG_TIMELIMIT[f]
    c = normalize_country(country)
    if c:
        kwargs["region"] = _DDG_REGION.get(c, "wt-wt")
    return kwargs
