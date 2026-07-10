# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Daily Usage Aggregator — usage_logs → usage.daily_aggregates 일일 roll-up.

admin-api/scheduler/daily_usage_aggregator.py 로부터 이관 (2026-04-21).
이관 이유: cost-recorder-worker 가 usage_logs 쓰기를 소유하므로, 집계 읽기도
같은 프로세스가 담당하여 usage_logs 관련 작업을 단일 서비스에 집중.

Granularity: (date KST, user_id, model_alias) per row.
Idempotent: ON CONFLICT DO NOTHING — cron 재실행/중복 run 안전.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = structlog.get_logger(__name__)


_KST_OFFSET_HOURS = 9


def _yesterday_kst_window() -> tuple[datetime, datetime]:
    now_utc = datetime.now(timezone.utc)
    now_kst = now_utc + timedelta(hours=_KST_OFFSET_HOURS)
    yesterday_kst_date = (now_kst - timedelta(days=1)).date()
    start_utc = datetime.combine(
        yesterday_kst_date, datetime.min.time(), tzinfo=timezone.utc
    ) - timedelta(hours=_KST_OFFSET_HOURS)
    end_utc = start_utc + timedelta(days=1)
    return start_utc, end_utc


_AGG_SQL = """
INSERT INTO usage.daily_aggregates
  (date, user_id, team_id, dept_id, model_alias,
   input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
   total_tokens, total_cost_usd, request_count)
SELECT
  DATE((requested_at AT TIME ZONE 'Asia/Seoul')::timestamp) AS date,
  user_id, team_id, dept_id, model_alias,
  SUM(input_tokens),
  SUM(output_tokens),
  SUM(cache_creation_tokens),
  SUM(cache_read_tokens),
  SUM(input_tokens + output_tokens + cache_creation_tokens + cache_read_tokens),
  SUM(cost_usd),
  COUNT(*)
FROM usage.usage_logs
WHERE requested_at >= :start AND requested_at < :end
GROUP BY
  DATE((requested_at AT TIME ZONE 'Asia/Seoul')::timestamp),
  user_id, team_id, dept_id, model_alias
ON CONFLICT (date, user_id, model_alias) DO NOTHING;
"""


_BACKFILL_SQL = """
INSERT INTO usage.daily_aggregates
  (date, user_id, team_id, dept_id, model_alias,
   input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
   total_tokens, total_cost_usd, request_count)
SELECT
  DATE((requested_at AT TIME ZONE 'Asia/Seoul')::timestamp) AS date,
  user_id, team_id, dept_id, model_alias,
  SUM(input_tokens),
  SUM(output_tokens),
  SUM(cache_creation_tokens),
  SUM(cache_read_tokens),
  SUM(input_tokens + output_tokens + cache_creation_tokens + cache_read_tokens),
  SUM(cost_usd),
  COUNT(*)
FROM usage.usage_logs
WHERE (requested_at AT TIME ZONE 'Asia/Seoul')::date < :today_kst
GROUP BY
  DATE((requested_at AT TIME ZONE 'Asia/Seoul')::timestamp),
  user_id, team_id, dept_id, model_alias
ON CONFLICT (date, user_id, model_alias) DO NOTHING;
"""


async def _is_empty(session: AsyncSession) -> bool:
    result = await session.execute(text("SELECT 1 FROM usage.daily_aggregates LIMIT 1"))
    return result.scalar_one_or_none() is None


async def aggregate_yesterday(session: AsyncSession) -> int:
    start, end = _yesterday_kst_window()
    result = await session.execute(text(_AGG_SQL), {"start": start, "end": end})
    count: int = result.rowcount
    await session.commit()
    logger.info(
        "daily_aggregator.ran",
        start=start.isoformat(),
        end=end.isoformat(),
        inserted=count,
    )
    return count


async def backfill_if_empty(session: AsyncSession) -> int:
    """첫 기동 시 daily_aggregates 비어있으면 전체 과거 집계. 이미 값 있으면 -1."""
    if not await _is_empty(session):
        return -1
    now_kst_date: date = (
        datetime.now(timezone.utc) + timedelta(hours=_KST_OFFSET_HOURS)
    ).date()
    result = await session.execute(
        text(_BACKFILL_SQL), {"today_kst": now_kst_date}
    )
    count: int = result.rowcount
    await session.commit()
    logger.info(
        "daily_aggregator.backfilled",
        inserted=count,
        cutoff=now_kst_date.isoformat(),
    )
    return count


async def run_daily_aggregation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """APScheduler가 매일 호출하는 엔트리포인트."""
    try:
        async with session_factory() as session:
            await aggregate_yesterday(session)
    except Exception:
        logger.exception("daily_aggregator.failed")


async def run_startup_backfill(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """기동 시 1회 backfill. 이미 데이터 있으면 no-op."""
    try:
        async with session_factory() as session:
            count = await backfill_if_empty(session)
            if count >= 0:
                logger.info("daily_aggregator.startup_backfill_done", count=count)
            else:
                logger.info("daily_aggregator.startup_backfill_skipped")
    except Exception:
        logger.exception("daily_aggregator.startup_backfill_failed")
