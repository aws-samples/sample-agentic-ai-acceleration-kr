# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Router-level rate limit enforcement (pre-reserve/post-settle 배선).

라우터(bedrock.py / openai_compat.py)가 `model_config` 해결 직후 호출.
이 모듈이 다음을 수행:
    1. 요청 body에서 max_tokens/추정 input tokens 추출
    2. 예약 토큰 계산 (`estimate_reserved_tokens`)
    3. 3-스코프 RPM 체크
    4. 3-스코프 TPM 체크 (pre-reserve)
    5. 거부 시 429 JSONResponse 반환 / 통과 시 state에 settle용 정보 주입

Step 5에서 `_load_scope_limits()`를 `model.rate_limit_configs` DB 조회로 교체 예정.
현재는 FR-4.1 배선 검증용 기본값 사용.
"""

from __future__ import annotations

import json
from decimal import Decimal
from uuid import uuid4

import structlog
from fastapi.responses import JSONResponse

from app.observability.provider_metrics import record_rate_limit_hit
from app.schemas.domain import AuthContext, BudgetStatus, DegradationLevel, ModelConfigSchema
from app.services.cost_recorder import resolve_tier_for_prompt_total
from app.services.rate_limit_config_loader import (
    AllScopeLimits,
    ScopeLimits,
    load_all_scope_limits,
)
from app.services.rate_limit_scope import (
    ScopeDescriptor,
    build_scope_descriptors,
    estimate_reserved_tokens,
)
from app.services.rate_limit_service import RateLimitService

logger = structlog.get_logger(__name__)

# 모델별 기본 max_output 상한 (Anthropic spec 기준 대체값).
_DEFAULT_MAX_OUTPUT = 4096

# 간단 추정: 평균 bytes per token (Bedrock count_tokens 호출 비용 회피).
# tiktoken/Anthropic tokenizer 반영은 추후 정확도 튜닝 단계에서.
_BYTES_PER_TOKEN = 4


def _extract_max_output(body: dict) -> int:
    """요청 body에서 max output 토큰 추출 (Bedrock/OpenAI/Responses 공통 필드).

    max_output_tokens 는 OpenAI **Responses API**(Codex/GPT-5.x) 필드 — 빠지면
    Codex 요청이 _DEFAULT_MAX_OUTPUT(4096)로 과대 예약돼 거짓 TPM/CPM throttle 유발.
    """
    for key in ("max_tokens", "max_completion_tokens", "max_new_tokens", "max_output_tokens"):
        value = body.get(key)
        if isinstance(value, int) and value > 0:
            return value
    return _DEFAULT_MAX_OUTPUT


def _estimate_input_tokens(body: dict) -> int:
    """body 전체 문자열 길이 기반 보수적 추정.

    정확한 카운팅은 tokenizer 필요하나 reserve는 과대 예약이 허용되므로
    bytes/4 휴리스틱으로 시작. post-settle에서 차액 환불됨.
    """
    try:
        serialized = json.dumps(body, ensure_ascii=False)
    except Exception:
        return _DEFAULT_MAX_OUTPUT
    return max(1, len(serialized.encode("utf-8")) // _BYTES_PER_TOKEN)


#: 티어 판정에서 빼는 미디어 — AWS 가 해상도·길이 기반 토큰으로 세는 것만.
_TOKENIZED_BY_SHAPE = ("image/", "audio/")


def _is_shape_media(mime: object) -> bool:
    return isinstance(mime, str) and mime.lower().startswith(_TOKENIZED_BY_SHAPE)


def _base64_media_chars(node) -> int:
    """티어 판정에서 뺄 base64 이미지·오디오 페이로드의 문자 수 합(base64 는 ASCII = 바이트 수).

    ⚠️ 이미지·오디오만 뺀다. 문서(PDF 등)와 텍스트는 그 **내용**이 프롬프트 토큰으로 청구되므로
       빼면 short 예약·long 청구가 된다 — MIME 을 보지 않고 data URL·``bytes``·base64 dict 를
       전부 빼던 처음 구현에서 1MB PDF 가 33 티어 토큰이 됐다(리뷰 프로브로 실측).

    잡는 모양:
      · data URL ``data:image/*`` / ``data:audio/*`` — Responses ``input_image.image_url``,
        Chat ``image_url.url``
      · Chat ``input_audio.data`` (data URL 이 아닌 맨 base64)
      · Anthropic ``{"type": "base64", "media_type": "image/*"|"audio/*", "data": ...}``
    """
    if isinstance(node, str):
        if node.startswith("data:"):
            head, sep, payload = node.partition(";base64,")
            if sep and "," not in head and _is_shape_media(head[len("data:"):]):
                return len(payload)
        return 0
    if isinstance(node, list):
        return sum(_base64_media_chars(v) for v in node)
    if not isinstance(node, dict):
        return 0
    total = 0
    for key, value in node.items():
        if (
            key == "data"
            and isinstance(value, str)
            and node.get("type") == "base64"
            and _is_shape_media(node.get("media_type"))
        ):
            total += len(value)
        elif key == "input_audio" and isinstance(value, dict):
            audio = value.get("data")
            total += len(audio) if isinstance(audio, str) else 0
        else:
            total += _base64_media_chars(value)
    return total


def _tier_prompt_tokens(model_config: ModelConfigSchema, body: dict, estimated_input: int) -> int:
    """272K 티어 **판정**에 쓰는 프롬프트 크기 — 예약량(``estimated_input``)은 바꾸지 않는다.

    ⚠️ bytes/4 는 base64 이미지·오디오를 텍스트처럼 센다. AWS 는 그것을 해상도·길이 기반 수백~수천
       토큰으로 세므로, ~1.1MB data URL 하나가 판정을 long 으로 밀어 CPM/CPH 를 2배로 잡고 멀티모달
       요청에 거짓 429 를 낸다. 그래서 판정에서만 그 바이트를 뺀다(문서·텍스트는 빼지 않는다 —
       ``_base64_media_chars``). 이미지 토큰은 세지 않으므로 임계 근처의 텍스트+이미지 요청은 short
       로 볼 수 있다 — 예약은 추정이고 settle 이 차액을 맞춘다.

    body 를 훑는 비용은 bytes/4 가 이미 long 일 때만 낸다(빼면 작아질 뿐이라 short 는 그대로).
    """
    if resolve_tier_for_prompt_total(estimated_input, model_config) != "long":
        return estimated_input
    return max(1, estimated_input - _base64_media_chars(body) // _BYTES_PER_TOKEN)


def _apply_budget_throttle(
    limits: AllScopeLimits, budget_status: BudgetStatus | None
) -> AllScopeLimits:
    """Budget throttle 활성 시 USER RPM을 설정값에 따라 감경.

    예산 throttle은 **USER 스코프에만** 적용 — 팀/전체는 별도 독립 한도.
    USER RPM 미설정(unlimited)인 경우 throttle 무시 (감경 대상 없음).
    """
    if not (budget_status and budget_status.throttle_active):
        return limits
    if limits.user.rpm is None:
        return limits
    pct = budget_status.throttle_rpm_pct or 50
    limits.user = ScopeLimits(
        rpm=max(1, int(limits.user.rpm * pct / 100)),
        tpm=limits.user.tpm,
    )
    return limits


async def enforce_rate_limits(
    *,
    redis,
    auth_context: AuthContext,
    model_config: ModelConfigSchema,
    body: dict,
    state: dict,
    request_id: str,
    budget_status: BudgetStatus | None = None,
    metrics=None,
) -> JSONResponse | None:
    """라우터용 Pre-reserve RPM + TPM 체크 진입점.

    거부할 때는 이 요청이 그때까지 넣은 TPM·비용 예약을 되돌린 뒤 429 를 돌려준다(뒤 스코프의
    거부는 각 검사가, 비용 단계의 거부는 여기서 TPM 을). RPM 의 +1 은 그대로다.

    Returns:
        거부 시 ``JSONResponse(status_code=429)``, 통과 시 ``None``.
        통과한 경우 ``state`` 에 ``rate_limit_state`` 키로 settle 정보 주입:
            - ``tpm_descriptors``: list[ScopeDescriptor]
            - ``tpm_reserved``: int
            - ``tpm_reserved_keys``: 예약한 스코프마다 (cur, prev, window 키, 버킷 번호)
            - ``cost_reserved``: Decimal (예약한 키가 없으면 0)
            - ``cost_reserved_keys``: 예약한 스코프마다 (cpm 키, cph 키)
            - ``reservation_id``: 이 예약의 id(정산 표지 — 한 번만 정산)
    """
    if redis is None:
        # Redis 다운 — fail-open (NFR-2.4). Middleware의 in-memory fallback이 처리 중.
        return None

    model_alias = model_config.alias or model_config.provider_model_id

    # DB fallback 가용성 확인 (NFR-2.4 degradation 반영)
    dm = state.get("_degradation_manager")
    is_db_degraded = dm and dm.level in (
        DegradationLevel.DB_DEGRADED,
        DegradationLevel.BOTH_DEGRADED,
    )
    session_factory = state.get("_session_factory")

    # short-lived session: rate limit config 조회 후 즉시 반환.
    if not is_db_degraded and session_factory is not None:
        async with session_factory() as db:
            limits = await load_all_scope_limits(
                redis=redis,
                db=db,
                user_id=auth_context.user_id,
                team_id=auth_context.team_id,
                model_alias=model_alias,
            )
    else:
        limits = await load_all_scope_limits(
            redis=redis,
            db=None,
            user_id=auth_context.user_id,
            team_id=auth_context.team_id,
            model_alias=model_alias,
        )
    limits = _apply_budget_throttle(limits, budget_status)

    descriptors = build_scope_descriptors(
        user_id=auth_context.user_id,
        team_id=auth_context.team_id,
        model_alias=model_alias,
        user_rpm=limits.user.rpm,
        user_tpm=limits.user.tpm,
        team_rpm=limits.team.rpm,
        team_tpm=limits.team.tpm,
        global_rpm=limits.global_.rpm,
        global_tpm=limits.global_.tpm,
    )

    svc = RateLimitService()
    # 이 요청의 예약 id — TPM·비용 정산이 같은 예약을 한 번만 반영하는 표지의 이름이다
    # (settle_* 참조). 요청 id 가 아니라 enforce 호출마다 새로 만든다: 폴백 루프는 후보마다
    # enforce 를 다시 부르고, 같은 비용 창 키에 후보마다의 예약이 따로 들어간다.
    reservation_id = uuid4().hex

    # RPM 체크 (요청 수)
    rpm_result = await svc.check_multi_scope_rpm(
        redis, descriptors, request_id=request_id
    )
    if not rpm_result.allowed:
        return _build_429(rpm_result, metrics)

    # TPM 체크 (Pre-reserve)
    max_output = _extract_max_output(body)
    estimated_input = _estimate_input_tokens(body)
    reserved = estimate_reserved_tokens(
        estimated_input_tokens=estimated_input,
        max_output_tokens=max_output,
    )

    tpm_result = await svc.check_multi_scope_tpm(
        redis, descriptors, reserved_tokens=reserved, reservation_id=reservation_id
    )
    if not tpm_result.allowed:
        return _build_429(tpm_result, metrics)

    # CPM/CPH 체크 (Pre-reserve, FR-4.6 — USER+TEAM 2 스코프)
    estimated_cost = _estimate_cost(
        model_config,
        estimated_input,
        max_output,
        tier_prompt_tokens=_tier_prompt_tokens(model_config, body, estimated_input),
    )
    cost_result = await svc.reserve_cost(
        redis,
        user_id=auth_context.user_id,
        estimated_cost=estimated_cost,
        user_cpm_limit=limits.user.cpm,
        user_cph_limit=limits.user.cph,
        team_id=auth_context.team_id,
        team_cpm_limit=limits.team.cpm,
        team_cph_limit=limits.team.cph,
        reservation_id=reservation_id,
    )
    if not cost_result.allowed:
        # 이 요청은 거부된다 — 위에서 넣은 TPM 예약을 되돌린다. 거부된 요청은 정산 상태를 받지 않아
        # 다른 곳에서는 되돌릴 수 없고, 남기면 그 분(가중 추정으로 약 2분) 동안 과잉제한한다.
        # 비용 쪽 앞 스코프의 예약은 reserve_cost 가 이미 되돌렸다.
        await svc.settle_tpm(
            redis,
            [],
            reserved,
            0,
            reserved_keys=tpm_result.reserved_keys,
            reservation_id=reservation_id,
        )
        return _build_cost_429(cost_result, metrics)

    # 통과 — settle용 정보 주입
    state["rate_limit_state"] = {
        "tpm_descriptors": _only_tpm(descriptors),
        "tpm_reserved": reserved,
        # 실제로 예약한 TPM 스코프·버킷. TPM 정산은 이것만 조정한다(RateLimitService.settle_tpm).
        "tpm_reserved_keys": tpm_result.reserved_keys,
        "cost_reserved": cost_result.reserved_cost,
        # 실제로 예약한 스코프·창의 키. 비용 정산은 이 키만 조정한다(RateLimitService.settle_cost).
        "cost_reserved_keys": cost_result.reserved_keys,
        # 이 예약의 id — 정산·되돌리기는 이 id 의 표지로 한 번만 반영된다.
        "reservation_id": reservation_id,
    }
    return None


def _estimate_cost(
    model_config: ModelConfigSchema,
    estimated_input: int,
    max_output: int,
    *,
    tier_prompt_tokens: int | None = None,
) -> Decimal:
    """Pre-reserve용 보수적 비용 추정 (USD).

    ``input_per_1k × input + output_per_1k × max_output`` 로 *과대* 예약.
    post-settle에서 실제 비용과의 차액이 환불됨.
    Cache 단가는 Pre-reserve 단계에서 판정 불가하므로 제외.

    ⚠️ 272K long-context 티어(0038)를 반영한다. 빠뜨리면 이 함수의 존재 이유인 과대 예약
       불변식이 깨진다: GPT-5.6 은 프롬프트가 임계를 넘는 순간 입력 2배·출력 1.5배로 청구되는데
       short 단가로 예약하면 예약이 실제의 절반 수준이 된다. 예: gpt-5.6-terra 에 500K 프롬프트 +
       max_output 4,000 → 예약 $1.1528 vs 실제 $2.2396(출력 2,000). CPM/CPH 가 $2 남은 사용자가
       게이트를 통과한 뒤 한도를 넘긴다 — settle 이 사후에 차액을 맞추므로 원장은 정상으로 보인다.

       판정은 ``cost_recorder.resolve_tier_for_prompt_total`` 에 위임한다(청구와 같은 함수).
       ``estimated_input`` 은 body 전체 크기 기반이라 캐시 분해 전의 프롬프트 총량에 대응하고,
       티어 판정이 쓰는 양(input + cache_read + cache_write)이 바로 그것이다.
       ``tier_prompt_tokens`` 를 주면 판정에만 그 값을 쓴다(base64 미디어 제외 —
       ``_tier_prompt_tokens``). 예약 크기는 언제나 ``estimated_input`` 이다.
    """
    pricing = model_config.pricing
    tier_tokens = estimated_input if tier_prompt_tokens is None else tier_prompt_tokens
    long_ctx = resolve_tier_for_prompt_total(tier_tokens, model_config) == "long"

    def _rate(short_rate: Decimal, long_rate: Decimal | None) -> Decimal:
        # long 단가가 비면 short 로 폴백 — calculate_cost 의 `_rate` 와 같은 fail-safe.
        # 둘이 다르게 폴백하면 예약과 청구가 또 갈라진다.
        return long_rate if (long_ctx and long_rate is not None) else short_rate

    input_rate = _rate(pricing.input_per_1k, pricing.long_input_per_1k)
    output_rate = _rate(pricing.output_per_1k, pricing.long_output_per_1k)
    input_cost = (Decimal(estimated_input) / Decimal(1000)) * input_rate
    output_cost = (Decimal(max_output) / Decimal(1000)) * output_rate
    return (input_cost + output_cost).quantize(Decimal("0.000001"))


def _build_cost_429(result, metrics=None) -> JSONResponse:
    scope = result.scope or "USER"
    limit_type = result.limit_type or "cpm"
    # ⚠️ 429 를 내는 **모든** 경로에서 올려야 한다. 한 경로만 빠지면 그 한도 위반은
    #    지표에서 사라지고, "스로틀 없음" 으로 읽힌다(observability/provider_metrics.py
    #    record_rate_limit_hit 주석 참조).
    record_rate_limit_hit(metrics, scope=scope, limit_type=limit_type)
    code = f"{scope.lower()}_{limit_type}_exceeded"
    retry_after = str(result.retry_after or 60)
    limit_value = str(result.limit) if result.limit is not None else ""
    remaining_value = str(result.remaining) if result.remaining is not None else "0"

    return JSONResponse(
        status_code=429,
        content={
            "error": {
                "type": "rate_limit_error",
                "message": (
                    f"Cost rate limit exceeded at {scope} scope ({limit_type}). "
                    f"Please retry after {retry_after} seconds."
                ),
                "code": code,
                "scope": result.scope,
                "limit_type": result.limit_type,
                "retry_after": result.retry_after,
            }
        },
        headers={
            "Retry-After": retry_after,
            "X-RateLimit-Scope": scope,
            "X-RateLimit-Type": limit_type,
            "X-RateLimit-Remaining": remaining_value,
            "X-RateLimit-Limit": limit_value,
        },
    )


def _only_tpm(descriptors: list[ScopeDescriptor]) -> list[ScopeDescriptor]:
    """TPM 한도가 있는 스코프만 필터 (settle 대상 축소)."""
    return [d for d in descriptors if d.tpm_limit and d.tpm_limit > 0]


def _build_429(result, metrics=None) -> JSONResponse:
    scope = result.scope or "USER"
    limit_type = result.limit_type or "rpm"
    record_rate_limit_hit(metrics, scope=scope, limit_type=limit_type)
    code = f"{scope.lower()}_{limit_type}_exceeded"
    retry_after = str(result.retry_after or 60)

    return JSONResponse(
        status_code=429,
        content={
            "error": {
                "type": "rate_limit_error",
                "message": (
                    f"Rate limit exceeded at {scope} scope ({limit_type}). "
                    f"Please retry after {retry_after} seconds."
                ),
                "code": code,
                "scope": result.scope,
                "limit_type": result.limit_type,
                "retry_after": result.retry_after,
            }
        },
        headers={
            "Retry-After": retry_after,
            "X-RateLimit-Scope": scope,
            "X-RateLimit-Type": limit_type,
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Limit": str(result.limit),
            "X-RateLimit-Reset": str(result.window_reset),
        },
    )
