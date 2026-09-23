# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import json
from decimal import ROUND_HALF_UP, Decimal

import structlog

from app.periods import current_kst_period
from app.schemas.cost_stream import CostStreamEntry
from app.schemas.domain import AuthContext, ModelConfigSchema, TokenUsage

logger = structlog.get_logger(__name__)

COST_PRECISION = Decimal("0.000001")

# Redis Stream key for cost-recorder-worker offload. Worker XREADGROUP에서 소비.
COST_STREAM_KEY = "cost:stream"
# MAXLEN approx trim: 500 RPS × ~3분 buffer.
COST_STREAM_MAXLEN = 100_000


def resolve_context_tier(usage: TokenUsage, pricing: ModelConfigSchema) -> str | None:
    """272K long-context 티어 판정 — 과금과 감사기록(usage_logs.context_tier)이 공유하는
    단일 판정점 (마이그레이션 0038/0039).

    반환:
      · None    = 티어 없는 모델(threshold 미설정: Claude 등) → 단일 요율.
      · "short" = 티어 있는 모델이고 이번 요청 프롬프트가 임계값 이하.
      · "long"  = 티어 있는 모델이고 임계값 초과 → 입력 2배·출력 1.5배로 청구됨.

    ★ 비교 대상은 usage.input_tokens 가 아니라 **프롬프트 총량**(세 입력 버킷의 합)이다.
      split_openai_input 이 OpenAI usage 를 서로 겹치지 않는 세 버킷(비캐시/캐시읽기/캐시쓰기)
      으로 분해하므로, AWS 가 말하는 "input tokens"(캐시 포함 프롬프트)를 되살리려면 셋을 다시
      더해야 한다. input_tokens 만 보면 캐시 히트가 큰 요청이 임계값 아래로 오판돼 과소청구된다.

    청구와 감사가 어긋나지 않도록(청구=long/기록=short) 판정은 여기 한 곳에서만 한다.
    """
    threshold = pricing.pricing.long_context_threshold_tokens
    if threshold is None:
        return None
    prompt_total = (
        usage.input_tokens
        + usage.cache_read_input_tokens
        + usage.cache_creation_input_tokens
    )
    return "long" if prompt_total > threshold else "short"


