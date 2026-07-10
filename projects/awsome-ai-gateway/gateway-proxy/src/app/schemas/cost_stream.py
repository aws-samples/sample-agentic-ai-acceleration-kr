# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel, Field


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
            ttft_ms=ttft_ms,
            is_streaming=is_streaming,
            estimated_usage=estimated_usage,
            downgraded_from=downgraded_from,
            availability_fallback_from=availability_fallback_from,
            requested_at=now.isoformat(),
            completed_at=now.isoformat(),
            period=now.strftime("%Y-%m"),
            date=now.strftime("%Y-%m-%d"),
            threshold_triggered=threshold_triggered,
            threshold_policy=threshold_policy,
            sso_subject=sso_subject,
            bedrock_request_id=bedrock_request_id,
            client=client,
        )
