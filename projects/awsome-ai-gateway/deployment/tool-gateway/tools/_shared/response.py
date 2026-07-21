"""Response stamping for search handlers.

Each engine returns its provider's native payload as-is; we only stamp two
metadata fields the rest of the stack reads:

  - ``engine``     — which target produced this (audit / trace summary).
  - ``latency_ms`` — end-to-end handler time.

The gateway feeds this JSON to the model VERBATIM (raw_text), so preserving the
provider's own shape keeps every provider's richest fields intact — Perplexity's
synthesized answer, Tavily's ``answer``, Exa highlights — instead of flattening
them into one lowest-common-denominator schema.
"""

from typing import Any, Dict


def stamp(payload: Dict[str, Any], engine: str, latency_ms: int) -> Dict[str, Any]:
    """Return the provider payload with ``engine``/``latency_ms`` added.

    A shallow copy is made so the caller's dict is not mutated. The provider's
    own keys win only if they collide with our metadata — they never do in
    practice, but we stamp last to guarantee the metadata is present.
    """
    out = dict(payload)
    out["engine"] = engine
    out["latency_ms"] = latency_ms
    return out


def error_response(engine: str, latency_ms: int, error: str) -> Dict[str, Any]:
    """Uniform error envelope (the one shape we DO keep consistent)."""
    return {"engine": engine, "latency_ms": latency_ms, "error": error}
