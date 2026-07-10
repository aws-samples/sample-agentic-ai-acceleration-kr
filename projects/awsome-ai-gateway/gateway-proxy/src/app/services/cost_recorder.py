# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

import structlog

from app.schemas.cost_stream import CostStreamEntry
from app.schemas.domain import AuthContext, ModelConfigSchema, TokenUsage

logger = structlog.get_logger(__name__)

COST_PRECISION = Decimal("0.000001")

# Redis Stream key for cost-recorder-worker offload. Worker XREADGROUP에서 소비.
COST_STREAM_KEY = "cost:stream"
# MAXLEN approx trim: 500 RPS × ~3분 buffer.
COST_STREAM_MAXLEN = 100_000


def calculate_cost(usage: TokenUsage, pricing: ModelConfigSchema) -> Decimal:
    """비용 계산: input + output + cache_write + cache_read.

    cache_write 단가는 요청의 cache TTL에 따라 분기:
      - 5-min (default): pricing.cache_write_per_1k
      - 1-hour (ttl=3600): pricing.cache_write_1h_per_1k
    """
    p = pricing.pricing
    input_cost = (Decimal(usage.input_tokens) / 1000) * p.input_per_1k
    output_cost = (Decimal(usage.output_tokens) / 1000) * p.output_per_1k
    cache_write_rate = p.cache_write_1h_per_1k if usage.cache_ttl_1h else p.cache_write_per_1k
    cache_write_cost = (Decimal(usage.cache_creation_input_tokens) / 1000) * cache_write_rate
    cache_read_cost = (Decimal(usage.cache_read_input_tokens) / 1000) * p.cache_read_per_1k
    return (input_cost + output_cost + cache_write_cost + cache_read_cost).quantize(
        COST_PRECISION, rounding=ROUND_HALF_UP
    )


