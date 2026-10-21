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
from fastapi import APIRouter, Request, Response
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
from app.services.account_credentials import profile_account_target, resolve_account_target
from app.services.body_log_records import (
    build_body_record_for_nonstream,
    build_body_record_for_stream,
    provider_name,
    resolve_body_logger,
)
from app.services.fallback_loop import (
    FallbackResult,
    enforce_candidate_admission,
    run_fallback_loop,
)
from app.services.fallback_resolver import make_same_provider
from app.services.router_service import RouterService
from app.services.streaming import bedrock_anthropic_sse_stream
from app.services.thinking_normalizer import normalize_thinking
from app.services.token_count_fallback import (
    BEDROCK_UNSUPPORTED,
    count_base_model_id,
    count_on_mantle,
    is_bedrock_unsupported,
)
from app.services.tool_filter import strip_unsupported_tools
from app.services.upstream_compat import (
    apply_forwarded_betas,
    client_betas,
    forward_beta_map,
)

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
# anthropic_beta and safeguards are deliberately NOT here. Bedrock takes betas only in the body
# and 400s on a name it does not know, so only the configured ones (and their paired fields)
# are added — by apply_forwarded_betas in _build_candidate_body, which is what makes the
# BEDROCK_FORWARD_BETAS kill switch work. Prompt caching needs no beta (cache_control in content).
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
    # ⚠️ adaptive 계열이 thinking 깊이를 제어하는 유일한 수단이다
    #    (`output_config.effort`). 여기 없으면 클라이언트가 보내도 우리가 버려서, 정규화를
    #    배선해도 깊이 제어가 동작하지 않는다.
    #
    # ⚠️ legacy 계열(haiku)은 이 필드를 받지 않는다. 그래서 이 필드를 허용하는 것은
    #    `normalize_thinking` 이 legacy 계열에서 이것을 **무조건 떨구는** 것과 한 쌍이다
    #    (thinking 형태와 무관하게). 그 두 변경 중 하나만 하면 haiku 가 400 을 내기 시작한다.
    "output_config",
}


@dataclass
class BackendDecision:
    provider: ProviderType
    profile: RoutingProfileSchema | None = None
    model_config: ModelConfigSchema | None = None  # pre-resolved for Mantle
    model_id: str | None = None          # resolved provider_model_id for Mantle; None = default Bedrock path
    endpoint: str | None = None


_COUNT_SOURCE_HEADER = "x-llm-gateway-token-count-source"
_COUNT_MODEL_HEADER = "x-llm-gateway-token-count-model"

_COUNT_ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    429: "rate_limit_error",
}


def _bedrock_count_body(count_body: dict, provider_model_id: str, request_id: str) -> dict:
    """The body /v1/messages would send this model, wrapped for Bedrock CountTokens.

    ``thinking`` is normalised exactly as the native path does (the 5 family runs
    ``adaptive``; Haiku 4.5 runs ``enabled``), so the count is of the request that will
    actually run. CountTokens generates nothing, but ``max_tokens`` is required and must
    exceed ``thinking.budget_tokens`` — Claude Code sends ``thinking`` without
    ``max_tokens`` whenever the conversation holds thinking blocks, and a bare
    placeholder of 1 made every such count a 400. The value does not change the count
    (1025, 8192 and 64000 measured identical).

    ``output_config`` is dropped: CountTokens rejects ``output_config.effort`` ("Extra
    inputs are not permitted") although InvokeModel accepts it, and effort never
    changes the input count (low/high/max measured identical).
    """
    # Shallow on purpose: every step below (and normalize_thinking) only reassigns
    # top-level keys. A deep copy recursed into client content and turned a deeply
    # nested body that json.loads accepts into a RecursionError (HTTP 500).
    b = dict(count_body)
    normalize_thinking(b, provider_model_id, request_id=request_id)
    b.pop("output_config", None)
    b["anthropic_version"] = "bedrock-2023-05-31"
    thinking = b.get("thinking")
    budget = (
        thinking.get("budget_tokens")
        if isinstance(thinking, dict) and thinking.get("type") == "enabled"
        else None
    )
    floor = budget + 1 if isinstance(budget, int) and budget > 0 else 1
    if not isinstance(b.get("max_tokens"), int) or b["max_tokens"] < floor:
        b["max_tokens"] = floor
    return b


