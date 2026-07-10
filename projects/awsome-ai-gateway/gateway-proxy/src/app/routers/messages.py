# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Anthropic Messages API route (/v1/messages) — Bedrock native proxy.

Claude Code sends POST /v1/messages with Anthropic Messages API format.
Bedrock invoke_model for Claude models accepts this body format directly.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.config import get_settings
from app.providers.bedrock_adapter import BedrockAdapter
from app.routers.bedrock import _rewrite_model_id_for_region, _strip_region_prefix
from app.schemas.domain import (
    DegradationLevel,
    ModelConfigSchema,
    ProviderType,
    TokenUsage,
)
from app.schemas.routing import RoutingProfileSchema
from app.services.fallback_loop import FallbackResult, run_fallback_loop
from app.services.fallback_resolver import make_same_provider
from app.services.router_service import RouterService
from app.services.streaming import bedrock_anthropic_sse_stream

logger = structlog.get_logger(__name__)

router = APIRouter()
_router_service = RouterService()


def _has_1h_cache_control(req_data: dict) -> bool:
    """Detect if any cache_control block in the request uses 1-hour TTL (ttl=3600).

    Scans system, messages, and tools for cache_control.ttl == "3600" or 3600.
    """
    def _check_blocks(blocks):
        if not isinstance(blocks, list):
            return False
        for block in blocks:
            if not isinstance(block, dict):
                continue
            cc = block.get("cache_control")
            if isinstance(cc, dict) and str(cc.get("ttl", "")) == "3600":
                return True
        return False

    # system prompt blocks
    system = req_data.get("system")
    if isinstance(system, list) and _check_blocks(system):
        return True

    # message content blocks
    for msg in req_data.get("messages", []):
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list) and _check_blocks(content):
            return True

    # tool definitions
    for tool in req_data.get("tools", []):
        if isinstance(tool, dict):
            cc = tool.get("cache_control")
            if isinstance(cc, dict) and str(cc.get("ttl", "")) == "3600":
                return True

    return False

# Bedrock invoke_model only accepts specific fields — strip everything else.
# Claude Code sends extra fields (model, stream, context_management, etc.) that Bedrock rejects.
# Bedrock does NOT accept anthropic_beta — caching is handled automatically via cache_control in content.
_BEDROCK_ALLOWED_FIELDS = {
    "anthropic_version",
    "messages",
    "max_tokens",
    "system",
    "stop_sequences",
    "temperature",
    "top_p",
    "top_k",
    "metadata",
    "tools",
    "tool_choice",
    "thinking",
}


@dataclass
class BackendDecision:
    provider: ProviderType
    profile: RoutingProfileSchema | None = None
    model_config: ModelConfigSchema | None = None  # pre-resolved for Mantle
    model_id: str | None = None          # resolved provider_model_id for Mantle; None = default Bedrock path
    endpoint: str | None = None


async def _select_backend(*, loader, router_service, redis, db, client, requested_alias):
    """Decide which backend serves this request.

    Rule A (Cowork client-override): a 'mantle' profile WITH a default_model forces
    that model (cowork → cowork-opus), ignoring the requested alias.
    Rule B (Claude Code alias opt-in): if the requested alias's provider is
    BEDROCK_MANTLE, route to Mantle using the requested alias. The profile (if any)
    supplies region/account_role_arn (NULL = in-account 374).
    Everyone else → the existing Bedrock path.
    """
    profile = await loader.load(redis, db, client)
    # loader.load() already filters disabled profiles → a non-None profile is always enabled here.

    # Rule A — cowork override (unchanged): mantle profile + default_model.
    if profile is not None and profile.backend == "mantle" and profile.default_model:
        cfg = await router_service.resolve_mantle_model(redis, db, profile.default_model)
        return BackendDecision(
            provider=ProviderType.BEDROCK_MANTLE,
            profile=profile,
            model_config=cfg,
            model_id=cfg.provider_model_id,
            endpoint=cfg.endpoint,
        )

    # Rule B — alias-triggered Mantle (Claude Code in-account option).
    # Rule B needs a profile for region/account (region is required, not synthesizable); no profile → fall through to Bedrock.
    if profile is not None and await router_service.alias_provider(redis, db, requested_alias) == ProviderType.BEDROCK_MANTLE:
        cfg = await router_service.resolve_mantle_model(redis, db, requested_alias)
        return BackendDecision(
            provider=ProviderType.BEDROCK_MANTLE,
            profile=profile,
            model_config=cfg,
            model_id=cfg.provider_model_id,
            endpoint=cfg.endpoint,
        )

    # Bedrock path: routing ignores the profile, but carry it through so the caller can
    # read per-client flags (e.g. web_search_enabled) uniformly across all backends.
    return BackendDecision(provider=ProviderType.BEDROCK, profile=profile)


