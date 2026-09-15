# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from app.periods import current_kst_date, current_kst_period


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
    # 요청의 최종 처리 결과. usage.usage_status enum 과 라벨이 **일치해야 한다** —
    # 워커가 이 문자열을 그대로 `CAST(:status AS usage.usage_status)` 에 넣는다.
    #
    # ⚠️ Literal 로 묶는 이유: 잘못된 라벨은 INSERT 시점에 배치 전체를 깨뜨리고
    #    per-row 폴백으로 떨어진다. 파싱 시점에 걸리면 그 한 건만 경고와 함께
    #    버려지므로, 실패가 보이는 곳이 앞으로 당겨진다.
    #
    # 기본값이 SUCCESS 인 것은 구버전 엔트리(이 필드가 없는 메시지)가 스트림에
    # 남아 있을 수 있기 때문이다 — 그 엔트리들은 성공 경로에서만 발행됐다.
    status: Literal["SUCCESS", "ERROR", "TIMEOUT"] = "SUCCESS"
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

    threshold_triggered: int | None = None
    threshold_policy: str | None = None

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
        status: Literal["SUCCESS", "ERROR", "TIMEOUT"] = "SUCCESS",
        ttft_ms: int | None = None,
        is_streaming: bool,
        estimated_usage: bool,
        downgraded_from: str | None,
        availability_fallback_from: str | None = None,
        threshold_triggered: int | None = None,
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
            status=status,
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
            threshold_policy=threshold_policy,
            sso_subject=sso_subject,
            bedrock_request_id=bedrock_request_id,
            client=client,
        )
