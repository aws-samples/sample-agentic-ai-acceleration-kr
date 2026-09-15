# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from app.periods import current_kst_date, current_kst_period


class ThresholdEvent(BaseModel):
    """예산 임계값 교차 한 건 — **어느 스코프**의 몇 %인가.

    ⚠️ 예전에는 ``CostStreamEntry.threshold_triggered`` 하나뿐이었고 그 값은 USER 스코프의
       것만 담겼다. 팀/앱 스코프의 교차는 Lua 가 계산해 돌려주는데 호출부가 반환값을 버려서
       **구조적으로 발송이 불가능**했다(워커의 알림 payload 도 ``target_type`` 을 "user" 로
       하드코딩했다). 팀 예산을 다 쓴 팀에게는 아무 알림도 가지 않았다.

       그리고 한 요청이 여러 임계값을 넘을 수 있으므로(70% → 105%) 스코프당 여러 건이
       나올 수 있다. 그래서 단일 값이 아니라 목록이다.
    """

    scope: Literal["user", "team", "client"]
    threshold_pct: int
    # 교차 시점의 누적 사용액과 한도.
    #
    # ⚠️ 워커의 알림 payload 는 예전에 이 자리에 **그 요청 하나의 비용**을 실었다
    #    (``current_used_usd=e.cost_usd``) — 메일이 "현재 $0.03 사용" 이라고 말하게 된다.
    #    누적값은 Lua 가 이미 알고 있으므로 여기서 함께 실어 보낸다.
    used_usd: Decimal
    limit_usd: Decimal
    # per-app 스코프일 때의 client 이름(claude-code / cowork / codex). 그 외에는 None.
    client: str | None = None


class CostStreamEntry(BaseModel):
    """Redis Stream `cost:stream`에 XADD 되는 단일 비용 레코드.

    gateway-proxy는 요청 완료 시점에 이 레코드를 XADD하고,
    cost-recorder-worker가 XREADGROUP으로 배치 소비 → DB INSERT/UPSERT.

    **Idempotency**: `request_id` UNIQUE 제약이 usage_logs에 있어 중복 소비되어도
    `ON CONFLICT DO NOTHING` 으로 dedup됨.
    """

    request_id: str
    user_id: str
    team_id: str
    dept_id: str
    model_alias: str
    provider: str

    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    # Reasoning/thinking tokens — visibility submetric (already inside output_tokens
    # for GPT-5.x; Anthropic extended-thinking lands here too). NOT a billing input.
    reasoning_tokens: int = 0
    # Server-side web search calls for this request — attribution metric, NOT billing.
    web_search_count: int = 0
    cost_usd: Decimal

    latency_ms: int
    # TTFT(time to first token) in ms. 스트리밍 첫 콘텐츠 델타까지의 시간.
    # 비스트리밍/미검출은 latency_ms와 동일 값. 구버전(schema_version 1) 엔트리는 부재 → None.
    ttft_ms: int | None = None
    is_streaming: bool = False
    estimated_usage: bool = False
    downgraded_from: str | None = None
    availability_fallback_from: str | None = None

    requested_at: str  # ISO format
    completed_at: str  # ISO format
    period: str  # YYYY-MM (for budget_usages)
    date: str  # YYYY-MM-DD (for daily counter + daily_aggregates)

    # 구버전 호환 필드 — 넘은 것 중 가장 높은 USER 스코프 임계값. 새 소비자는
    # ``threshold_events`` 를 쓴다(스코프와 다중 교차를 표현할 수 있는 유일한 형태다).
    threshold_triggered: int | None = None
    threshold_policy: str | None = None
    # 이 요청이 넘은 모든 임계값(스코프별). 구버전 엔트리에는 없으므로 기본값은 빈 목록이다.
    threshold_events: list[ThresholdEvent] = Field(default_factory=list)

    sso_subject: str | None = None  # OIDC sub or stable user identifier for Bedrock metadata
    bedrock_request_id: str | None = None
    client: str | None = None  # "claude-code" | "cowork" | "other" — identification tag

    schema_version: int = Field(default=2)

    @classmethod
    def make(
        cls,
        *,
        request_id: str,
        user_id: str,
        team_id: str,
        dept_id: str,
        model_alias: str,
        provider: str,
        input_tokens: int,
        output_tokens: int,
        cache_creation_tokens: int,
        cache_read_tokens: int,
        cost_usd: Decimal,
        reasoning_tokens: int = 0,
        web_search_count: int = 0,
        latency_ms: int,
        ttft_ms: int | None = None,
        is_streaming: bool,
        estimated_usage: bool,
        downgraded_from: str | None,
        availability_fallback_from: str | None = None,
        threshold_triggered: int | None = None,
        threshold_events: list | None = None,
        threshold_policy: str | None = None,
        sso_subject: str | None = None,
        bedrock_request_id: str | None = None,
        client: str | None = None,
    ) -> CostStreamEntry:
        now = datetime.now(tz=UTC)
        return cls(
            request_id=request_id,
            user_id=user_id,
            team_id=team_id,
            dept_id=dept_id,
            model_alias=model_alias,
            provider=provider,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_tokens=cache_creation_tokens,
            cache_read_tokens=cache_read_tokens,
            reasoning_tokens=reasoning_tokens,
            web_search_count=web_search_count,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            ttft_ms=ttft_ms,
            is_streaming=is_streaming,
            estimated_usage=estimated_usage,
            downgraded_from=downgraded_from,
            availability_fallback_from=availability_fallback_from,
            requested_at=now.isoformat(),
            completed_at=now.isoformat(),
            # ⚠️ period/date 는 UTC 가 아니라 **KST** 경계다(app.periods 참조).
            # 이 두 값이 cost-recorder-worker 에서 그대로 키가 된다:
            #   period → budget.budget_usages.period 행 + budget:*:{period} Redis 키
            #   date   → usage:daily:user:{uid}:{date} Redis 카운터
            # 집계 데이터는 전부 KST 로 버킷되고(daily_aggregator 는 AT TIME ZONE
            # 'Asia/Seoul') admin-api 는 KST 월로 읽으므로(§59), 여기서 UTC 로 쓰면
            # 매월/매일 경계에서 9시간 어긋난다. requested_at/completed_at 은 절대시각
            # (timestamptz)이므로 UTC 그대로가 맞다 — 버킷 라벨만 KST 다.
            period=current_kst_period(),
            date=current_kst_date(),
            threshold_triggered=threshold_triggered,
            threshold_events=threshold_events or [],
            threshold_policy=threshold_policy,
            sso_subject=sso_subject,
            bedrock_request_id=bedrock_request_id,
            client=client,
        )
