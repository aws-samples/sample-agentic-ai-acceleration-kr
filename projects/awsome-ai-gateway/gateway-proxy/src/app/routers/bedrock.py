# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import asyncio
import time

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.observability.provider_metrics import (
    STATUS_STREAM_ERROR,
    build_provider_labels,
    record_provider_error,
    record_provider_request,
)
from app.schemas.domain import ProviderType
from app.services.fallback_loop import release_reservations
from app.services.router_service import RouterService

logger = structlog.get_logger(__name__)

router = APIRouter()
_router_service = RouterService()

# Cross-region inference profile prefix mapping for Bedrock.
# Bedrock groups regions into inference-profile families: us, eu, apac.
# `global.` is a region-agnostic family (Opus 4.5/4.6, Sonnet 4.5/4.6) and is passed through unchanged.
_REGION_PREFIX_MAP = {
    "us-east-1": "us",
    "us-west-2": "us",
    "eu-west-1": "eu",
    "eu-central-1": "eu",
    "ap-northeast-1": "apac",  # Tokyo
    "ap-northeast-2": "apac",  # Seoul
    "ap-southeast-1": "apac",  # Singapore
    "ap-southeast-2": "apac",  # Sydney
}

# Known region prefixes that Bedrock actually uses on inference profile IDs.
_KNOWN_REGION_PREFIXES = {"us", "eu", "apac", "global"}


def _rewrite_model_id_for_region(model_id: str, region: str | None = None) -> str:
    """Rewrite cross-region inference profile prefix to match target region.

    e.g., us.anthropic.claude-sonnet-4-20250514-v1:0
       → apac.anthropic.claude-sonnet-4-20250514-v1:0 (if region=ap-northeast-2)

    `global.` prefix models are passed through unchanged — they resolve from any region.
    `region` explicit override(예: cross-account claude-code→374 의 profile.region) 우선;
    없으면 pod 의 AWS_REGION env(in-account 기본).
    """
    import os

    parts = model_id.split(".", 1)
    if len(parts) < 2 or parts[0] not in _KNOWN_REGION_PREFIXES:
        return model_id  # Not a cross-region inference profile

    prefix = parts[0]
    rest = parts[1]  # e.g., "anthropic.claude-sonnet-4-5-20250929-v1:0"

    region = region or os.environ.get("AWS_REGION", "ap-northeast-2")
    target_prefix = _REGION_PREFIX_MAP.get(region)

    if target_prefix and prefix != target_prefix and prefix != "global":
        new_model_id = f"{target_prefix}.{rest}"
        logger.info("model_id_rewritten", original=model_id, rewritten=new_model_id, region=region)
        return new_model_id

    return model_id


def _strip_region_prefix(model_id: str) -> str:
    """Drop cross-region inference profile prefix (global./us./eu./apac.).

    Bedrock CountTokens API rejects cross-region profile IDs and requires the
    base foundation-model ID (e.g. anthropic.claude-sonnet-4-6). Stripping the
    prefix yields the form accepted by CountTokens.
    """
    parts = model_id.split(".", 1)
    if len(parts) == 2 and parts[0] in _KNOWN_REGION_PREFIXES:
        return parts[1]
    return model_id


async def _get_request_body(request: Request) -> bytes:
    return await request.body()


@router.post("/model/{model_id:path}/invoke", response_model=None)
async def bedrock_invoke(model_id: str, request: Request) -> StreamingResponse | JSONResponse:
    return await _handle_bedrock(request, model_id, "invoke", stream=False)


@router.post("/model/{model_id:path}/invoke-with-response-stream")
async def bedrock_invoke_stream(model_id: str, request: Request) -> StreamingResponse:
    return await _handle_bedrock(request, model_id, "invoke-with-response-stream", stream=True)


@router.post("/model/{model_id:path}/converse")
async def bedrock_converse(model_id: str, request: Request) -> JSONResponse:
    return await _handle_bedrock(request, model_id, "converse", stream=False)


@router.post("/model/{model_id:path}/converse-stream")
async def bedrock_converse_stream(model_id: str, request: Request) -> StreamingResponse:
    return await _handle_bedrock(request, model_id, "converse-stream", stream=True)


