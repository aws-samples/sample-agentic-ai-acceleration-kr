# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""ROI Aggregation + VK Expiry Scheduler — separate process from Admin API server.

Run: python -m app.scheduler.main

``daily_usage_aggregation`` 잡은 cost-recorder-worker 로 이관됨
이 scheduler는 ROI/key-expiry 만 담당.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import get_settings
from app.core.db import AsyncSessionLocal
from app.scheduler.key_expirer import expire_virtual_keys
from app.scheduler.roi_aggregator import aggregate_usage

logger = structlog.get_logger()


async def run_aggregation() -> None:
    period = datetime.now(timezone.utc).strftime("%Y-%m")
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
