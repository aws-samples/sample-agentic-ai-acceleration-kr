# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""ROI Aggregation + VK Expiry Scheduler — separate process from Admin API server.

Run: python -m app.scheduler.main

``daily_usage_aggregation`` 잡은 cost-recorder-worker 로 이관됨
이 scheduler는 ROI/key-expiry 만 담당.
"""
from __future__ import annotations

import asyncio

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import get_settings
from app.core.db import AsyncSessionLocal
from app.core.usage_filters import current_kst_period
from app.scheduler.key_expirer import expire_virtual_keys
from app.scheduler.roi_aggregator import aggregate_usage

logger = structlog.get_logger()


async def run_aggregation() -> None:
    # ⚠️ UTC 가 아니라 **KST** 월 — 이 잡이 ROI 행을 실제로 **쓰는** 곳이라 영향이 가장 크다.
    # 과거엔 datetime.now(timezone.utc).strftime("%Y-%m") 였다. 데이터는 KST 월로
    # 버킷되므로(§59) 매월 1일 KST 00:00~09:00 의 9시간 동안 이 잡은 지난달을 다시
    # 집계하고 **새 달 행은 아예 만들지 않는다**. 그동안 화면은 (current_kst_period 로
    # 고쳐진) 새 달을 조회하므로 roi_aggregations 에 행이 없어 전부 0 으로 보인다.
    # 즉 읽는 쪽만 KST 로 고치면 9시간짜리 "지표가 0" 창이 남는다 — 쓰는 쪽도 같아야 한다.
    period = current_kst_period()
    logger.info("scheduler.trigger", period=period)

    async with AsyncSessionLocal() as session:
        try:
            await aggregate_usage(session, period)
        except Exception:
            logger.exception("scheduler.aggregation_failed", period=period)


async def run_key_expiry() -> None:
    async with AsyncSessionLocal() as session:
        try:
            await expire_virtual_keys(session)
        except Exception:
            logger.exception("scheduler.key_expiry_failed")


def main() -> None:
    settings = get_settings()
    logger.info(
        "scheduler.starting",
        roi_cron=settings.ROI_AGGREGATION_CRON,
        key_expiry_cron=settings.KEY_EXPIRY_CRON,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    scheduler = AsyncIOScheduler(event_loop=loop)
    scheduler.add_job(
        run_aggregation,
        CronTrigger.from_crontab(settings.ROI_AGGREGATION_CRON),
        id="roi_aggregation",
        replace_existing=True,
    )
    scheduler.add_job(
        run_key_expiry,
        CronTrigger.from_crontab(settings.KEY_EXPIRY_CRON),
        id="key_expiry",
        replace_existing=True,
    )
    scheduler.start()

    try:
        loop.run_forever()
    except (KeyboardInterrupt, SystemExit):
        logger.info("scheduler.shutdown")
        scheduler.shutdown()


if __name__ == "__main__":
    main()
