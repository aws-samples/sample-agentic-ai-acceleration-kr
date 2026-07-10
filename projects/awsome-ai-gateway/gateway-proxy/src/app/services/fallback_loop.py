# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Availability fallback loop helper for the /v1/messages route.

Wraps the per-candidate invoke with:
  - Circuit-breaker gating (is_open / half-open probe)
  - Key-scope and rate-limit enforcement per candidate
  - TPM+cost reservation unwind on 5xx/timeout
  - Circuit-breaker record_failure / record_success

Returns a `FallbackResult` that carries enough information for the caller
(messages.py) to build its normal streaming or non-streaming response.

Design contract
---------------
- Only BEDROCK candidates produce a real fallback chain (the FallbackResolver
  only adds same-provider candidates).  A BEDROCK_MANTLE original resolves to
  just [original] because resolve() never crosses provider boundaries.
- The caller must supply `try_order` from FallbackResolver.resolve().
- For STREAMING the initial (status, chunk_iter) is returned BEFORE any bytes
  are yielded to the client, so a bad status can still trigger fallback.
- Mid-stream failures are NOT caught here — they propagate to the client.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import structlog

from app.schemas.domain import ModelConfigSchema, TokenUsage
from app.services.circuit_breaker import CircuitBreakerService
from app.services.rate_limit_enforcement import enforce_rate_limits
from app.services.rate_limit_service import RateLimitService

logger = structlog.get_logger(__name__)

# HTTP status codes that trigger a fallback attempt
_FALLBACK_STATUSES = frozenset({502, 503, 504})
# Among fallback statuses, only these count as a CB failure
# (504 = ModelTimeoutException = per-prompt fault, do NOT penalise the circuit)
_CB_FAILURE_STATUSES = frozenset({502, 503})


@dataclass
class FallbackResult:
    """Outcome of `run_fallback_loop`."""

    status: int
    # Non-streaming: (bytes body, {headers}, TokenUsage)
    # Streaming: (AsyncIterator[bytes], {headers}, str|None request-id)
    payload: tuple[Any, dict, Any]
    model_config: ModelConfigSchema
    availability_fallback_from: str | None = None
    # True when every candidate was circuit-open (no invoke attempted)
    all_open: bool = False


async def _unwind_reservation(
    *,
    redis,
    state: dict,
    auth_context,
) -> None:
    """Release TPM + cost reservations made by enforce_rate_limits for the
    current candidate (stored in state['rate_limit_state'])."""
    rls = state.get("rate_limit_state")
    if not rls:
        return
    if redis is None:
        return

    svc = RateLimitService()

    tpm_descriptors = rls.get("tpm_descriptors", [])
    tpm_reserved = rls.get("tpm_reserved", 0)
    if tpm_descriptors and tpm_reserved > 0:
        try:
            await svc.settle_tpm(redis, tpm_descriptors, tpm_reserved, 0)
        except Exception:
            logger.warning("fallback_unwind_tpm_failed")

    cost_reserved = rls.get("cost_reserved")
    if cost_reserved is not None and cost_reserved != Decimal("0") and auth_context:
        try:
            await svc.settle_cost(
                redis,
                user_id=str(auth_context.user_id),
                actual_cost=Decimal("0"),
                reserved_cost=cost_reserved,
                team_id=str(auth_context.team_id) if auth_context.team_id else None,
            )
        except Exception:
            logger.warning("fallback_unwind_cost_failed")

    # Clear so finalize() does not double-settle
    state.pop("rate_limit_state", None)


