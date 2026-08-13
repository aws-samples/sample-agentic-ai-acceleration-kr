# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import asyncio
import time

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.exc import DBAPIError

from app.schemas.domain import ProviderType, TokenUsage
from app.schemas.responses import ModelObject, ModelPricingObject, ModelsListResponse
from app.services.router_service import ModelInactiveError, RouterService
from app.services.streaming import openai_sse_stream

logger = structlog.get_logger(__name__)

router = APIRouter()
_router_service = RouterService()


@router.get("/v1/models")
async def list_models(request: Request) -> JSONResponse:
    state = request.scope.get("state", {})
    redis = state.get("_redis")
    session_factory = state.get("_session_factory")

    if session_factory is not None:
        async with session_factory() as db:
            models = await _router_service.list_active_models(redis, db)
    else:
        models = await _router_service.list_active_models(redis, None)
    data = []
    for m in models:
        pricing_obj = None
        if m.pricing and (m.pricing.input_per_1k or m.pricing.output_per_1k):
            pricing_obj = ModelPricingObject(
                input_per_1k_usd=m.pricing.input_per_1k,
                output_per_1k_usd=m.pricing.output_per_1k,
            )
        model_id = m.alias or m.provider_model_id
        data.append(
            ModelObject(
                id=model_id,
                display_name=m.description or model_id,
                created_at=m.created_at.isoformat() if m.created_at else None,
                created=int(m.created_at.timestamp()) if m.created_at else 0,
                provider=m.provider.value,
                api_format=m.api_format.value,
                provider_model_id=m.provider_model_id,
                description=m.description,
                pricing=pricing_obj,
            )
        )
    response = ModelsListResponse(
        data=data,
        first_id=data[0].id if data else None,
        last_id=data[-1].id if data else None,
    )
    return JSONResponse(content=response.model_dump(mode="json"))


@router.get("/v1/models/{model_id:path}")
async def get_model(model_id: str, request: Request) -> JSONResponse:
    """Anthropic 호환 single-model detail endpoint.

    Claude Code 클라이언트는 세션 시작 시 `/v1/models/{id}` 를 호출해서 모델
    가용성을 검증함. 이 엔드포인트가 없으면 (404/401) 모델을 사용 불가로 판단하고
    `/v1/messages` 호출을 아예 시작하지 않음.
    """
    state = request.scope.get("state", {})
    redis = state.get("_redis")
    session_factory = state.get("_session_factory")

    if session_factory is not None:
        async with session_factory() as db:
            models = await _router_service.list_active_models(redis, db)
    else:
        models = await _router_service.list_active_models(redis, None)
    match = next((m for m in models if (m.alias or m.provider_model_id) == model_id), None)
    if match is None:
        return JSONResponse(
            status_code=404,
            content={
                "type": "error",
                "error": {"type": "not_found_error", "message": f"Model '{model_id}' not found"},
            },
        )

    pricing_obj = None
    if match.pricing and (match.pricing.input_per_1k or match.pricing.output_per_1k):
        pricing_obj = ModelPricingObject(
            input_per_1k_usd=match.pricing.input_per_1k,
            output_per_1k_usd=match.pricing.output_per_1k,
        )
    resolved_id = match.alias or match.provider_model_id
    obj = ModelObject(
        id=resolved_id,
        display_name=match.description or resolved_id,
        created_at=match.created_at.isoformat() if match.created_at else None,
        created=int(match.created_at.timestamp()) if match.created_at else 0,
        provider=match.provider.value,
        api_format=match.api_format.value,
        provider_model_id=match.provider_model_id,
        description=match.description,
        pricing=pricing_obj,
    )
    return JSONResponse(content=obj.model_dump(mode="json"))


@router.post("/v1/chat/completions", response_model=None)
async def chat_completions(request: Request) -> StreamingResponse | JSONResponse:
    return await _handle_openai(request, "/v1/chat/completions")


@router.post("/v1/completions", response_model=None)
async def completions(request: Request) -> StreamingResponse | JSONResponse:
    return await _handle_openai(request, "/v1/completions")