class CostRecorder:
    """요청 완료 시점 critical path 처리기 (FR-3.3 리팩터, 2026-04-20).

    **Inline (gateway critical path, 동기 await)**:
    1. KI-08 zero-usage 가드 — tokenizer 역산까지 실패 시 TPM 예약만 해제
    2. ``calculate_cost``
    3. OTEL 메트릭
    4. Redis budget_deduct Lua (user + team) — 예산 enforcement 실시간 차감
    5. CPM/CPH ``settle_cost``
    6. TPM ``settle_tpm``
    7. XADD ``cost:stream`` — 나머지는 worker에게 위임

    **Offloaded (cost-recorder-worker via Redis Stream)**:
    - ``usage_logs`` INSERT (idempotent via ``request_id`` UNIQUE)
    - ``budget_usages`` UPSERT (limit_usd snapshot)
    - 당일 Redis 집계 카운터 (``usage:daily:*``) — 최대 ~5s 지연 허용
    - Threshold pub/sub ``notifications:budget``
    - ``daily_aggregates`` cron (KST 00:10)
    """

    def __init__(self, metrics=None, spool=None) -> None:
        self._metrics = metrics
        # P0-②: dead-letter spool for cost:stream XADD failures (Redis down at
        # finalize). When set, a failed XADD buffers the payload for re-publish on
        # Redis recovery instead of being lost forever.
        self._spool = spool

    async def finalize(
        self,
        redis,
        auth_context: AuthContext,
        model_config: ModelConfigSchema,
        usage: TokenUsage,
        request_id: str,
        is_stream: bool,
        duration_ms: int,
        ttft_ms: int | None = None,
        reserved_cost: Decimal = Decimal("0"),
        rate_limit_state: dict | None = None,
        downgraded_from: str | None = None,
        availability_fallback_from: str | None = None,
        bedrock_request_id: str | None = None,
        client: str | None = None,
    ) -> Decimal:
        """요청 완료 후 critical path 전체를 동기 await으로 수행. 실제 cost_usd 반환.

        라우터는 응답 반환 **전에** ``await finalize(...)`` 호출해야 함 —
        budget_deduct + settle_*가 다음 요청 enforce에 영향.
        XADD 자체는 1-2ms (DB I/O 없음).

        ``usage.total_tokens == 0`` (KI-08 tokenizer 역산까지 실패) →
        TPM 예약만 해제하고 return.
        """
        # KI-08: usage 없는 disconnect 경로 — TPM 예약만 해제
        if usage.total_tokens == 0 and usage.input_tokens == 0 and usage.output_tokens == 0:
            if rate_limit_state and redis is not None:
                try:
                    from app.services.rate_limit_service import RateLimitService

                    await RateLimitService().settle_tpm(
                        redis,
                        rate_limit_state.get("tpm_descriptors", []),
                        rate_limit_state.get("tpm_reserved", 0),
                        0,  # actual=0 → 전액 환불
                    )
                except Exception:
                    logger.warning(
                        "tpm_release_on_disconnect_failed",
                        user_id=auth_context.user_id,
                    )
            return Decimal("0")

        cost_usd = calculate_cost(usage, model_config)
        period = datetime.now(tz=UTC).strftime("%Y-%m")

        # OTEL metrics
        if self._metrics:
            model_name = model_config.alias or model_config.provider_model_id
            attrs = {
                "model": model_name,
                "user_id": auth_context.user_id,
                "team_id": auth_context.team_id or "",
            }
            self._metrics.token_usage_total.add(
                usage.input_tokens, {**attrs, "token_type": "input"}
            )
            self._metrics.token_usage_total.add(
                usage.output_tokens, {**attrs, "token_type": "output"}
            )
            if usage.cache_creation_input_tokens:
                self._metrics.token_usage_total.add(
                    usage.cache_creation_input_tokens, {**attrs, "token_type": "cache_write"}
                )
            if usage.cache_read_input_tokens:
                self._metrics.token_usage_total.add(
                    usage.cache_read_input_tokens, {**attrs, "token_type": "cache_read"}
                )
            self._metrics.cost_usd_total.add(float(cost_usd), attrs)

        # 1. Redis 예산 차감 + 임계값 체크
        threshold_triggered = None
        if redis is not None:
            from app.services.lua_loader import LuaScriptLoader

            # Redis Cluster hash tag: {<scope_id>} ensures usage/config keys
            # for the same user (or team) hash to the same slot, so Lua multi-key
            # operations never hit CROSSSLOT.
            user_usage_key = f"budget:user:{{{auth_context.user_id}}}:{period}"
            team_usage_key = f"budget:team:{{{auth_context.team_id}}}:{period}"
            user_config_key = f"budget:config:user:{{{auth_context.user_id}}}"
            team_config_key = f"budget:config:team:{{{auth_context.team_id}}}"

            result = None
            try:
                raw = await redis.eval(
                    LuaScriptLoader.get("budget_deduct"),
                    2,
                    user_usage_key,
                    user_config_key,
                    str(cost_usd),
                )
                result = json.loads(raw)
                threshold_triggered = result.get("threshold_triggered")
            except Exception:
                logger.exception("budget_deduct_failed", user_id=auth_context.user_id)

            # 팀 예산 차감
            try:
                await redis.eval(
                    LuaScriptLoader.get("budget_deduct"),
                    2,
                    team_usage_key,
                    team_config_key,
                    str(cost_usd),
                )
            except Exception:
                logger.warning("team_budget_deduct_failed", team_id=auth_context.team_id)

            # 앱(client) 예산 차감 — user 설정에 app_clients 로 등록된 client 만(free gate).
            if client in ("claude-code", "cowork", "codex"):
                app_clients = result.get("app_clients") if isinstance(result, dict) else None
                if isinstance(app_clients, list) and client in app_clients:
                    client_usage_key = f"budget:user:{{{auth_context.user_id}}}:{client}:{period}"
                    client_config_key = f"budget:config:user:{{{auth_context.user_id}}}:{client}"
                    try:
                        await redis.eval(
                            LuaScriptLoader.get("budget_deduct"),
                            2, client_usage_key, client_config_key, str(cost_usd),
                        )
                    except Exception:
                        logger.warning("client_budget_deduct_failed", client=client)

        # 2. CPM/CPH 정산 (USER+TEAM 2 스코프, FR-4.6)
        # reserved_cost는 rate_limit_state['cost_reserved'] (enforcement 주입) 우선,
        # 없으면 legacy 파라미터 사용.
        cost_reserved = reserved_cost
        if rate_limit_state and "cost_reserved" in rate_limit_state:
            cost_reserved = rate_limit_state["cost_reserved"]
        if redis is not None and cost_reserved != Decimal("0"):
            try:
                from app.services.rate_limit_service import RateLimitService

                await RateLimitService().settle_cost(
                    redis,
                    user_id=auth_context.user_id,
                    actual_cost=cost_usd,
                    reserved_cost=cost_reserved,
                    team_id=auth_context.team_id,
                )
            except Exception:
                logger.warning("cost_settle_failed", user_id=auth_context.user_id)

        # 2b. TPM 정산 (FR-4.1 §D2, settle_tpm) — reserve_tpm 상태가 state에 있을 때만
        tpm_descriptors = rate_limit_state.get("tpm_descriptors") if rate_limit_state else None
        tpm_reserved = rate_limit_state.get("tpm_reserved") if rate_limit_state else 0
        if redis is not None and tpm_descriptors and tpm_reserved > 0:
            try:
                from app.services.rate_limit_scope import compute_tpm_incr
                from app.services.rate_limit_service import RateLimitService

                actual_tpm = compute_tpm_incr(usage)
                await RateLimitService().settle_tpm(
                    redis, tpm_descriptors, tpm_reserved, actual_tpm
                )
            except Exception:
                logger.warning("tpm_settle_failed", user_id=auth_context.user_id)

        # 3. XADD cost:stream — cost-recorder-worker가 DB INSERT / daily counter /
        #    threshold pub/sub 배치 처리. 실패는 경고만 (gateway response는 영향 없음).
        if redis is not None:
            await self._publish_to_stream(
                redis,
                auth_context=auth_context,
                model_config=model_config,
                usage=usage,
                cost_usd=cost_usd,
                request_id=request_id,
                is_stream=is_stream,
                duration_ms=duration_ms,
                ttft_ms=ttft_ms,
                threshold_triggered=threshold_triggered,
                downgraded_from=downgraded_from,
                availability_fallback_from=availability_fallback_from,
                bedrock_request_id=bedrock_request_id,
                client=client,
            )

        return cost_usd

    async def _publish_to_stream(
        self,
        redis,
        *,
        auth_context: AuthContext,
        model_config: ModelConfigSchema,
        usage: TokenUsage,
        cost_usd: Decimal,
        request_id: str,
        is_stream: bool,
        duration_ms: int,
        ttft_ms: int | None = None,
        threshold_triggered: int | None,
        downgraded_from: str | None = None,
        availability_fallback_from: str | None = None,
        bedrock_request_id: str | None = None,
        client: str | None = None,
    ) -> None:
        """XADD cost:stream — worker가 배치 소비하여 DB 쓰기 + threshold pub/sub.

        XADD 실패 시(주로 Redis 장애) gateway response는 이미 반환됐으므로 예외
        전파하지 않는다. 단 P0-②: dead-letter spool 이 연결돼 있으면 payload를
        버퍼에 넣어 Redis 복구 시 재발행(re-XADD) → 기록 영구 유실 방지.
        """
        _DEFAULT_TEAM_ID = "00000000-0000-4000-a000-000000000003"
        _DEFAULT_DEPT_ID = "00000000-0000-4000-a000-000000000002"

        provider_str = (
            model_config.provider.value
            if hasattr(model_config.provider, "value")
            else str(model_config.provider)
        )

        entry = CostStreamEntry.make(
            request_id=request_id,
            user_id=auth_context.user_id,
            team_id=auth_context.team_id or _DEFAULT_TEAM_ID,
            dept_id=auth_context.dept_id or _DEFAULT_DEPT_ID,
            model_alias=model_config.alias or model_config.provider_model_id,
            provider=provider_str,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_creation_tokens=usage.cache_creation_input_tokens,
            cache_read_tokens=usage.cache_read_input_tokens,
            reasoning_tokens=usage.reasoning_tokens,
            web_search_count=usage.web_search_count,
            cost_usd=cost_usd,
            latency_ms=duration_ms,
            ttft_ms=ttft_ms,
            is_streaming=is_stream,
            estimated_usage=bool(usage.estimated),
            downgraded_from=downgraded_from,
            availability_fallback_from=availability_fallback_from,
            threshold_triggered=threshold_triggered,
            threshold_policy=None,  # worker가 budget_configs에서 조회해 채움
            sso_subject=auth_context.sso_subject,
            bedrock_request_id=bedrock_request_id,
            client=client,
        )

        payload_json = entry.model_dump_json()
        try:
            await redis.xadd(
                COST_STREAM_KEY,
                {"payload": payload_json},
                maxlen=COST_STREAM_MAXLEN,
                approximate=True,
            )
        except Exception:
            logger.exception(
                "cost_stream_xadd_failed",
                user_id=auth_context.user_id,
                request_id=request_id,
            )
            # P0-②: don't lose the record — spool for re-publish on Redis recovery.
            if self._spool is not None:
                try:
                    self._spool.enqueue(payload_json)
                except Exception:
                    logger.exception("cost_stream_spool_enqueue_failed", request_id=request_id)
