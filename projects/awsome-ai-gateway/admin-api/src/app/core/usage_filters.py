# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""비용 집계의 **정답 정의** — 단일 진실원 (DEVLOG §59).

대시보드·budget·analytics·my·chat 어디서 봐도 같은 숫자가 나오도록, "운영 비용
집계"의 기준을 한 곳에 못박는다:

  1. **SUCCESS 만 합산** — ERROR/TIMEOUT 호출은 비용에서 제외(유효 사용량 관점).
     (실측: 이번 달 ERROR 26건 $2.32 + TIMEOUT 17건 $1.18 이 실패 호출에도 비용으로
      쌓여 있어, status 필터 없으면 Top 사용자/팀이 부풀려졌었다.)
  2. **KST(Asia/Seoul) 월 경계** — 한국 운영 자산이므로 캘린더 경계는 KST 기준.
     timestamptz 에 to_char 를 그냥 쓰면 DB 세션 TZ(UTC)로 잘려 KST 6/1 0~9시
     호출이 5월로 새는 9시간 오차가 생긴다 → 명시적 KST 변환으로 강제.

⚠️ 이 필터는 **비용/사용량 표시용**에만 쓴다. 에러율·모니터링처럼 ERROR/TIMEOUT 을
세야 하는 쿼리에는 success_only=False 로 쓰거나 쓰지 않는다.
"""

from __future__ import annotations

from sqlalchemy import ColumnElement, and_, func

from app.models.usage import UsageLog, UsageStatus


def kst_month_expr() -> ColumnElement:
    """usage_logs.requested_at 을 KST 로 변환한 'YYYY-MM' 문자열 식.

    UI/호출부가 period(YYYY-MM)를 KST 기준으로 넘긴다는 전제 — 이 식과 == 비교.
    """
    return func.to_char(func.timezone("Asia/Seoul", UsageLog.requested_at), "YYYY-MM")


def cost_period_filter(period: str, *, success_only: bool = True) -> ColumnElement:
    """비용 집계 표준 WHERE — KST 월 경계 + (기본) SUCCESS 만.

    기존 `func.to_char(UsageLog.requested_at, "YYYY-MM") == period` (UTC 암묵 +
    status 무필터)을 대체. success_only=False 면 status 필터 생략(전체 호출).
    """
    conds: list[ColumnElement] = [kst_month_expr() == period]
    if success_only:
        conds.append(UsageLog.status == UsageStatus.SUCCESS)
    return and_(*conds)


def client_coalesce_expr() -> ColumnElement:
    """usage_logs.client with legacy NULL rows folded into 'other'.

    Use this in GROUP BY so pre-feature rows (client IS NULL) surface as 'other'
    instead of being dropped.
    """
    return func.coalesce(UsageLog.client, "other")


def client_filter(client: str | None) -> ColumnElement | None:
    """Optional WHERE predicate for a dashboard ?client= filter.

    Returns None for 'all'/None/'' (no filtering). For a specific client,
    matches COALESCE(client,'other') so 'other' also catches legacy NULL rows.
    Canonical values: claude-code | cowork | other.
    """
    if not client or client == "all":
        return None
    return client_coalesce_expr() == client