@router.post("/v1/responses", response_model=None)
async def responses(request: Request) -> StreamingResponse | JSONResponse:
    """OpenAI **Responses API** endpoint — Codex -> Bedrock Mantle GPT-5.5.

    Distinct from _handle_openai (Chat Completions -> OPENMODEL/vLLM): this path is
    routing-profile-driven. A request whose identified client has a `mantle` routing
    profile (e.g. codex) is dispatched to the BEDROCK_MANTLE_OPENAI adapter using the
    profile's region/account + the profile's default_model alias. We deliberately do
    NOT touch _handle_openai so the existing chat/completions behaviour is unchanged.
    """
    return await _handle_responses(request)


async def _handle_openai(request: Request, path: str):
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

    # 모델 alias 추출
    import json

    try:
        req_data = json.loads(body)
        alias = req_data.get("model", "")
        is_stream = req_data.get("stream", False)
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"error": {"type": "invalid_request", "message": "Invalid JSON"}},
        )

    from app.schemas.domain import DegradationLevel

    dm = state.get("_degradation_manager")
    is_db_degraded = dm and dm.level in (
        DegradationLevel.DB_DEGRADED,
        DegradationLevel.BOTH_DEGRADED,
    )

    try:
        if not is_db_degraded and session_factory is not None:
            async with session_factory() as db:
                model_config = await _router_service.resolve_openai_model(redis, db, alias)
        else:
            model_config = await _router_service.resolve_openai_model(redis, None, alias)
    except LookupError as e:
        return JSONResponse(
            status_code=404, content={"error": {"type": "not_found", "message": str(e)}}
        )

    state["model_config"] = model_config

    # Key Scope 검사
    if auth_context:
        try:
            _router_service.check_key_scope(auth_context, model_config)
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

    # Pre-reserve RPM + TPM (3-scope: USER/TEAM/GLOBAL)
    if auth_context:
        from app.services.rate_limit_enforcement import enforce_rate_limits

        rejected = await enforce_rate_limits(
            redis=redis,
            auth_context=auth_context,
            model_config=model_config,
            body=req_data if isinstance(req_data, dict) else {},
            state=state,
            request_id=request_id,
            budget_status=state.get("budget_status"),
        )
        if rejected is not None:
            return rejected

    adapter = registry.get(ProviderType.OPENMODEL)
    rate_limit_state = state.get("rate_limit_state")
    tokenizer = getattr(request.app.state, "tokenizer", None)

    if is_stream:
        status, chunk_iter, headers = await adapter.invoke_stream(
            body, model_config.provider_model_id, path=path
        )

        # KI-08: OpenAI path는 tiktoken(cl100k_base) 근사로 출력 토큰 역산.
        async def _estimate(text: str) -> int | None:
            if not tokenizer:
                return None
            return await tokenizer.estimate_output_tokens(
                text,
                provider=ProviderType.OPENMODEL,
                model_id=model_config.provider_model_id,
            )

        async def _record(usage: TokenUsage, first_token_time: float | None) -> None:
            if not auth_context:
                return
            duration_ms = int((time.monotonic() - start_time) * 1000)
            if first_token_time is not None:
                ttft_ms = int((first_token_time - start_time) * 1000)
            else:
                ttft_ms = duration_ms
            await cost_recorder.finalize(
                redis,
                auth_context,
                model_config,
                usage,
                request_id,
                True,
                duration_ms,
                ttft_ms=ttft_ms,
                rate_limit_state=rate_limit_state,
                downgraded_from=state.get("downgraded_from"),
                client=client,
            )

        return StreamingResponse(
            openai_sse_stream(
                request, chunk_iter, on_usage=_record, tokenizer_hook=_estimate
            ),
            status_code=status,
            media_type="text/event-stream",
        )
    else:
        status, response_body, _, usage = await adapter.invoke(
            body, model_config.provider_model_id, path=path
        )
        if auth_context and usage.total_tokens > 0:
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
                client=client,
            )
        try:
            content = json.loads(response_body)
        except Exception:
            content = {"error": {"type": "provider_error", "message": "Invalid response"}}
        return JSONResponse(status_code=status, content=content)