@router.post("/v1/messages", response_model=None)
async def messages(request: Request) -> StreamingResponse | JSONResponse:
    state = request.scope.get("state", {})
    auth_context = state.get("auth_context")
    client = state.get("client")
    redis = state.get("_redis")
    session_factory = state.get("_session_factory")
    registry = request.app.state.provider_registry
    cost_recorder = request.app.state.cost_recorder
    request_id = state.get("request_id", "")
    start_time = state.get("request_start_time", time.monotonic())

    body = await request.body()

    try:
        req_data = json.loads(body)
        model_alias = req_data.get("model", "")
        is_stream = req_data.get("stream", False)

        bedrock_body = {k: v for k, v in req_data.items() if k in _BEDROCK_ALLOWED_FIELDS}
        bedrock_body["anthropic_version"] = "bedrock-2023-05-31"
        if auth_context and auth_context.sso_subject:
            bedrock_body["metadata"] = {"user_id": auth_context.sso_subject}
        cache_ttl_1h = _has_1h_cache_control(req_data)
        body = json.dumps(bedrock_body).encode()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"error": {"type": "invalid_request_error", "message": "Invalid JSON body"}},
        )

    if not model_alias:
        return JSONResponse(
            status_code=400,
            content={
                "error": {"type": "invalid_request_error", "message": "model field is required"}
            },
        )

    # Resolve backend (client routing profile) → model config (short-lived session)
    dm = state.get("_degradation_manager")
    is_db_degraded = dm and dm.level in (
        DegradationLevel.DB_DEGRADED,
        DegradationLevel.BOTH_DEGRADED,
    )

    routing_loader = getattr(request.app.state, "routing_profile_loader", None)
    decision = BackendDecision(provider=ProviderType.BEDROCK)
    model_config = None
    try:
        if not is_db_degraded and session_factory is not None:
            async with session_factory() as db:
                if routing_loader is not None:
                    decision = await _select_backend(
                        loader=routing_loader, router_service=_router_service,
                        redis=redis, db=db, client=client, requested_alias=model_alias,
                    )
                if decision.provider != ProviderType.BEDROCK_MANTLE:
                    model_config = await _router_service.resolve_bedrock_model(
                        redis, db, model_alias
                    )
        else:
            if routing_loader is not None:
                decision = await _select_backend(
                    loader=routing_loader, router_service=_router_service,
                    redis=redis, db=None, client=client, requested_alias=model_alias,
                )
            if decision.provider != ProviderType.BEDROCK_MANTLE:
                model_config = await _router_service.resolve_bedrock_model(
                    redis, None, model_alias
                )
    except LookupError as e:
        return JSONResponse(
            status_code=404,
            content={"error": {"type": "not_found_error", "message": str(e)}},
        )

    # cross-account(claude-code→374) 일 때만 Bedrock 분기에서 profile.region 으로 설정.
    # 분기 전 명시 초기화 → 아래 _rewrite 클로저가 locals() 리플렉션 없이 직접 참조(리팩터 안전).
    _region_for_rewrite = None
    if decision.provider == ProviderType.BEDROCK_MANTLE:
        model_config = decision.model_config
        adapter = registry.get(ProviderType.BEDROCK_MANTLE)
        call_model_id = decision.model_id
        stream_kwargs: dict = {"profile": decision.profile, "endpoint": decision.endpoint}
        nonstream_kwargs: dict = {"profile": decision.profile, "endpoint": decision.endpoint}
        # Rebuild body as standard Anthropic Messages (NOT the bedrock-wrapped body):
        # anthropic-version is a HEADER for Mantle (adapter sets it); model goes in body.
        mantle_body = {k: v for k, v in req_data.items() if k in _BEDROCK_ALLOWED_FIELDS}
        mantle_body.pop("anthropic_version", None)
        mantle_body["model"] = call_model_id
        # Carry user attribution metadata, same as the Bedrock path (line ~138).
        if auth_context and auth_context.sso_subject:
            mantle_body["metadata"] = {"user_id": auth_context.sso_subject}
        if is_stream:
            mantle_body["stream"] = True
        body = json.dumps(mantle_body).encode()
    else:
        # Bedrock native. cross-account(claude-code→374): backend=invoke AND account_role_arn
        # 이면 대상 계정 role 을 assume 한 bedrock-runtime 클라이언트로 호출. account_role_arn
        # NULL(codex/기본 in-account)이면 기존 in-account adapter 그대로(zero-regression).
        _prof = decision.profile
        _xacct = bool(_prof and _prof.backend == "invoke" and _prof.account_role_arn)
        if _xacct:
            _provider = getattr(request.app.state, "bedrock_account_client_provider", None)
            _inacct_adapter = registry.get(ProviderType.BEDROCK)
            if _provider is not None:
                _role, _reg, _ext = _prof.account_role_arn, _prof.region, _prof.external_id
                adapter = BedrockAdapter(
                    bedrock_client=None,
                    client_resolver=lambda: _provider.get_client(_role, _reg, _ext),
                    fallback_client=getattr(_inacct_adapter, "_client", None),  # assume 실패 시 859 폴백
                )
                _region_for_rewrite = _prof.region
            else:
                adapter = _inacct_adapter
                _region_for_rewrite = None
        else:
            adapter = registry.get(ProviderType.BEDROCK)
            _region_for_rewrite = None
        call_model_id = _rewrite_model_id_for_region(
            model_config.provider_model_id, region=_region_for_rewrite
        )
        stream_kwargs = {"path_suffix": "invoke-with-response-stream"}
        nonstream_kwargs = {"path_suffix": "invoke"}

    # Invariant: every reachable path above either set model_config or returned 404.
    # Guard it explicitly so a future change can't silently pass None to check_key_scope
    # (which would bypass scope enforcement).
    assert model_config is not None
    state["model_config"] = model_config

    # --- Fallback chain resolution ---
    # For BEDROCK: resolve the chain and attempt fallback on 5xx.
    # For BEDROCK_MANTLE: resolver returns [original] only (no same-provider alternatives).
    cb = getattr(request.app.state, "circuit_breaker", None)
    fallback_resolver = getattr(request.app.state, "fallback_resolver", None)

    original_provider = model_config.provider

    # Use the startup alias→provider map (loaded from model.model_aliases at boot).
    # All known aliases are present so same-provider fallback candidates ARE admitted,
    # while cross-provider aliases (e.g. Bedrock aliases when original is BEDROCK_MANTLE)
    # are excluded because their map entry carries a different provider value.
    # Deny-by-default: aliases absent from the map return None → excluded.
    # We also ensure the original alias is always present (guard for the edge case where
    # it was somehow absent from the DB snapshot; the original is always same-provider).
    _startup_alias_provider_map: dict = getattr(request.app.state, "alias_provider_map", {})
    _alias_provider_map: dict = {**_startup_alias_provider_map, model_alias: original_provider}

    _same_provider_as_original = make_same_provider(_alias_provider_map, original_provider)

    allowed_set: set[str] | None = (
        set(auth_context.allowed_models) if auth_context and auth_context.allowed_models else None
    )

    if fallback_resolver is not None:
        try_order = fallback_resolver.resolve(
            original=model_alias,
            allowed=allowed_set,
            same_provider=_same_provider_as_original,
        )
    else:
        try_order = [model_alias]

    # --- Async model resolver for fallback candidates ---
    async def _resolve_candidate(alias: str) -> ModelConfigSchema:
        if decision.provider == ProviderType.BEDROCK_MANTLE:
            if not is_db_degraded and session_factory is not None:
                async with session_factory() as db:
                    return await _router_service.resolve_mantle_model(redis, db, alias)
            return await _router_service.resolve_mantle_model(redis, None, alias)
        else:
            if not is_db_degraded and session_factory is not None:
                async with session_factory() as db:
                    return await _router_service.resolve_bedrock_model(redis, db, alias)
            return await _router_service.resolve_bedrock_model(redis, None, alias)

    is_mantle = decision.provider == ProviderType.BEDROCK_MANTLE

    def _build_candidate_body(
        req_d: dict, cand_config: ModelConfigSchema, streaming: bool
    ) -> tuple[bytes, dict, dict]:
        """Build invoke body + kwargs for a given candidate model_config."""
        if is_mantle:
            mantle_b = {k: v for k, v in req_d.items() if k in _BEDROCK_ALLOWED_FIELDS}
            mantle_b.pop("anthropic_version", None)
            mantle_b["model"] = cand_config.provider_model_id
            if auth_context and auth_context.sso_subject:
                mantle_b["metadata"] = {"user_id": auth_context.sso_subject}
            if streaming:
                mantle_b["stream"] = True
            return (
                json.dumps(mantle_b).encode(),
                {"profile": decision.profile, "endpoint": decision.endpoint},
                {"profile": decision.profile, "endpoint": decision.endpoint},
            )
        else:
            bedrock_b = {k: v for k, v in req_d.items() if k in _BEDROCK_ALLOWED_FIELDS}
            bedrock_b["anthropic_version"] = "bedrock-2023-05-31"
            if auth_context and auth_context.sso_subject:
                bedrock_b["metadata"] = {"user_id": auth_context.sso_subject}
            return (
                json.dumps(bedrock_b).encode(),
                {"path_suffix": "invoke-with-response-stream"},
                {"path_suffix": "invoke"},
            )

    # cross-account(374) 면 profile.region, 아니면 None (분기 전 초기화됨).
    def _rewrite(pmid: str) -> str:
        if is_mantle:
            return pmid
        return _rewrite_model_id_for_region(pmid, region=_region_for_rewrite)

    # --- Server-side web search (Architecture C) — opt-in per routing profile ---
    # When the client's profile enables web search AND the AgentCore MCP client is
    # configured, run the tool-use loop instead of the plain fallback dispatch. The
    # loop injects a web_search tool, intercepts tool_use, calls AgentCore, and stitches
    # the answer. Skipped entirely otherwise → the existing path below is byte-identical.
    mcp_client = getattr(request.app.state, "agentcore_mcp_client", None)
    _profile = decision.profile
    if (
        mcp_client is not None
        and _profile is not None
        and getattr(_profile, "web_search_enabled", False)
    ):
        from app.services.web_search_loop import run_web_search_loop

        _settings_ws = get_settings()

        async def _ws_invoke(turn_body: dict) -> tuple[int, bytes, dict, TokenUsage]:
            body_b, _sk, nsk = _build_candidate_body(turn_body, model_config, False)
            return await adapter.invoke(body_b, _rewrite(model_config.provider_model_id), **nsk)

        async def _ws_invoke_stream(turn_body: dict):
            body_b, sk, _nsk = _build_candidate_body(turn_body, model_config, True)
            return await adapter.invoke_stream(
                body_b, _rewrite(model_config.provider_model_id), **sk
            )

        async def _ws_record(usage: TokenUsage) -> None:
            if not auth_context:
                return
            usage.cache_ttl_1h = cache_ttl_1h
            duration_ms = int((time.monotonic() - start_time) * 1000)
            await cost_recorder.finalize(
                redis, auth_context, model_config, usage, request_id,
                is_stream, duration_ms,
                rate_limit_state=state.get("rate_limit_state"),
                downgraded_from=state.get("downgraded_from"),
                client=client,
            )

        return await run_web_search_loop(
            dialect="anthropic",
            invoke=_ws_invoke,
            invoke_stream=_ws_invoke_stream,
            initial_req_data=req_data if isinstance(req_data, dict) else {},
            is_stream=is_stream,
            mcp_client=mcp_client,
            request=request,
            on_usage=_ws_record,
            max_iterations=_settings_ws.web_search_max_iterations,
            total_deadline_sec=_settings_ws.web_search_total_deadline_sec,
            default_max_results=_settings_ws.web_search_max_results_default,
        )

    # Use a no-op CB when the service is not wired (e.g. tests that don't configure it)
    if cb is None:
        from app.services.circuit_breaker import CircuitBreakerService

        cb = CircuitBreakerService()

    result: FallbackResult = await run_fallback_loop(
        try_order=try_order,
        original_alias=model_alias,
        is_stream=is_stream,
        req_data=req_data if isinstance(req_data, dict) else {},
        redis=redis,
        auth_context=auth_context,
        state=state,
        request_id=request_id,
        budget_status=state.get("budget_status"),
        adapter=adapter,
        stream_kwargs=stream_kwargs,
        nonstream_kwargs=nonstream_kwargs,
        cb=cb,
        router_service=_router_service,
        session_factory=session_factory,
        is_db_degraded=is_db_degraded,
        original_model_config=model_config,
        resolve_model_config=_resolve_candidate,
        build_candidate_body=_build_candidate_body,
        rewrite_model_id=_rewrite,
    )

    # --- Handle the result ---
    status = result.status
    effective_model_config = result.model_config
    availability_fallback_from = result.availability_fallback_from
    tokenizer = getattr(request.app.state, "tokenizer", None)

    # Synthetic all-open 503
    if result.all_open:
        _settings = get_settings()
        return JSONResponse(
            status_code=503,
            content=json.loads(result.payload[0]),
            headers={"Retry-After": str(_settings.cb_open_sec)},
        )

    if is_stream:
        chunk_iter, headers, aws_request_id = result.payload

        # When every fallback candidate failed (fallback_loop exhausted all
        # streaming candidates), the payload[0] is raw JSON error *bytes*, not
        # an async chunk iterator. Streaming that through the SSE stream would
        # raise (bytes has no __anext__). Return it as a JSON error response,
        # mirroring the all_open 503 branch above.
        if isinstance(chunk_iter, (bytes, bytearray)):
            try:
                err_content = json.loads(chunk_iter)
            except (json.JSONDecodeError, ValueError):
                err_content = {
                    "error": {
                        "type": "service_unavailable",
                        "message": "All fallback models failed",
                    }
                }
            return JSONResponse(status_code=status, content=err_content)

        # If the fallback loop returned a non-200 status for streaming, the
        # chunk_iter is an error generator from the adapter — just stream it.
        call_model_id_final = _rewrite(effective_model_config.provider_model_id)
        rate_limit_state = state.get("rate_limit_state")

        async def _estimate(text: str) -> int | None:
            if not tokenizer or is_mantle:
                return None
            return await tokenizer.estimate_output_tokens(
                text,
                provider=ProviderType.BEDROCK,
                model_id=call_model_id_final,
            )

        async def _record(usage: TokenUsage, first_token_time: float | None) -> None:
            if not auth_context:
                return
            usage.cache_ttl_1h = cache_ttl_1h
            duration_ms = int((time.monotonic() - start_time) * 1000)
            if first_token_time is not None:
                ttft_ms = int((first_token_time - start_time) * 1000)
            else:
                ttft_ms = duration_ms  # fallback: 첫 콘텐츠 델타 미검출
            await cost_recorder.finalize(
                redis,
                auth_context,
                effective_model_config,
                usage,
                request_id,
                True,
                duration_ms,
                ttft_ms=ttft_ms,
                rate_limit_state=rate_limit_state,
                downgraded_from=state.get("downgraded_from"),
                availability_fallback_from=availability_fallback_from,
                bedrock_request_id=aws_request_id,
                client=client,
            )

        response_headers: dict = {}
        if availability_fallback_from:
            response_headers["x-llm-gateway-fallback-from"] = availability_fallback_from

        return StreamingResponse(
            bedrock_anthropic_sse_stream(
                request, chunk_iter, on_usage=_record, tokenizer_hook=_estimate
            ),
            status_code=status,
            media_type="text/event-stream",
            headers=response_headers,
        )
    else:
        response_body, headers, usage = result.payload
        rate_limit_state = state.get("rate_limit_state")

        if auth_context and isinstance(usage, TokenUsage) and (usage.input_tokens + usage.output_tokens) > 0:
            usage.cache_ttl_1h = cache_ttl_1h
            duration_ms = int((time.monotonic() - start_time) * 1000)
            await cost_recorder.finalize(
                redis,
                auth_context,
                effective_model_config,
                usage,
                request_id,
                False,
                duration_ms,
                ttft_ms=duration_ms,
                rate_limit_state=rate_limit_state,
                downgraded_from=state.get("downgraded_from"),
                availability_fallback_from=availability_fallback_from,
                client=client,
            )

        response_headers_out: dict = {}
        if availability_fallback_from:
            response_headers_out["x-llm-gateway-fallback-from"] = availability_fallback_from

        # For the synthetic 429 / 403 paths returned via FallbackResult.payload,
        # response_body is already JSON bytes.
        try:
            content = json.loads(response_body)
        except Exception:
            content = {"error": {"type": "api_error", "message": "Invalid response from provider"}}
        return JSONResponse(
            status_code=status,
            content=content,
            headers=response_headers_out if response_headers_out else None,
        )


