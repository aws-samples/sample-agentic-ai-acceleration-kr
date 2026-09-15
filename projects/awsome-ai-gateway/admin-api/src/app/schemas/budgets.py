# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from decimal import Decimal

from typing import Annotated

from pydantic import BaseModel, Field

from app.schemas.common import BudgetPolicy


# ── Requests ──


class SetBudgetRequest(BaseModel):
    max_budget_usd: Decimal = Field(ge=0, decimal_places=4)
    policy: BudgetPolicy = BudgetPolicy.HARD_BLOCK
    # ⚠️ 범위를 여기서도 강제한다. DB 는 ``budget.alert_pct`` 도메인(1..100)으로 막고
    #    admin-ui 는 zod 로 막는데, API 스키마만 비어 있었다 — 즉 UI 를 거치지 않는
    #    호출이 ``[150]`` 을 보내면 스키마를 통과해 DB 에서 터진다(422 여야 할 것이
    #    500 이 된다). 세 계층의 범위가 **같아야** 한다.
    #
    #    빈 목록은 허용한다 — "이 예산에는 알림을 보내지 않는다" 는 유효한 설정이고,
    #    막으면 알림을 끌 방법이 없다.
    alert_thresholds: list[Annotated[int, Field(ge=1, le=100)]] = Field(
        default=[80, 90, 100],
        description="Budget usage % thresholds for alert notifications (1-100; empty = no alerts)",
    )


class AllocateBudgetItem(BaseModel):
    user_id: str
    allocated_usd: Decimal = Field(ge=0, decimal_places=4)


class AllocateBudgetRequest(BaseModel):
    allocations: list[AllocateBudgetItem] = Field(min_length=1)


class SeedSpentItem(BaseModel):
    scope: str
    scope_id: str
    client: str | None = None
    period: str
    spent_usd: Decimal = Field(ge=0, decimal_places=4)


class SeedSpentRequest(BaseModel):
    items: list[SeedSpentItem] = Field(min_length=1)


class SeedSpentResult(BaseModel):
    scope: str
    scope_id: str
    client: str | None = None
    period: str
    status: str  # "ok" | "error"
    before_usd: Decimal | None = None
    after_usd: Decimal | None = None
    error: str | None = None


class SeedSpentResponse(BaseModel):
    total: int
    succeeded: int
    failed: int
    results: list[SeedSpentResult]


# ── Responses ──


class BudgetSummaryItem(BaseModel):
    target_type: str
    target_id: str
    target_name: str | None = None
    team_id: str | None = None
    is_active: bool = True
    limit_usd: Decimal | None  # None = 개인 예산 미설정 (팀 예산 적용)
    used_usd: Decimal
    remaining_usd: Decimal | None
    usage_pct: Decimal | None
    department_id: str | None = None
    department_name: str | None = None
    # 저장된 알림 임계값. ⚠️ 예전에는 쓰기 전용이었다 — UI 는 값을 보낼 수 있지만 되읽을
    # 수 없어서, 다이얼로그를 다시 열면 항상 [80,90,100] 이 보였다. 예산 미설정 대상은
    # None(설정 자체가 없다)이고 빈 목록은 "알림 없음" 이라는 유효한 설정이다.
    alert_thresholds: list[int] | None = None


class BudgetSummaryResponse(BaseModel):
    period: str
    summary: list[BudgetSummaryItem]


class AppBudgetItem(BaseModel):
    client: str
    max_budget_usd: Decimal
    policy: BudgetPolicy


class UserAppBudgetsResponse(BaseModel):
    user_id: str
    apps: list[AppBudgetItem]


# ── Team Allocation ──


class AllocationEntry(BaseModel):
    target_id: str
    target_name: str
    target_type: str  # TEAM | USER
    allocated_usd: Decimal
    used_usd: Decimal
    remaining_usd: Decimal
    alert_level: str  # NORMAL | WARNING | CRITICAL


class TeamBudgetAllocation(BaseModel):
    team_id: str
    team_name: str
    total_budget_usd: Decimal
    entries: list[AllocationEntry]


# ── Auto-Downgrade ──


class DowngradeRuleItem(BaseModel):
    from_model_alias: str
    to_model_alias: str
    threshold_pct: int = Field(ge=1, le=100)


class AutoDowngradeConfigRequest(BaseModel):
    enabled: bool = True
    rules: list[DowngradeRuleItem] = Field(min_length=1)


class DowngradeRuleResponse(BaseModel):
    id: str
    from_model_alias: str
    to_model_alias: str
    threshold_pct: int
    is_active: bool
    created_at: str


class AutoDowngradeConfigResponse(BaseModel):
    scope: str
    scope_id: str
    enabled: bool
    rules: list[DowngradeRuleResponse]