def calculate_cost(usage: TokenUsage, pricing: ModelConfigSchema) -> Decimal:
    """비용 계산: input + output + cache_write + cache_read.

    cache_write 단가는 요청의 cache TTL에 따라 분기(5m/1h). long-context 티어(0038)에서는
    프롬프트가 임계를 넘으면 각 항이 long 요율로 재청구된다 — 판정은 resolve_context_tier
    한 곳에서만(감사기록과 동일 판정 공유). threshold 없는 모델은 None → 전부 short =
    오늘의 단일 요율 동작 그대로.

    quantize 는 마지막에 한 번만 — 항마다 반올림하면 오차가 누적된다.
    """
    p = pricing.pricing
    long_ctx = resolve_context_tier(usage, pricing) == "long"

    def _rate(short_rate: Decimal, long_rate: Decimal | None) -> Decimal:
        # long 단가가 None 이면 short 로 폴백(fail-safe): threshold 만 두고 특정 버킷 long
        # 단가를 비운 행에서도 크래시 없이 최소 short 로 과금한다.
        return long_rate if (long_ctx and long_rate is not None) else short_rate

    input_rate = _rate(p.input_per_1k, p.long_input_per_1k)
    output_rate = _rate(p.output_per_1k, p.long_output_per_1k)
    if usage.cache_ttl_1h:
        cache_write_rate = _rate(p.cache_write_1h_per_1k, p.long_cache_write_1h_per_1k)
    else:
        cache_write_rate = _rate(p.cache_write_per_1k, p.long_cache_write_per_1k)
    cache_read_rate = _rate(p.cache_read_per_1k, p.long_cache_read_per_1k)

    input_cost = (Decimal(usage.input_tokens) / 1000) * input_rate
    output_cost = (Decimal(usage.output_tokens) / 1000) * output_rate
    cache_write_cost = (Decimal(usage.cache_creation_input_tokens) / 1000) * cache_write_rate
    cache_read_cost = (Decimal(usage.cache_read_input_tokens) / 1000) * cache_read_rate
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
        # KI-08: usage 없는 disconnect 경로 — 예약을 **전부** 되돌린다.
        #
        # ⚠️ 오랫동안 여기서 TPM 만 해제했다. 비용(CPM/CPH) 예약은 그대로 남아, 응답을
        #    한 토큰도 받지 못한 요청이 사용자의 분/시간 비용 한도를 계속 물고 있었다.
        #    예약은 `max_tokens` 기준의 **과대** 추정이라, 큰 max_tokens 로 몇 번 끊기면
        #    실제 지출 $0 로도 자기 CPH 를 소진해 그 시간이 끝날 때까지 429 를 맞는다.
        #    TPM 만 돌려주면 두 한도 중 하나만 정상으로 보여 원인 추적이 더 어렵다.
        if usage.total_tokens == 0 and usage.input_tokens == 0 and usage.output_tokens == 0:
            if rate_limit_state and redis is not None:
                from app.services.rate_limit_service import RateLimitService

                svc = RateLimitService()
                try:
                    await svc.settle_tpm(
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
                # ⚠️ 비용 해제를 TPM 과 **분리된** try 로 둔다. 한 블록에 묶으면 TPM 쪽이
                #    터졌을 때 비용 해제가 실행되지 않아, 정확히 원래의 결함으로 되돌아간다.
                reserved_cost = rate_limit_state.get("cost_reserved")
                if reserved_cost is not None and reserved_cost != Decimal("0"):
                    try:
                        await svc.settle_cost(
                            redis,
                            user_id=str(auth_context.user_id),
                            actual_cost=Decimal("0"),
                            reserved_cost=reserved_cost,
                            team_id=str(auth_context.team_id) if auth_context.team_id else None,
                        )
                    except Exception:
                        logger.warning(
                            "cost_release_on_disconnect_failed",
                            user_id=auth_context.user_id,
                        )
            return Decimal("0")

        cost_usd = calculate_cost(usage, model_config)
        # KST 월 — 아래 budget:*:{period} 키를 **쓰는** 쪽이다. 읽는 쪽
        # (middleware/budget.py, routers/usage.py)과 반드시 같은 경계여야 한다.
        period = current_kst_period()

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
            from app.services.budget_service import PER_APP_BUDGET_CLIENTS
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

            # 앱(client) 예산 차감.
            #
            # ⚠️ 예전에는 이 차감이 ``result["app_clients"]`` 게이트 뒤에 있었다 — 즉 위
            #    user-layer EVAL 이 **에코해 준** 목록에 이 client 가 있어야만 차감했다.
            #    그 게이트는 검사 경로와 어긋나서 조용히 과소청구를 만들었다:
            #
            #      * ``budget:config:user:{uid}`` 는 ex=300 으로 쓰이고, 없을 때만 다시
            #        만들어진다. 60초짜리 스트리밍 응답이 그 키의 잔여 20초에 시작해
            #        만료 뒤에 finalize 하면, budget_deduct.lua 는 ``app_clients = {}`` 로
            #        시작하고 cjson 이 빈 Lua 테이블을 JSON **객체** ``{}`` 로 인코딩하므로
            #        ``isinstance(..., list)`` 가 False 가 되어 그 요청의 앱별 카운터가
            #        올라가지 않는다.
            #      * user-layer EVAL 이 예외를 내면 ``result`` 가 None 이라 게이트가 닫힌다 —
            #        Redis 딸꾹질 한 번이 앱 계층 차감까지 함께 떨어뜨린다.
            #
            #    반면 **검사** 경로는 다음 요청에서 DB 로부터 per-app 설정을 재수화해 $50
            #    앱 한도를 계속 집행한다. 앱별 카운터에는 TTL 이 없고 복원 경로는 키가
            #    없을 때만 도는데, 이 경우 키는 존재하므로 그 달 내내 어긋난 채 남는다 —
            #    한편 ``budget.budget_usages`` 에는 참값이 들어간다(워커는 게이트가 없다).
            #
            #    그래서 게이트를 없앤다. 대상 client 이면 항상 차감한다 — DB 기록자
            #    (cost-recorder-worker) 와 같은 규칙이다.
            #
            # ⚠️ 대가: per-app 예산이 없는 사용자에게도 ``budget:user:{uid}:{client}:{period}``
            #    키가 생긴다. TTL 이 없으므로 volatile-lru 에서는 축출되지 않는다
            #    (사용자·월당 최대 3개). 그리고 관리자가 달 중간에 per-app 예산을 만들면
            #    이미 누적된 카운터가 즉시 그 한도에 계산된다 — 의도된 동작이지만
            #    운영자에게 알려야 하는 변경이다.
            if client in PER_APP_BUDGET_CLIENTS:
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

        # 청구와 **같은 입력**으로 티어를 판정해 감사 기록에 싣는다(불일치 방지).
        context_tier = resolve_context_tier(usage, model_config)
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
            context_tier=context_tier,
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