@router.post("/v1/messages/count_tokens", response_model=None)
async def count_tokens(request: Request) -> JSONResponse:
    """Anthropic count_tokens — proxies to Bedrock CountTokens (no inference, no cost)."""
    # count_tokens 는 Bedrock native 경로 유지(Mantle/Cowork 는 Phase 3에서 VK scope 후 라우팅).
    # claude-code→374 cross-account: CountTokens 도 invoke 와 동일 계정(374)에서 실행되도록
    # 라우팅 프로파일을 읽어 _xacct 면 cross-account adapter 사용(assume 실패 시 859 투명 폴백).
    # → invoke=374 / count_tokens=859 로 갈리는 무음 계정 스플릿 방지(어드버서리 리뷰 #4).
    state = request.scope.get("state", {})
    auth_context = state.get("auth_context")
    client = state.get("client")
    redis = state.get("_redis")
    session_factory = state.get("_session_factory")
    registry = request.app.state.provider_registry
    routing_loader = getattr(request.app.state, "routing_profile_loader", None)

    body = await request.body()

    try:
        req_data = json.loads(body)
        model_alias = req_data.get("model", "")
        bedrock_body = {k: v for k, v in req_data.items() if k in _BEDROCK_ALLOWED_FIELDS}
        bedrock_body["anthropic_version"] = "bedrock-2023-05-31"
        # Bedrock CountTokens requires max_tokens in the wrapped Anthropic body
        # even though it doesn't generate output; inject a placeholder when absent.
        bedrock_body.setdefault("max_tokens", 1)
        body = json.dumps(bedrock_body).encode()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"error": {"type": "invalid_request_error", "message": "Invalid JSON body"}},
        )

    if not model_alias:
        return JSONResponse(
            status_code=400,
            content={
                "error": {"type": "invalid_request_error", "message": "model field is required"}
            },
        )

    dm = state.get("_degradation_manager")
    is_db_degraded = dm and dm.level in (
        DegradationLevel.DB_DEGRADED,
        DegradationLevel.BOTH_DEGRADED,
    )

    _ct_profile = None
    try:
        if not is_db_degraded and session_factory is not None:
            async with session_factory() as db:
                model_config = await _router_service.resolve_bedrock_model(redis, db, model_alias)
                if routing_loader is not None:
                    _ct_profile = await routing_loader.load(redis, db, client)
        else:
            model_config = await _router_service.resolve_bedrock_model(redis, None, model_alias)
            if routing_loader is not None:
                _ct_profile = await routing_loader.load(redis, None, client)
    except LookupError as e:
        return JSONResponse(
            status_code=404,
            content={"error": {"type": "not_found_error", "message": str(e)}},
        )

    if auth_context:
        try:
            _router_service.check_key_scope(auth_context, model_config)
        except PermissionError:
            return JSONResponse(
                status_code=403,
                content={
                    "error": {
                        "type": "permission_error",
                        "message": "Model not allowed for this key",
                    }
                },
            )

    # CountTokens rejects cross-region inference profile IDs (global./us./apac./eu.)
    # — use the base foundation-model ID instead.
    count_tokens_model_id = _strip_region_prefix(model_config.provider_model_id)

    # cross-account(claude-code→374): invoke 와 동일 계정에서 CountTokens 실행 → 계정 스플릿 방지.
    # account_role_arn NULL(codex/기본)이면 in-account adapter 그대로(무회귀). assume 실패 시 859 폴백.
    _ct_xacct = bool(
        _ct_profile and _ct_profile.backend == "invoke" and _ct_profile.account_role_arn
    )
    if _ct_xacct:
        _provider = getattr(request.app.state, "bedrock_account_client_provider", None)
        _inacct_adapter = registry.get(ProviderType.BEDROCK)
        if _provider is not None:
            _role, _reg, _ext = (
                _ct_profile.account_role_arn,
                _ct_profile.region,
                _ct_profile.external_id,
            )
            adapter = BedrockAdapter(
                bedrock_client=None,
                client_resolver=lambda: _provider.get_client(_role, _reg, _ext),
                fallback_client=getattr(_inacct_adapter, "_client", None),
            )
        else:
            adapter = _inacct_adapter
    else:
        adapter = registry.get(ProviderType.BEDROCK)
    status, input_tokens = await adapter.count_tokens(body, count_tokens_model_id)

    if status != 200:
        return JSONResponse(
            status_code=status,
            content={"error": {"type": "provider_error", "message": "count_tokens failed"}},
        )

    return JSONResponse(status_code=200, content={"input_tokens": input_tokens})
