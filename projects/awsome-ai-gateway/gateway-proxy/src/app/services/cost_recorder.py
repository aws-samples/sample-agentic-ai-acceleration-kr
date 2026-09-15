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



def usage_status_for(http_status: int) -> str:
    """HTTP 상태 → ``usage.usage_status`` 라벨.

    enum 라벨은 ``('SUCCESS','ERROR','TIMEOUT')`` 셋뿐이다. 504/408/524 만 TIMEOUT 으로
    구분하고 나머지 실패는 ERROR 다 — 상류가 타임아웃과 거부를 다르게 다뤄야 하는 것은
    운영자이고(재시도 대상인지 아닌지), 그 구분이 라벨의 존재 이유다.
    """
    if http_status in (408, 504, 524):
        return "TIMEOUT"
    return "SUCCESS" if 200 <= http_status < 300 else "ERROR"


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

    async def record_failure(
        self,
        redis,
        auth_context: AuthContext,
        model_config: ModelConfigSchema,
        *,
        request_id: str,
        http_status: int,
        duration_ms: int,
        is_stream: bool = False,
        downgraded_from: str | None = None,
        availability_fallback_from: str | None = None,
        bedrock_request_id: str | None = None,
        client: str | None = None,
    ) -> None:
        """실패로 끝난 요청을 ``usage_logs`` 에 남긴다 — 토큰 0, 비용 0, status=ERROR/TIMEOUT.

        왜 필요한가
        -----------
        라우터의 실패 반환은 ``finalize`` 를 아예 부르지 않았다. 그래서 실패한 요청은
        ``usage_logs`` 에 **행이 없었고**, 워커는 남은 성공 행에 status 를 ``"SUCCESS"`` 로
        하드코딩해 넣었다. 두 사실이 겹쳐 admin-api 의 세 모니터링 엔드포인트가 계산하는
        ``error_rate_pct`` 는 분자도 0, 분모도 성공뿐이라 **구조적으로 항상 0.00%** 였다.

        그것이 단순한 결측보다 나쁜 이유: 화면은 비어 있지 않고 "0%" 를 녹색으로 칠해
        보여준다. 상류가 절반씩 5xx 를 뱉는 중에도 운영자의 건강 화면은 정상이라고
        적극적으로 주장한다.

        예약 해제는 여기서 하지 않는다
        ------------------------------
        RPM/TPM/비용 예약은 ``release_reservations`` 가 비-2xx 응답에서 이미 되돌린다.
        여기서 또 건드리면 이중 해제가 되어 사용자에게 남의 헤드룸을 준다. 이 메서드는
        **기록만** 한다 — 예산 차감도, settle 도, 메트릭도 없다.

        비용 0 을 쓰는 것도 의도다. 상류가 거부한 요청에는 청구할 것이 없고, 0 은
        ``budget_usages`` 의 가산 UPSERT 에서 값을 바꾸지 않는다.
        """
        if not request_id:
            return
        try:
            await self._publish_to_stream(
                redis,
                auth_context=auth_context,
                model_config=model_config,
                usage=TokenUsage(input_tokens=0, output_tokens=0),
                cost_usd=Decimal("0"),
                request_id=request_id,
                is_stream=is_stream,
                duration_ms=duration_ms,
                ttft_ms=None,
                threshold_triggered=None,
                status=usage_status_for(http_status),
                downgraded_from=downgraded_from,
                availability_fallback_from=availability_fallback_from,
                bedrock_request_id=bedrock_request_id,
                client=client,
            )
        except Exception:
            # 기록 실패가 이미 만들어진 오류 응답을 바꿀 이유는 없다.
            logger.exception("usage_failure_record_failed", request_id=request_id)

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
        status: str = "SUCCESS",
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
            status=status,  # type: ignore[arg-type]
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