async def _handle_bedrock(request: Request, model_id: str, path_suffix: str, stream: bool):
    state = request.scope.get("state", {})
    auth_context = state.get("auth_context")
    redis = state.get("_redis")
    session_factory = state.get("_session_factory")
    registry = request.app.state.provider_registry
    cost_recorder = request.app.state.cost_recorder
    request_id = state.get("request_id", "")
    start_time = state.get("request_start_time", time.monotonic())

    # ModelConfig 조회 (비용 계산용, short-lived session)
    from app.schemas.domain import DegradationLevel

    dm = state.get("_degradation_manager")
    is_db_degraded = dm and dm.level in (
        DegradationLevel.DB_DEGRADED,
        DegradationLevel.BOTH_DEGRADED,
    )

    if not is_db_degraded and session_factory is not None:
        async with session_factory() as db:
            model_config = await _router_service.resolve_bedrock_model(redis, db, model_id)
    else:
        model_config = await _router_service.resolve_bedrock_model(redis, None, model_id)
    state["model_config"] = model_config

    # Use resolved model_id (may differ from URL model_id due to aliasing)
    # Also rewrite cross-region prefix if needed (e.g., us.→apac.)
    bedrock_model_id = _rewrite_model_id_for_region(model_config.provider_model_id)

    # Key Scope 검사
    if auth_context:
        try:
            _router_service.check_key_scope(auth_context, model_config)
            # 모델 × 앱 축(migration 0035). 위 게이트(사용자 × 모델)와 AND 로 걸린다.
            # allowed_clients: None=제한 없음 / []=어떤 앱도 불가 / 목록=그 앱만.
            _router_service.check_client_model_scope(model_config, state.get("client"))
        except PermissionError:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "type": "invalid_request_error",
                        "message": f"Your account does not have access to model '{model_config.alias}'. Contact your administrator to request access.",
                    }
                },
            )

    body = await _get_request_body(request)

    # Pre-reserve RPM + TPM (3-scope: USER/TEAM/GLOBAL)
    if auth_context:
        import json as _json

        from app.services.rate_limit_enforcement import enforce_rate_limits

        try:
            body_dict = _json.loads(body) if body else {}
            if not isinstance(body_dict, dict):
                body_dict = {}
        except Exception:
            body_dict = {}

        rejected = await enforce_rate_limits(
            redis=redis,
            auth_context=auth_context,
            model_config=model_config,
            body=body_dict,
            state=state,
            request_id=request_id,
            budget_status=state.get("budget_status"),
            metrics=getattr(request.app.state, "metrics", None),
        )
        if rejected is not None:
            return rejected

    adapter = registry.get(ProviderType.BEDROCK)
    rate_limit_state = state.get("rate_limit_state")

    # ── 프로바이더 호출 결과 지표 ──
    #
    # 여기서 request(분모)와 error(분자)를 **같은 라벨로** 올린다. 라벨 구성은
    # observability/provider_metrics.py 한 곳에서만 만든다 — 호출부마다 다르게 붙이면
    # 모델별 에러율 시계열이 섞여 어느 모델이 죽는지 알 수 없게 된다.
    #
    # ⚠️ 이 경로는 `/model/*` 패스스루다. 미들웨어의 `gateway_error_total` 은 HTTP
    #    상태만 보므로 "Bedrock 이 ThrottlingException 을 냈다" 와 "우리 쿼터가 찼다" 를
    #    구분하지 못한다. 그래서 error_code 를 별도 라벨로 남긴다.
    _pm = getattr(request.app.state, "metrics", None)
    _pm_labels = build_provider_labels(
        model_config=model_config,
        client=state.get("client"),
        is_stream=bool(stream),
    )
    record_provider_request(_pm, _pm_labels)

    if stream:
        # invoke_stream 은 4-튜플 (status, chunk_iter, headers, request_id) 반환.
        # ⚠️ 버그수정(2026-07-09): 기존 `await invoke_stream(...)[:3]` 은 연산자 우선순위상
        # `await (coroutine[:3])` 로 파싱돼 coroutine 슬라이싱 TypeError → 이 raw
        # /model/*/invoke-with-response-stream 경로가 깨져 있었음(테스트 미커버, 주경로는
        # /v1/messages 라 안 드러남). await 를 먼저 풀고 앞 3개만 취한다.
        try:
            status, chunk_iter, headers, _req_id = await adapter.invoke_stream(
                body, bedrock_model_id, path_suffix=path_suffix
            )
        except Exception as e:
            # 기록만 하고 **그대로 재던진다** — 지표를 위해 에러를 삼키면 안 된다.
            record_provider_error(
                _pm, _pm_labels, status=STATUS_STREAM_ERROR, error_code=type(e).__name__
            )
            # 예약을 되돌린 뒤 재던진다 — 스트림이 시작되지 못했으므로 정산할 usage 가
            # 영원히 오지 않는다.
            await release_reservations(redis=redis, state=state, auth_context=auth_context)
            raise

        async def stream_with_cost():
            """Bedrock `/model/*` pass-through: 원본 바이트 유지. usage는 OpenAI 형식
            (있을 경우) 탐지. KI-08 tokenizer 역산은 `/v1/messages` 경로에서만 적용
            (이 경로는 Bedrock EventStream binary라 누적 텍스트 파싱 비용이 높음).
            """
            usage = None
            from app.schemas.domain import TokenUsage
            from app.services.streaming import _try_extract_usage

            try:
                async for chunk in chunk_iter:
                    u = _try_extract_usage(chunk)
                    if u:
                        usage = u
                    yield chunk
            finally:
                if auth_context and (usage or rate_limit_state):
                    effective_usage = usage or TokenUsage(
                        input_tokens=0,
                        output_tokens=0,
                        total_tokens=0,
                        cache_creation_input_tokens=0,
                        cache_read_input_tokens=0,
                    )
                    duration_ms = int((time.monotonic() - start_time) * 1000)
                    await cost_recorder.finalize(
                        redis,
                        auth_context,
                        model_config,
                        effective_usage,
                        request_id,
                        True,
                        duration_ms,
                        ttft_ms=duration_ms,
                        rate_limit_state=rate_limit_state,
                        downgraded_from=state.get("downgraded_from"),
                    )

        return StreamingResponse(
            stream_with_cost(),
            status_code=status,
            media_type=headers.get("Content-Type", "application/octet-stream"),
        )
    else:
        try:
            status, response_body, headers, usage = await adapter.invoke(
                body, bedrock_model_id, path_suffix=path_suffix
            )
        except Exception as e:
            record_provider_error(_pm, _pm_labels, status="exception", error_code=type(e).__name__)
            await release_reservations(redis=redis, state=state, auth_context=auth_context)
            raise
        if status >= 400:
            # 예외가 아니라 상태코드로 실패가 오는 경우(어댑터가 응답을 그대로 넘긴다).
            record_provider_error(_pm, _pm_labels, status=status)
        if auth_context and (usage.input_tokens + usage.output_tokens) > 0:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            await cost_recorder.finalize(
                redis,
                auth_context,
                model_config,
                usage,
                request_id,
                False,
                duration_ms,
                ttft_ms=duration_ms,
                rate_limit_state=rate_limit_state,
                downgraded_from=state.get("downgraded_from"),
                # Join key to the Bedrock model-invocation log record (adapter.invoke
                # returns it in the headers dict; not forwarded to the client).
                bedrock_request_id=(headers or {}).get("x-amzn-requestid"),
            )
        else:
            # ⚠️ finalize 를 타지 않는 경로다(상류 4xx/5xx, 또는 usage 가 비어 온 응답).
            #    이 라우트에는 폴백 루프가 없어 그쪽 unwind 도 돌지 않으므로, 여기서
            #    되돌리지 않으면 예약이 창(window) 끝까지 사용자 한도를 물고 있다.
            await release_reservations(redis=redis, state=state, auth_context=auth_context)
        return JSONResponse(
            status_code=status,
            content=__import__("json").loads(response_body) if response_body else {},
        )