def _count_tokens_error(result) -> JSONResponse:
    """The upstream reason, not a fixed string — "count_tokens failed" hid that the
    model, not the permission, was the problem. Account identifiers are already
    redacted by the adapter."""
    status = result[0]
    message = getattr(result, "error_message", None) or "count_tokens failed"
    return JSONResponse(
        status_code=status,
        content={
            "error": {"type": _COUNT_ERROR_TYPES.get(status, "api_error"), "message": message}
        },
    )


def _account_kwargs(cand_config) -> dict:
    """후보 alias 의 대상 계정(0043)을 어댑터 kwargs 로. 계정이 없으면 ``{}``.

    빈 dict 인 이유: 계정 없는 alias 의 어댑터 호출 인자가 이 기능 이전과 **같아야** 한다
    (account=None 키조차 없다). 우선순위는 alias 계정 → 경로별 오늘 규칙(프로필) → 파드이고,
    뒤 두 단계는 어댑터/브로커의 기존 코드가 그대로 정한다(services/account_credentials.py).
    """
    target = resolve_account_target(cand_config)
    return {"account": target} if target is not None else {}


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
    # 요청 단위 cross-account 어댑터(있으면). 실제 서빙 계정 기록(0045)이 "프로필 계정으로
    # 나갔는가, in-account 로 폴백했는가" 를 이 어댑터에 묻는다.
    _xacct_adapter: BedrockAdapter | None = None
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
                    # alias 계정(0043)이 있는 후보는 위 resolver 대신 그 계정으로 — 같은 리전
                    # (profile.region)에서, 폴백 없이.
                    account_clients=_provider,
                    account_region=_prof.region,
                )
                _xacct_adapter = adapter
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

    def _served_profile_account():
        """이 요청에서 **라우팅 프로필 계정이 실제로 서명에 쓰였다면** 그 계정(0045 기록용).

        서명 규칙과 같다: Mantle 은 프로필에 역할이 있으면 그 계정. native 는 claude-code 형
        cross-account 어댑터가 있고 그 호출이 in-account 로 폴백하지 **않았을** 때만 — 폴백한
        호출을 프로필 계정으로 기록하면 정산이 그 계정의 invocation log 에서 찾다가 "미매칭" 이
        된다. alias 계정은 finalize 쪽(effective_account_id)이 이 값보다 먼저 본다.
        """
        if is_mantle:
            return profile_account_target(decision.profile)
        if _xacct_adapter is not None and not _xacct_adapter.served_by_fallback:
            return profile_account_target(decision.profile)
        return None

    # 클라이언트가 보낸 anthropic-beta 와 넘길 목록은 요청마다 한 번만 읽는다
    # (_build_candidate_body 는 폴백 후보·웹 검색 턴마다 불린다).
    # English: read once per request; the body builder runs per fallback candidate and per
    # web-search turn.
    _client_betas = client_betas(request.headers.getlist("anthropic-beta"))
    _fwd_map = forward_beta_map(get_settings().bedrock_forward_betas)

    def _build_candidate_body(
        req_d: dict, cand_config: ModelConfigSchema, streaming: bool
    ) -> tuple[bytes, dict, dict]:
        """Build invoke body + kwargs for a given candidate model_config.

        ⚠️ 여기가 상류로 나가는 본문이 만들어지는 **단일 지점**이다 — 폴백 후보와 웹서치
           턴이 모두 이 함수를 지난다. 그래서 모델별 본문 정규화도 여기서 한다.
        """
        if is_mantle:
            mantle_b = {k: v for k, v in req_d.items() if k in _BEDROCK_ALLOWED_FIELDS}
            mantle_b.pop("anthropic_version", None)
            mantle_b["model"] = cand_config.provider_model_id
            if auth_context and auth_context.sso_subject:
                mantle_b["metadata"] = {"user_id": auth_context.sso_subject}
            if streaming:
                mantle_b["stream"] = True
            # ⚠️ Mantle 경로에서 Anthropic **네이티브** 도구 선언을 걷어낸다.
            #    Cowork 가 `web_search_20250305` / `code_execution_*` / `text_editor_*` /
            #    `computer_*` 를 선언하면 Mantle 은 "tool type is not supported for this
            #    model" 로 400 을 준다. 클라이언트가 고칠 수 없는 거부라 게이트웨이가
            #    걸러야 한다.
            #
            #    ⚠️ 우리가 주입하는 서버사이드 web_search 는 영향받지 않는다: 필터는
            #       `type` 필드로 판정하는데 주입된 도구에는 그 필드가 없다.
            mantle_b = strip_unsupported_tools(mantle_b, request_id=request_id)
            mantle_b = normalize_thinking(
                mantle_b, cand_config.provider_model_id, request_id=request_id
            )
            # 후보 **자신의** alias 계정(0043). model_config(원래 요청 alias)가 아니라
            # cand_config 다 — 폴백 후보는 다른 alias 이고 다른 계정일 수 있다. 없으면 kwargs 가
            # 오늘과 같다(브로커가 프로필 계정 → 파드).
            _acct = _account_kwargs(cand_config)
            return (
                json.dumps(mantle_b).encode(),
                {"profile": decision.profile, "endpoint": decision.endpoint, **_acct},
                {"profile": decision.profile, "endpoint": decision.endpoint, **_acct},
            )
        else:
            bedrock_b = {k: v for k, v in req_d.items() if k in _BEDROCK_ALLOWED_FIELDS}
            bedrock_b["anthropic_version"] = "bedrock-2023-05-31"
            if auth_context and auth_context.sso_subject:
                bedrock_b["metadata"] = {"user_id": auth_context.sso_subject}
            # ⚠️ `thinking` 의 형태를 **후보 모델이 받는 형태로** 맞춘다.
            #
            #    두 계열이 서로를 거부한다: opus-4-7/4-8/opus-5/sonnet-5/fable-5/mythos-5 는
            #    `{"type":"adaptive"}` 만 받고, haiku-4-5 는 `{"type":"enabled"}` 만 받는다.
            #    정규화가 없으면 MAX_THINKING_TOKENS>0 로 설정된 Claude Code 의 **모든**
            #    요청이 opus-4-8 에서 400 이고, 400 은 폴백 대상이 아니라 그대로 사용자에게
            #    간다. 반대 방향도 마찬가지다.
            #
            #    후보 **단위**로 하는 것이 핵심이다: 가용성 폴백과 예산 강등이 계열을 넘어
            #    모델을 바꾸므로, 79% 예산에서 되던 요청이 80% 에서 400 이 되는 경로가
            #    실재한다. 모르는 모델은 그대로 통과시킨다(fail-open).
            bedrock_b = strip_unsupported_tools(bedrock_b, request_id=request_id)
            bedrock_b = normalize_thinking(
                bedrock_b, cand_config.provider_model_id, request_id=request_id
            )
            # 정해 둔 anthropic-beta 와 짝 필드(safeguards)를 마지막에 넣는다 — 위의 정규화가
            # 지우지 못하게. Mantle 분기에는 넣지 않는다(헤더로 가야 하고 시험하지 않았다).
            # English: last, so no normalisation step above can drop them. Not on the Mantle
            # branch (betas would go as a header there; untested).
            apply_forwarded_betas(bedrock_b, req_d, _client_betas, _fwd_map)
            # 후보별 alias 계정 — Mantle 분기와 같은 이유(cand_config). 없으면 kwargs 가 오늘과
            # 같다(프로필 cross-account resolver + in-account 폴백, 또는 파드).
            _acct = _account_kwargs(cand_config)
            return (
                json.dumps(bedrock_b).encode(),
                {"path_suffix": "invoke-with-response-stream", **_acct},
                {"path_suffix": "invoke", **_acct},
            )

    # cross-account(333) 면 profile.region, 아니면 None (분기 전 초기화됨).
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

        # ⚠️ **입장 심사를 여기서 해야 한다.** 이 분기는 아래 `run_fallback_loop` 보다
        #    먼저 리턴하고, 그 폴백 루프가 `/v1/messages` 에서 스코프 2축과 레이트리밋을
        #    거는 **유일한** 지점이다. 그래서 이 분기는 오랫동안 다음을 전부 건너뛰었다:
        #      - check_key_scope            (사용자 × 모델 허용목록)
        #      - check_client_model_scope   (앱 × 모델 허용목록, migration 0035)
        #      - enforce_rate_limits        (RPM / TPM / 비용 한도)
        #    웹서치를 켠 프로파일에서만 그랬으므로 증상이 없었다 — 접근권 없는 모델도
        #    호출되고, 한도를 넘겨도 429 가 나지 않았다.
        #
        #    같은 함수를 폴백 루프도 쓴다(단일 구현). 여기서 거절되면 상류를 호출하지
        #    않으므로 예약도 남지 않는다.
        _admission = await enforce_candidate_admission(
            router_service=_router_service,
            auth_context=auth_context,
            candidate_config=model_config,
            redis=redis,
            req_data=req_data,
            state=state,
            request_id=request_id,
            budget_status=state.get("budget_status"),
        )
        if _admission is not None:
            return Response(
                content=_admission.body,
                status_code=_admission.status,
                headers=_admission.headers or None,
                media_type="application/json",
            )

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
                # 실제 서빙 계정 기록(0045).
                profile_account=_served_profile_account(),
            )

        # 본문 로깅 — 웹서치 경로 전용 훅. 이 return 은 아래 본문 로깅 배선보다 **먼저**
        # 일어나므로, 여기 걸지 않으면 웹서치를 켠 프로파일의 요청은 로깅 코드를 아예
        # 지나지 않고 조용히 미기록된다.
        #
        # ⚠️ bedrock_request_id 는 None 이다. 루프가 턴마다 별개의 Bedrock 호출을 하므로
        #    단일 요청 id 가 조인 키가 되지 못한다(`_ws_record` 의 같은 판단).
        async def _ws_log_stream(sse_text: str, log_status: str) -> None:
            bl = getattr(request.app.state, "body_logger", None)
            if bl is None:
                return
            await bl.enqueue(
                build_body_record_for_stream(
                    request_id=request_id,
                    provider=provider_name(model_config),
                    client=client,
                    model_alias=model_config.alias or model_config.provider_model_id,
                    status=log_status,
                    request_body=body,
                    sse_text=sse_text,
                    user_id=auth_context.user_id if auth_context else None,
                    team_id=auth_context.team_id if auth_context else None,
                    sso_subject=auth_context.sso_subject if auth_context else None,
                    bedrock_request_id=None,
                )
            )

        async def _ws_log_nonstream(resp_status: int, resp_body: bytes) -> None:
            bl = getattr(request.app.state, "body_logger", None)
            if bl is None:
                return
            await bl.enqueue(
                build_body_record_for_nonstream(
                    request_id=request_id,
                    provider=provider_name(model_config),
                    client=client,
                    model_alias=model_config.alias or model_config.provider_model_id,
                    status_code=resp_status,
                    request_body=body,
                    response_body=resp_body,
                    is_streaming=False,
                    user_id=auth_context.user_id if auth_context else None,
                    team_id=auth_context.team_id if auth_context else None,
                    sso_subject=auth_context.sso_subject if auth_context else None,
                    bedrock_request_id=None,
                )
            )

        # KI-08 역산 훅 — 웹서치 경로 **전용**.
        #
        # ⚠️ 아래쪽 `_estimate` 를 재사용할 수 없다. 그것은 `call_model_id_final` /
        #    `is_mantle` 에 의존하고 둘 다 폴백 루프 **뒤에** 계산되므로, 여기서 참조하면
        #    UnboundLocalError 다(실측으로 잡았다 — "함수 안에 정의돼 있다" 는 AST 검사로는
        #    잡히지 않는다). 이 경로는 폴백을 타지 않으므로 model_config 가 곧 실제 모델이다.
        _ws_tokenizer = getattr(request.app.state, "tokenizer", None)

        async def _ws_estimate(text: str) -> int | None:
            if not _ws_tokenizer:
                return None
            return await _ws_tokenizer.estimate_output_tokens(
                text,
                provider=model_config.provider,
                model_id=model_config.provider_model_id,
            )

        # 사전 게이팅 — 훅을 넘기면 루프가 SSE 전문을 누적한다.
        _ws_logging = await resolve_body_logger(request.app.state, redis, session_factory)

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
            max_result_chars=_settings_ws.web_search_max_result_chars,
            max_searches_per_turn=_settings_ws.web_search_max_searches_per_turn,
            handshake_timeout=_settings_ws.agentcore_handshake_timeout,
            tokenizer_hook=_ws_estimate,
            on_stream_complete=_ws_log_stream if _ws_logging else None,
            on_nonstream_complete=_ws_log_nonstream if _ws_logging else None,
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
        metrics=getattr(request.app.state, "metrics", None),
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
                # 실제 서빙 계정 기록(0045).
                profile_account=_served_profile_account(),
            )

        response_headers: dict = {}
        if availability_fallback_from:
            response_headers["x-llm-gateway-fallback-from"] = availability_fallback_from

        async def _log_body_stream(sse_text: str, log_status: str) -> None:
            """스트림 종료 시 본문을 큐에 넣는다. 절대 블로킹하지 않는다(enqueue 만)."""
            bl = getattr(request.app.state, "body_logger", None)
            if bl is None:
                return
            await bl.enqueue(
                build_body_record_for_stream(
                    request_id=request_id,
                    provider=provider_name(effective_model_config),
                    client=client,
                    model_alias=(
                        effective_model_config.alias
                        or effective_model_config.provider_model_id
                    ),
                    status=log_status,
                    request_body=body,
                    sse_text=sse_text,
                    user_id=auth_context.user_id if auth_context else None,
                    team_id=auth_context.team_id if auth_context else None,
                    sso_subject=auth_context.sso_subject if auth_context else None,
                )
            )

        # ⚠️ **사전** 게이팅이다. on_complete 를 넘기면 제너레이터가 SSE 프레임 전문을
        #    메모리에 누적하므로(services/streaming.py OnComplete 주석), 로깅이 꺼져
        #    있을 때 그 비용을 내지 않으려면 스트림이 시작되기 **전에** 판정해야 한다.
        #    끝나고 물어보면 모든 요청에 대해 응답 본문을 버릴 목적으로 버퍼링하게 된다.
        _on_complete = (
            _log_body_stream
            if await resolve_body_logger(request.app.state, redis, session_factory)
            else None
        )

        return StreamingResponse(
            bedrock_anthropic_sse_stream(
                request,
                chunk_iter,
                on_usage=_record,
                tokenizer_hook=_estimate,
                on_complete=_on_complete,
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
                # Join key to the Bedrock model-invocation log record. The streaming
                # branch above has always recorded it; this one did not, so every
                # non-streaming Claude call landed with a NULL id and was unauditable
                # even though AWS had logged it. `headers` comes from adapter.invoke via
                # FallbackResult.payload and is never sent to the client.
                bedrock_request_id=(headers or {}).get("x-amzn-requestid"),
                client=client,
                # 실제 서빙 계정 기록(0045).
                profile_account=_served_profile_account(),
            )

        # 본문 로깅(성공 **및** 오류). enqueue 만 하므로 응답을 지연시키지 않는다.
        # effective_model_config 를 쓴다 — 폴백이 일어났으면 실제로 응답한 모델이 남아야 한다.
        body_logger = await resolve_body_logger(request.app.state, redis, session_factory)
        if body_logger is not None:
            await body_logger.enqueue(
                build_body_record_for_nonstream(
                    request_id=request_id,
                    provider=provider_name(effective_model_config),
                    client=client,
                    model_alias=(
                        effective_model_config.alias
                        or effective_model_config.provider_model_id
                    ),
                    status_code=status,
                    request_body=body,
                    response_body=response_body,
                    is_streaming=False,
                    user_id=auth_context.user_id if auth_context else None,
                    team_id=auth_context.team_id if auth_context else None,
                    sso_subject=auth_context.sso_subject if auth_context else None,
                    bedrock_request_id=(headers or {}).get("x-amzn-requestid"),
                )
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
    # claude-code→374 cross-account: CountTokens 도 invoke 와 동일 계정(333)에서 실행되도록
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

    request_id = state.get("request_id", "")
    try:
        req_data = json.loads(body)
        model_alias = req_data.get("model", "")
        count_body = {k: v for k, v in req_data.items() if k in _BEDROCK_ALLOWED_FIELDS}
        count_body.pop("anthropic_version", None)
        # Same filter as the /v1/messages body builder — an Anthropic-only tool (Claude
        # Code's advisor) or a tool_addition naming one is a 400 here too.
        count_body = strip_unsupported_tools(count_body, request_id=request_id)
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

    # CountTokens rejects cross-region inference profile IDs (global./us./apac./jp./...)
    # and profile ARNs — use the base foundation-model ID instead.
    count_tokens_model_id = count_base_model_id(
        _strip_region_prefix(model_config.provider_model_id)
    )
    body = json.dumps(
        _bedrock_count_body(count_body, model_config.provider_model_id, request_id)
    ).encode()

    # cross-account(claude-code→374): invoke 와 동일 계정에서 CountTokens 실행 → 계정 스플릿 방지.
    # account_role_arn NULL(codex/기본)이면 in-account adapter 그대로(무회귀). assume 실패 시 859 폴백.
    _ct_xacct = bool(
        _ct_profile and _ct_profile.backend == "invoke" and _ct_profile.account_role_arn
    )
    settings = get_settings()
    # The Mantle fallback below must count with the account and region this branch picks
    # for Bedrock — decided here, once, so the two cannot drift apart.
    count_region, count_role, count_external_id = settings.aws_region, None, None
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
                account_clients=_provider,
                account_region=_ct_profile.region,
            )
            count_region, count_role, count_external_id = _reg, _role, _ext
        else:
            adapter = _inacct_adapter
    else:
        adapter = registry.get(ProviderType.BEDROCK)
    # alias 별 대상 계정(0043). CountTokens 도 모델 액세스를 가진 계정에서만 허가된다 — 다른
    # 계정으로 서빙되는 alias 를 여기서 세면 추론은 되는데 카운트만 403 이 된다(위 claude-code
    # 계정 스플릿과 같은 부류). alias 계정이 없으면 인자가 오늘과 같다.
    _acct_kwargs = _account_kwargs(model_config)
    # Bedrock CountTokens 가 이 모델을 아예 못 센다고 이미 답했으면 다시 묻지 않는다.
    if count_tokens_model_id not in BEDROCK_UNSUPPORTED:
        result = await adapter.count_tokens(body, count_tokens_model_id, **_acct_kwargs)
        if result[0] == 200:
            return JSONResponse(
                status_code=200,
                content={"input_tokens": result[1]},
                headers={_COUNT_SOURCE_HEADER: "bedrock"},
            )
        if not is_bedrock_unsupported(result):
            return _count_tokens_error(result)
        BEDROCK_UNSUPPORTED.add(count_tokens_model_id)

    # 여기부터는 Bedrock 이 이 모델의 카운트를 지원하지 않는 경우다(Opus 4.7+, 5 계열 —
    # 권한과 무관). 켜져 있으면 같은 계정·같은 리전의 Mantle 로 센다.
    region = count_region
    if settings.count_tokens_mantle_fallback:
        alias_account = _acct_kwargs.get("account")
        try:
            mantle_adapter = registry.get(ProviderType.BEDROCK_MANTLE)
        except ValueError:
            mantle_adapter = None
        try:
            counted = await count_on_mantle(
                mantle_adapter,
                body=count_body,
                base_model=count_tokens_model_id,
                region=region,
                account=alias_account,
                profile_role_arn=count_role if alias_account is None else None,
                profile_external_id=count_external_id if alias_account is None else None,
                client=client,
            )
        except Exception:
            # A malformed client body must not become a gateway 500.
            logger.warning("count_tokens_mantle_fallback_failed", exc_info=True)
            counted = None
        if counted is not None:
            return JSONResponse(
                status_code=200,
                content={"input_tokens": counted.input_tokens},
                headers={
                    _COUNT_SOURCE_HEADER: "mantle",
                    _COUNT_MODEL_HEADER: counted.model,
                },
            )

    # 400 이지 501 이 아니다: Claude Code 는 게이트웨이의 501 을 받으면 실제 /v1/messages 를
    # max_tokens=1 로 보내 그 usage 로 센다 — 과금되는 호출이다. 400 이면 자체 추정으로 넘어간다.
    detail = (
        f"and Mantle could not count it in {region}"
        if settings.count_tokens_mantle_fallback
        else "(the Mantle count fallback is off: COUNT_TOKENS_MANTLE_FALLBACK)"
    )
    return JSONResponse(
        status_code=400,
        content={
            "error": {
                "type": "invalid_request_error",
                "message": (
                    f"Token counting is not available for model '{model_config.alias}': "
                    f"Bedrock CountTokens does not support {count_tokens_model_id} {detail}."
                ),
            }
        },
    )
