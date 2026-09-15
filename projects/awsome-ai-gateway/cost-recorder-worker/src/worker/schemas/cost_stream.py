# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""CostStreamEntry 스키마 — gateway-proxy `schemas/cost_stream.py` 의 거울본.

gateway가 XADD 하는 payload를 worker가 JSON으로 역직렬화 → Pydantic 검증 →
DB writer 로 넘기는 흐름. 양쪽 파일을 동기화 상태로 유지 (지금은 수동, 향후
단일 소스로 통합 가능).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field


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
    # Reasoning/thinking tokens — visibility submetric (already inside output_tokens).
    # NOT a billing input; persisted to usage_logs.reasoning_tokens for analytics/UI.
    reasoning_tokens: int = 0
    # Server-side web search calls for this request — attribution metric, NOT billing.
    # Persisted to usage_logs.web_search_count for per-client AgentCore search analytics.
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
    completed_at: str
    period: str  # YYYY-MM
    date: str  # YYYY-MM-DD

    # 구버전 호환 필드 — 넘은 것 중 가장 높은 USER 스코프 임계값. 새 소비자는
    # ``threshold_events`` 를 쓴다(스코프와 다중 교차를 표현할 수 있는 유일한 형태다).
    threshold_triggered: int | None = None
    threshold_policy: str | None = None
    # 이 요청이 넘은 모든 임계값(스코프별). 구버전 엔트리에는 없으므로 기본값은 빈 목록이다.
    threshold_events: list[ThresholdEvent] = Field(default_factory=list)

    sso_subject: str | None = None  # OIDC sub or stable user identifier
    bedrock_request_id: str | None = None
    # Identification tag "claude-code" | "cowork" | "codex" | "other". Was previously
    # dropped here (Pydantic ignored the extra field) -> NULL client logs; now carried
    # through so per-client analytics/dashboards see all three apps.
    client: str | None = None

    schema_version: int = Field(default=2)
