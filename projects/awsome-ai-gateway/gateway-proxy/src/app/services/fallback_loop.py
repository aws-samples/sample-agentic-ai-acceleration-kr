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

from app.observability.provider_metrics import (
    build_provider_labels,
    record_provider_error,
    record_provider_request,
)
from app.schemas.domain import ModelConfigSchema, TokenUsage
from app.services.circuit_breaker import CircuitBreakerService
from app.services.rate_limit_enforcement import enforce_rate_limits
from app.services.rate_limit_service import RateLimitService
from app.services.router_service import ClientModelScopeError

logger = structlog.get_logger(__name__)

# HTTP status codes that trigger a fallback attempt
_FALLBACK_STATUSES = frozenset({502, 503, 504})
# Among fallback statuses, only these count as a CB failure
# (504 = ModelTimeoutException = per-prompt fault, do NOT penalise the circuit)
_CB_FAILURE_STATUSES = frozenset({502, 503})


@dataclass
class AdmissionRejection:
    """요청을 상류로 보내기 전에 거절한 결과(스코프 거부 또는 레이트리밋)."""

    status: int
    body: bytes
    headers: dict


def scope_denial_body(candidate_config: Any, *, client_scoped: bool) -> bytes:
    """스코프 거부의 클라이언트 응답 본문.

    ⚠️ 400 이지 403 이 아니다. Anthropic 클라이언트들은 403 을 자격증명 문제로 읽고
       재인증 루프를 돌린다 — 실제로는 모델 접근권이 없는 것이므로 재시도로 풀리지 않는다.

    ⚠️ 두 축의 안내가 달라야 한다. 계정 축(``check_key_scope``)은 "관리자에게 접근 요청",
       앱 축(``check_client_model_scope``)은 "이 앱에서는 못 쓴다" 다. 한 문구로 합치면
       Codex 에서만 막힌 사용자가 계정 권한을 요청하러 가고, 운영자는 이미 권한이 있다고
       답한다 — 아무도 원인을 못 찾는다.
    """
    alias = getattr(candidate_config, "alias", "?")
    if client_scoped:
        message = (
            f"Model '{alias}' is not available to this application. "
            "Your account may still use it from other applications — "
            "ask your administrator to allow this app for this model."
        )
    else:
        message = (
            f"Your account does not have access to model '{alias}'. "
            "Contact your administrator to request access."
        )
    return json.dumps({"error": {"type": "invalid_request_error", "message": message}}).encode()