async def _handle_responses(request: Request):
    """Routing-profile-driven OpenAI Responses handler (Codex -> Mantle GPT-5.5).

    Resolves the backend from the identified client's routing profile (mantle), then
    dispatches to BEDROCK_MANTLE_OPENAI. Auth/key-scope/rate-limit/cost mirror the
    chat path; budget enforcement already ran in middleware (path now registered).
    """
    import json

    state = request.scope.get("state", {})
    auth_context = state.get("auth_context")
    client = state.get("client")
    redis = state.get("_redis")
    session_factory = state.get("_session_factory")
    registry = request.app.state.provider_registry
    cost_recorder = request.app.state.cost_recorder
    request_id = state.get("request_id", "")
    start_time = state.get("request_start_time", time.monotonic())
    routing_loader = getattr(request.app.state, "routing_profile_loader", None)

    body = await request.body()
    try:
        req_data = json.loads(body)
        requested_alias = req_data.get("model", "")
        is_stream = bool(req_data.get("stream", False))
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"error": {"type": "invalid_request", "message": "Invalid JSON"}},
        )

    from app.schemas.domain import DegradationLevel

    dm = state.get("_degradation_manager")
    is_db_degraded = dm and dm.level in (
        DegradationLevel.DB_DEGRADED,
        DegradationLevel.BOTH_DEGRADED,
    )

    # Load the client's routing profile. Codex must have a mantle profile with a
    # default_model; without it (or wrong client), /v1/responses is not serviceable
    # here (we do NOT silently fall back to vLLM).
    async def _load_profile(db):
        if routing_loader is None:
            return None
        return await routing_loader.load(redis, db, client)

    # Per-request model selection (added with the GPT-5.6 Sol/Terra/Luna aliases).
    #
    # Before this, the profile's default_model was the ONLY reachable model on this
    # route, so registering three GPT-5.6 aliases would have left all three
    # unreachable: a client sending {"model": "codex-gpt-5.6-sol"} still got the
    # default. Now the client's own model wins WHEN it resolves to an ACTIVE
    # BEDROCK_MANTLE_OPENAI alias.
    #
    # Falling back (rather than 404ing) on an unresolvable value is what makes this
    # change regression-free for existing clients: Codex always sends SOME model id,
    # typically an upstream OpenAI name like "gpt-5.5-codex" that is not one of our
    # aliases. Before this change that value was ignored; a strict lookup would turn
    # every such request into a 404. The default_model path below is therefore reached
    # by exactly the requests that reached it before.
    #
    # This is NOT an entitlement bypass: whatever is resolved goes through
    # check_key_scope() below, which matches against {alias, provider_model_id}. A key
    # scoped to codex-gpt cannot reach a GPT-5.6 alias by asking for it.
    #
    # KNOWN COST, accepted: a model name we do not register costs 2 extra indexed
    # SELECTs (alias match, then provider_model_id match) before the fallback, on every
    # such request -- failed lookups are not negatively cached, and a Codex client sends
    # the same unregistered name every time. Measured: exactly 2 queries per miss.
    # Accepted rather than optimised because the alternative (caching negative lookups)
    # would delay a newly-registered alias becoming reachable, and an operator adding a
    # model expects it to work immediately. If this ever shows up in DB load, negatively
    # cache the miss with a TTL well under MODEL_CACHE_TTL rather than reordering these
    # lookups -- and note the cost lands only on clients sending unregistered names.
    def _is_usable_model_ref(value) -> bool:
        """True only for a value we can safely hand to the resolver.

        "model" comes straight from untrusted client JSON, so it is not necessarily a
        usable string, and an unusable one must not reach the resolver: the resolver's
        failures below are caught as LookupError, but these two failure modes are NOT
        LookupError, so they would escape the fallback and 500 a request that returned
        200 before this feature existed.

          * non-str (number/list/dict/bool) → asyncpg gets a non-text bind for a VARCHAR
            comparison and raises DBAPIError. Verified against real Postgres with
            123 / ["a"] / {"x":1} / True.
          * lone UTF-16 surrogate (e.g. {"model": "\\ud800"}) → valid JSON and a real str,
            but redis-py encodes cache keys with encoding_errors="strict", so
            redis.get(f"model:{ref}") raises UnicodeEncodeError before any DB call.
            Verified directly against redis-py's Encoder.
          * NUL byte (e.g. {"model": "codex\\u0000gpt"}) → valid JSON, a real str, and
            encodes to UTF-8 cleanly, so neither check above catches it. Postgres has no
            NUL in text: asyncpg raises CharacterNotInRepertoireError ("invalid byte
            sequence for encoding UTF8: 0x00"). Verified against real Postgres.

        All are rejected here rather than caught below, so the profile default serves
        them exactly as it did before per-request selection.

        Deliberately NOT rejected: an over-long value. Postgres compares
        `alias = $1` by VALUE, not by the column's declared width, so a 100 000-char ref
        simply matches nothing and falls back (verified: HTTP 200). Length is bounded
        for LOGGING only, at the log call below.
        """
        if not isinstance(value, str) or not value or "\x00" in value:
            return False
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return False
        return True

    async def _resolve_model(db, profile):
        """Resolve the client-sent model, falling back to the profile default."""
        if _is_usable_model_ref(requested_alias) and requested_alias != profile.default_model:
            try:
                return await _router_service.resolve_codex_model(redis, db, requested_alias)
            except ModelInactiveError:
                # Do NOT fall back. status=INACTIVE is an operator kill switch: an alias
                # that exists but was deliberately disabled must be refused, not quietly
                # served as some other model (which would also bill the wrong alias).
                # Propagates as a 404 via the LookupError handler below.
                raise
            except (LookupError, DBAPIError):
                # The alias is genuinely not ours (unknown name, or a different
                # provider's alias) → the pre-existing behaviour: profile default.
                #
                # DBAPIError is a deliberate backstop, not a substitute for the guard
                # above: "model" is untrusted client JSON, and a driver-level rejection
                # of some value we did not anticipate must degrade to the default (the
                # pre-feature behaviour) rather than 500 a request that used to succeed.
                # It is caught HERE only — around the *requested* model. The default
                # model's own lookup below is left to propagate, because a broken
                # default is a real server fault and must not be hidden.
                #
                # INFO, not DEBUG: this is a silent model substitution, so it has to be
                # visible at the default log level to be diagnosable. Truncated to the
                # alias column's own width (128): the value is unbounded client input,
                # and logging it verbatim turns one request into a multi-MB log line,
                # while nothing legitimate is ever longer than the column allows.
                logger.info(
                    "responses_requested_model_unresolved_using_default",
                    requested=requested_alias[:128],
                    default_model=profile.default_model,
                    client=client,
                )
        return await _router_service.resolve_codex_model(redis, db, profile.default_model)

    try:
        if not is_db_degraded and session_factory is not None:
            async with session_factory() as db:
                profile = await _load_profile(db)
                if profile is None or profile.backend != "mantle" or not profile.default_model:
                    return JSONResponse(
                        status_code=404,
                        content={"error": {"type": "not_found",
                                            "message": "No Responses backend for this client"}},
                    )
                model_config = await _resolve_model(db, profile)
        else:
            profile = await _load_profile(None)
            if profile is None or profile.backend != "mantle" or not profile.default_model:
                return JSONResponse(
                    status_code=404,
                    content={"error": {"type": "not_found",
                                        "message": "No Responses backend for this client"}},
                )
            model_config = await _resolve_model(None, profile)
    except LookupError as e:
        return JSONResponse(
            status_code=404, content={"error": {"type": "not_found", "message": str(e)}}
        )

    state["model_config"] = model_config

    # Key scope (model allow-list) — the entitlement gate (trust axis).
    if auth_context:
        try:
            _router_service.check_key_scope(auth_context, model_config)
        except PermissionError:
            return JSONResponse(
                status_code=400,
                content={"error": {"type": "invalid_request_error",
                                    "message": f"Your account does not have access to model '{model_config.alias}'. Contact your administrator to request access."}},
            )

    # Pre-reserve RPM + TPM (USER/TEAM/GLOBAL) — same enforcement as chat path.
    if auth_context:
        from app.services.rate_limit_enforcement import enforce_rate_limits

        rejected = await enforce_rate_limits(
            redis=redis,
            auth_context=auth_context,
            model_config=model_config,
            body=req_data if isinstance(req_data, dict) else {},
            state=state,
            request_id=request_id,
            budget_status=state.get("budget_status"),
        )
        if rejected is not None:
            return rejected

    adapter = registry.get(ProviderType.BEDROCK_MANTLE_OPENAI)
    rate_limit_state = state.get("rate_limit_state")

    # Rewrite the outgoing model id to the resolved provider_model_id (e.g. openai.gpt-5.5,
    # openai.gpt-5.6-terra), so the alias the client sent (codex-gpt-5.6-terra) — or the
    # profile default, when the client sent something we do not recognise — maps to the
    # Mantle model. Mantle only accepts provider model ids, never our aliases.
    if isinstance(req_data, dict):
        req_data["model"] = model_config.provider_model_id
        body = json.dumps(req_data).encode()

    # --- Server-side web search (Architecture C) — opt-in per routing profile ---
    # When the codex profile enables web search AND the AgentCore MCP client is present,
    # run the Responses-dialect tool-use loop instead of the plain single dispatch below.
    # Skipped otherwise → the existing dispatch is byte-identical (zero regression).
    mcp_client = getattr(request.app.state, "agentcore_mcp_client", None)
    if (
        mcp_client is not None
        and profile is not None
        and getattr(profile, "web_search_enabled", False)
        and isinstance(req_data, dict)
    ):
        from app.config import get_settings
        from app.services.web_search_loop import run_web_search_loop

        _pmid = model_config.provider_model_id

        async def _ws_invoke(turn_body: dict) -> tuple[int, bytes, dict, TokenUsage]:
            tb = dict(turn_body)
            tb["model"] = _pmid
            return await adapter.invoke(
                json.dumps(tb).encode(), _pmid, profile=profile, endpoint=model_config.endpoint
            )

        async def _ws_invoke_stream(turn_body: dict):
            tb = dict(turn_body)
            tb["model"] = _pmid
            return await adapter.invoke_stream(
                json.dumps(tb).encode(), _pmid, profile=profile, endpoint=model_config.endpoint
            )

        async def _ws_record(usage: TokenUsage) -> None:
            if not auth_context:
                return
            duration_ms = int((time.monotonic() - start_time) * 1000)
            await cost_recorder.finalize(
                redis, auth_context, model_config, usage, request_id, is_stream, duration_ms,
                rate_limit_state=rate_limit_state,
                downgraded_from=state.get("downgraded_from"), client=client,
            )

        _settings_ws = get_settings()
        return await run_web_search_loop(
            dialect="responses",
            invoke=_ws_invoke,
            invoke_stream=_ws_invoke_stream,
            initial_req_data=req_data,
            is_stream=is_stream,
            mcp_client=mcp_client,
            request=request,
            on_usage=_ws_record,
            max_iterations=_settings_ws.web_search_max_iterations,
            total_deadline_sec=_settings_ws.web_search_total_deadline_sec,
            default_max_results=_settings_ws.web_search_max_results_default,
        )

    if is_stream:
        status, chunk_iter, headers, _ = await adapter.invoke_stream(
            body, model_config.provider_model_id,
            profile=profile, endpoint=model_config.endpoint,
        )

        async def _record(usage: TokenUsage, first_token_time: float | None) -> None:
            if not auth_context:
                return
            duration_ms = int((time.monotonic() - start_time) * 1000)
            if first_token_time is not None:
                ttft_ms = int((first_token_time - start_time) * 1000)
            else:
                ttft_ms = duration_ms
            await cost_recorder.finalize(
                redis, auth_context, model_config, usage, request_id, True, duration_ms,
                ttft_ms=ttft_ms,
                rate_limit_state=rate_limit_state,
                downgraded_from=state.get("downgraded_from"), client=client,
            )

        from app.services.streaming import responses_sse_stream

        return StreamingResponse(
            responses_sse_stream(request, chunk_iter, on_usage=_record),
            status_code=status,
            media_type="text/event-stream",
        )
    else:
        status, response_body, _, usage = await adapter.invoke(
            body, model_config.provider_model_id,
            profile=profile, endpoint=model_config.endpoint,
        )
        if auth_context and usage.total_tokens > 0:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            await cost_recorder.finalize(
                redis, auth_context, model_config, usage, request_id, False, duration_ms,
                ttft_ms=duration_ms,
                rate_limit_state=rate_limit_state,
                downgraded_from=state.get("downgraded_from"), client=client,
            )
        try:
            content = json.loads(response_body)
        except Exception:
            content = {"error": {"type": "provider_error", "message": "Invalid response"}}
        return JSONResponse(status_code=status, content=content)