async def run_fallback_loop(
    *,
    try_order: list[str],
    original_alias: str,
    is_stream: bool,
    req_data: dict,
    redis,
    auth_context,
    state: dict,
    request_id: str,
    budget_status,
    adapter,
    stream_kwargs: dict,
    nonstream_kwargs: dict,
    cb: CircuitBreakerService,
    router_service,
    session_factory,
    is_db_degraded: bool,
    # For haiku thinking-strip detection
    original_model_config: ModelConfigSchema,
    # These callables allow the helper to resolve candidate model configs
    # and rebuild the invoke body for each candidate.
    resolve_model_config,  # async callable(alias) -> ModelConfigSchema
    build_candidate_body,  # callable(req_data, model_config, is_stream) -> (bytes, dict, dict)
    # For Bedrock: _rewrite_model_id_for_region; for Mantle: identity
    rewrite_model_id,  # callable(provider_model_id) -> str
) -> FallbackResult:
    """Run the fallback loop over `try_order`.

    Each element of `try_order` is a model alias.  The original is always
    try_order[0].  We iterate:
      1. Resolve model_config + pmid
      2. Circuit-breaker gate
      3. check_key_scope (immediate 403 on failure)
      4. enforce_rate_limits (immediate 429 on failure)
      5. Invoke
      6. On 5xx / timeout: unwind reservation, record_failure, continue
      7. On success: record_success, build FallbackResult

    Returns a FallbackResult.  The caller decides how to respond.
    """
    last_error: tuple[int, bytes] | None = None
    any_invoked = False

    for idx, alias in enumerate(try_order):
        is_original = idx == 0

        # --- 1. Resolve model config for this candidate ---
        try:
            if is_original:
                # Already resolved by messages(); re-use to avoid extra DB hit
                candidate_config = original_model_config
            else:
                candidate_config = await resolve_model_config(alias)
        except LookupError as exc:
            logger.warning("fallback_candidate_not_found", alias=alias, error=str(exc))
            continue

        pmid = candidate_config.provider_model_id

        # --- 2. Circuit-breaker gate ---
        if await cb.is_open(redis, pmid):
            probe_won = await cb.try_acquire_halfopen_probe(redis, pmid)
            if not probe_won:
                logger.info("fallback_cb_open_skip", alias=alias, pmid=pmid)
                continue  # skip — circuit open, no probe slot
            logger.info("fallback_cb_halfopen_probe", alias=alias, pmid=pmid)

        # --- 3. Key-scope check ---
        if auth_context:
            try:
                router_service.check_key_scope(auth_context, candidate_config)
            except PermissionError:
                logger.info("fallback_candidate_scope_denied", alias=alias)
                # Treat scope denial on the original the same as before;
                # on a fallback candidate just skip (the original 403 was what mattered).
                if is_original:
                    return FallbackResult(
                        status=403,
                        payload=(
                            json.dumps(
                                {
                                    "error": {
                                        "type": "permission_error",
                                        "message": "Model not allowed for this key",
                                    }
                                }
                            ).encode(),
                            {},
                            TokenUsage(),
                        ),
                        model_config=candidate_config,
                    )
                continue

        # --- 4. Rate-limit enforcement ---
        # Clear the previous candidate's rate_limit_state so enforce writes fresh
        state.pop("rate_limit_state", None)
        if auth_context:
            rejected = await enforce_rate_limits(
                redis=redis,
                auth_context=auth_context,
                model_config=candidate_config,
                body=req_data,
                state=state,
                request_id=request_id,
                budget_status=budget_status,
            )
            if rejected is not None:
                if is_original:
                    # Return 429 immediately — no fallback for rate limits.
                    # rejected.body is already bytes (Starlette JSONResponse).
                    raw_body = getattr(rejected, "body", None)
                    if isinstance(raw_body, bytes):
                        body_bytes = raw_body
                    elif raw_body is not None:
                        body_bytes = json.dumps(raw_body).encode()
                    else:
                        body_bytes = b"{}"
                    return FallbackResult(
                        status=rejected.status_code,
                        payload=(
                            body_bytes,
                            dict(rejected.headers) if hasattr(rejected, "headers") else {},
                            TokenUsage(),
                        ),
                        model_config=candidate_config,
                    )
                # On a fallback candidate a RL rejection means this slot is
                # exhausted — propagate original error.
                logger.info("fallback_candidate_rl_rejected", alias=alias)
                continue

        # --- 5. Build invoke body for this candidate ---
        invoke_body, cand_stream_kwargs, cand_nonstream_kwargs = build_candidate_body(
            req_data, candidate_config, is_stream
        )

        # Haiku thinking-strip (mirrors downgrade.py:191)
        candidate_alias = candidate_config.alias or ""
        if candidate_alias.startswith("claude-haiku-4-5"):
            try:
                body_dict = json.loads(invoke_body)
                if "thinking" in body_dict:
                    body_dict.pop("thinking")
                    invoke_body = json.dumps(body_dict).encode()
                    logger.info("fallback_thinking_stripped", alias=candidate_alias)
            except Exception as _exc:
                logger.warning("fallback_haiku_strip_skipped", reason=str(_exc))

        call_model_id = rewrite_model_id(pmid)

        any_invoked = True

        # --- 6. Invoke ---
        caught_connection_error = False
        try:
            if is_stream:
                status, chunk_iter, headers, aws_request_id = await adapter.invoke_stream(
                    invoke_body, call_model_id, **cand_stream_kwargs
                )
            else:
                status, response_body, headers, usage = await adapter.invoke(
                    invoke_body, call_model_id, **cand_nonstream_kwargs
                )
        except (TimeoutError, ConnectionError, OSError) as exc:
            logger.warning(
                "fallback_invoke_connection_error",
                alias=alias,
                error=type(exc).__name__,
            )
            caught_connection_error = True
            status = 503  # treat as 503 for unwind/CB purposes
            response_body = json.dumps(
                {"error": {"type": "connection_error", "message": str(exc)}}
            ).encode()
            headers = {}
            usage = TokenUsage()

        # --- 7. Handle result ---
        if status in _FALLBACK_STATUSES or caught_connection_error:
            # CB: record failure only for {502,503} and connection errors, NOT 504
            if status in _CB_FAILURE_STATUSES or caught_connection_error:
                await cb.record_failure(redis, pmid)

            # Unwind this candidate's rate-limit reservation
            await _unwind_reservation(redis=redis, state=state, auth_context=auth_context)

            error_bytes = response_body if not is_stream else json.dumps(
                {"error": {"type": "provider_error", "message": f"Backend returned {status}"}}
            ).encode()
            last_error = (status, error_bytes)

            logger.info(
                "fallback_candidate_failed",
                alias=alias,
                status=status,
                is_original=is_original,
            )
            continue

        else:
            # Non-fallback status (2xx, 4xx, etc.)
            if 200 <= status < 300:
                await cb.record_success(redis, pmid)

            availability_fallback_from = original_alias if not is_original else None

            if availability_fallback_from:
                logger.info(
                    "fallback_succeeded",
                    original=original_alias,
                    used=alias,
                    status=status,
                )

            if is_stream:
                return FallbackResult(
                    status=status,
                    payload=(chunk_iter, headers, aws_request_id),
                    model_config=candidate_config,
                    availability_fallback_from=availability_fallback_from,
                )
            else:
                return FallbackResult(
                    status=status,
                    payload=(response_body, headers, usage),
                    model_config=candidate_config,
                    availability_fallback_from=availability_fallback_from,
                )

    # Loop exhausted
    if not any_invoked:
        # All candidates were circuit-open
        return FallbackResult(
            status=503,
            payload=(
                json.dumps(
                    {
                        "error": {
                            "type": "service_unavailable",
                            "message": "All fallback models are temporarily unavailable",
                        }
                    }
                ).encode(),
                {},
                TokenUsage(),
            ),
            model_config=original_model_config,
            all_open=True,
        )

    # All candidates failed with real backend errors — return last error
    last_status, last_body = last_error  # type: ignore[misc]
    return FallbackResult(
        status=last_status,
        payload=(last_body, {}, TokenUsage() if not is_stream else None),
        model_config=original_model_config,
    )
