# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field


# ── Query Params ──


class AnalyticsQueryParams(BaseModel):
    period: str = Field(description="YYYY-MM format")
    group_by: str = Field("model", description="model | team | department | user")
    scope: str = Field("all", description="all | team:{id}")
    start_date: str | None = Field(None, description="YYYY-MM-DD (KST). end_date 와 함께만 유효")
    end_date: str | None = Field(None, description="YYYY-MM-DD (KST). start_date 와 함께만 유효")


# ── Responses ──


class CostSummary(BaseModel):
    total_requests: int = 0
    total_tokens: int = 0
    total_cost_usd: Decimal = Decimal("0")
    active_users: int = 0
    avg_cost_per_user_usd: Decimal = Decimal("0")


class ModelBreakdown(BaseModel):
    model: str
    requests: int = 0
    cost_usd: Decimal = Decimal("0")


class TeamBreakdown(BaseModel):
    team: str
    team_id: str
    cost_usd: Decimal = Decimal("0")
    active_users: int = 0


class UserBreakdown(BaseModel):
    user: str  # display_name (PII=sso_subject 미노출)
    email: str
    cost_usd: Decimal = Decimal("0")
    requests: int = 0


class TrendItem(BaseModel):
    date: str
    cost_usd: Decimal = Decimal("0")
    requests: int = 0


class AnalyticsResponse(BaseModel):
    period: str
    # 실제로 집계한 KST 일자 범위. start_date/end_date 를 주지 않으면 None(= period 월 전체).
    # 응답에 싣는 이유: 클라이언트가 "서버가 내 범위를 정말 반영했는가" 를 확인할 수 있어야
    # 한다. 예전에는 UI 가 날짜를 보내도 라우트가 선언하지 않아 FastAPI 가 조용히 버렸고
    # (선언되지 않은 쿼리 파라미터는 422 없이 드롭된다), 5일을 고른 화면에 월 전체 숫자가 떴다.
    start_date: str | None = None
    end_date: str | None = None
    currency: str = "USD"
    cost_summary: CostSummary
    by_model: list[ModelBreakdown] = []
    by_team: list[TeamBreakdown] = []
    by_user: list[UserBreakdown] = []
    trends: list[TrendItem] = []


class UsageByUserModelItem(BaseModel):
    date: str
    user_id: str
    user_name: str | None = None
    # ⚠️ display_name 은 IdP 에서 비어 오거나 동명이인이 생긴다(auto-provisioning 은
    #    name claim 이 없으면 email 을 넣고, 그것도 없으면 <sub>@unknown 을 쓴다).
    #    운영자가 화면에서 사람을 특정할 축이 하나뿐이면 잘못된 사용자에게 예산 조치를
    #    하게 된다. ADMIN/TEAM_LEADER 전용 엔드포인트라 email 노출 범위는 넓어지지 않는다.
    user_email: str | None = None
    model_alias: str
    cost_usd: Decimal
    calls: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    department_id: str | None = None
    department_name: str | None = None
    team_id: str | None = None
    team_name: str | None = None
    avg_latency_ms: int


class UsageByUserModelResponse(BaseModel):
    period: str
    date: str
    items: list[UsageByUserModelItem]


class UsageByUserItem(BaseModel):
    date: str
    user_id: str
    user_name: str | None = None
    user_email: str | None = None
    #: 실사용 + 마이그레이션 주입분(`seeded_usd`)의 합. 화면에 뜨는 사용자 총액.
    cost_usd: Decimal
    calls: int
    #: 입력/출력 외 캐시 토큰. by-user-model 에는 이미 있었는데 사용자 합계에는 없어서,
    #: 모델 표를 접었을 때 캐시 사용량이 화면에서 사라졌다(합이 맞지 않아 보인다).
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    #: `budget_usages` 에 주입된 마이그레이션 이전 사용액(`POST /admin/budgets/seed-spent`).
    #:
    #: ⚠️ 이 값을 **따로 드러내는 것**이 중요하다. cost_usd 에만 접어 넣으면 운영자가
    #:    "이 사용자는 게이트웨이로 $13 밖에 안 썼는데 왜 $963 인가" 를 화면에서 해석할
    #:    수 없다(예산 페이지와 분석 페이지가 서로 다른 숫자를 보여주던 문제의 반대편).
    #:    seed 는 일자·모델 granularity 가 없어 모델 표(by-user-model)에는 넣지 않는다.
    seeded_usd: Decimal = Decimal("0")
    department_id: str | None = None
    department_name: str | None = None
    team_id: str | None = None
    team_name: str | None = None


class UsageByUserResponse(BaseModel):
    period: str
    date: str
    items: list[UsageByUserItem]