async def enforce_candidate_admission(
    *,
    router_service: Any,
    auth_context: Any,
    candidate_config: Any,
    redis: Any,
    req_data: Any,
    state: dict,
    request_id: str,
    budget_status: Any = None,
) -> AdmissionRejection | None:
    """한 후보 모델에 대한 **입장 심사**: 스코프 2축 + 레이트리밋.

    ``None`` 이면 통과. 값이 있으면 그것이 클라이언트에게 돌려줄 거절이다.

    ⚠️ 이 함수가 따로 있는 이유. 이 세 검사는 오래도록 ``run_fallback_loop`` 안에만
       있었고, 그것이 ``/v1/messages`` 의 **유일한** 호출 지점이었다. 그래서 폴백 루프
       앞에서 리턴하는 경로(웹서치 루프)는 세 검사를 전부 건너뛰었다 — 사용자별 모델
       허용목록, 앱별 모델 허용목록, RPM/TPM/비용 한도가 모두 무시됐고, 웹서치를 켠
       프로파일에서만 그랬으므로 증상이 없었다. 호출자가 늘어날 때 이 검사를 복사하지
       않도록 한 곳에 둔다.

    ⚠️ ``auth_context`` 가 없으면(인증 미들웨어를 거치지 않는 내부 호출) 통과시킨다 —
       기존 동작 그대로다. 이 함수가 인증을 대체하지는 않는다.
    """
    if not auth_context:
        return None

    try:
        router_service.check_key_scope(auth_context, candidate_config)
        # 모델 × 앱 축(migration 0035). 위 게이트(사용자 × 모델)와 AND 로 걸린다.
        # allowed_clients: None=제한 없음 / []=어떤 앱도 불가 / 목록=그 앱만.
        router_service.check_client_model_scope(candidate_config, state.get("client"))
    except PermissionError as exc:
        client_scoped = isinstance(exc, ClientModelScopeError)
        logger.info(
            "candidate_scope_denied",
            alias=getattr(candidate_config, "alias", None),
            axis="client_model" if client_scoped else "key",
        )
        return AdmissionRejection(
            status=400,
            body=scope_denial_body(candidate_config, client_scoped=client_scoped),
            headers={},
        )

    # 이전 후보의 예약 상태를 지운다 — enforce 가 새로 쓰도록.
    state.pop("rate_limit_state", None)
    rejected = await enforce_rate_limits(
        redis=redis,
        auth_context=auth_context,
        model_config=candidate_config,
        body=req_data,
        state=state,
        request_id=request_id,
        budget_status=budget_status,
    )
    if rejected is None:
        return None

    # rejected 는 Starlette JSONResponse — body 는 이미 bytes 다.
    raw_body = getattr(rejected, "body", None)
    if isinstance(raw_body, bytes):
        body_bytes = raw_body
    elif raw_body is not None:
        body_bytes = json.dumps(raw_body).encode()
    else:
        body_bytes = b"{}"
    return AdmissionRejection(
        status=rejected.status_code,
        body=body_bytes,
        headers=dict(rejected.headers) if hasattr(rejected, "headers") else {},
    )


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
    # 프로바이더 호출 결과 지표(선택). None 이면 아무것도 기록하지 않는다.
    metrics=None,
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

        # --- 3+4. Admission: scope (2 axes) + rate limits ---
        # ⚠️ 이 세 검사는 :func:`enforce_candidate_admission` 한 곳에 있다. 여기 인라인으로
        #    두면 폴백 루프 밖에서 상류를 호출하는 경로(웹서치 루프)가 그대로 우회한다 —
        #    실제로 그런 상태였다. 그 함수의 docstring 에 경위가 있다.
        rejection = await enforce_candidate_admission(
            router_service=router_service,
            auth_context=auth_context,
            candidate_config=candidate_config,
            redis=redis,
            req_data=req_data,
            state=state,
            request_id=request_id,
            budget_status=budget_status,
        )
        if rejection is not None:
            if is_original:
                # 원본 후보의 거절은 그대로 클라이언트에게 돌려준다(스코프 400 /
                # 레이트리밋 429). 레이트리밋에는 폴백을 시도하지 않는다 — 다른 모델로
                # 우회하는 것이 곧 한도 우회다.
                return FallbackResult(
                    status=rejection.status,
                    payload=(rejection.body, rejection.headers, TokenUsage()),
                    model_config=candidate_config,
                )
            # 폴백 후보의 거절은 그 슬롯만 건너뛴다 — 원본의 오류가 클라이언트가 볼 것이다.
            logger.info("fallback_candidate_rejected", alias=alias, status=rejection.status)
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

        # ── 이 후보 1건 = 프로바이더 호출 1건 ──
        #
        # 폴백 루프에서는 후보마다 따로 세는 것이 맞다. 요청 단위로 한 번만 세면
        # "원본 모델이 죽어서 폴백이 성공했다" 가 **성공 1건**으로만 보이고, 원본의
        # 실패는 지표에서 사라진다 — 정확히 알아야 할 사실이 그것이다.
        _pm_labels = build_provider_labels(
            model_config=candidate_config,
            client=state.get("client"),
            is_stream=is_stream,
        )
        record_provider_request(metrics, _pm_labels)

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
            record_provider_error(
                metrics, _pm_labels, status=503, error_code=type(exc).__name__
            )
            status = 503  # treat as 503 for unwind/CB purposes
            response_body = json.dumps(
                {"error": {"type": "connection_error", "message": str(exc)}}
            ).encode()
            headers = {}
            usage = TokenUsage()

        # --- 7. Handle result ---
        if status in _FALLBACK_STATUSES or caught_connection_error:
            # ⚠️ 연결 오류는 위 except 에서 이미 셌다 — 여기서 또 세면 2배가 된다.
            if not caught_connection_error:
                record_provider_error(metrics, _pm_labels, status=status)
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
