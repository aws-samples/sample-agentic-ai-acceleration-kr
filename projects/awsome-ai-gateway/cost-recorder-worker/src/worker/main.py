# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Cost Recorder Worker 진입점.

초기화 순서:
  Settings → OTel → Redis → DB → Instrumentation → Metrics →
  BatchFlusher → StreamConsumer → APScheduler(daily_aggregator) → TaskSupervisor

종료 순서 (SIGTERM/SIGINT):
  Supervisor.stop_all(grace) → APScheduler.shutdown → Redis.aclose →
  DB.dispose → OTel.shutdown
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from worker.batch_flusher import BatchFlusher
from worker.config import get_settings
from worker.daily_aggregator import run_daily_aggregation, run_startup_backfill
from worker.db import create_db_engine, create_session_factory
from worker.observability import WorkerMetrics, init_otel, shutdown_otel
from worker.redis_client import create_redis_client
from worker.stream_consumer import StreamConsumer
from worker.worker import TaskSupervisor

logger = structlog.get_logger(__name__)


def _configure_logging(log_level: str, log_format: str) -> None:
    level = getattr(logging, log_level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
    ]
    final_processors = (
        [*shared_processors, structlog.processors.JSONRenderer()]
        if log_format == "json"
        else [*shared_processors, structlog.dev.ConsoleRenderer()]
    )
    structlog.configure(
        processors=final_processors,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


async def main() -> None:
    settings = get_settings()
    _configure_logging(settings.log_level, settings.log_format)

    logger.info("worker_starting", service=settings.otel_service_name)

    # 1. OTel
    init_otel(settings)

    # 2. Redis
    redis = await create_redis_client(settings)

    # 3. DB
    db_engine = create_db_engine(settings)
    session_factory = create_session_factory(db_engine)

    # 4. Auto-instrumentation (best-effort)
    try:
        from opentelemetry.instrumentation.redis import RedisInstrumentor
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        RedisInstrumentor().instrument()
        SQLAlchemyInstrumentor().instrument(engine=db_engine.sync_engine)
    except Exception:
        logger.warning("otel_instrumentation_unavailable")

    # 5. Metrics
    metrics = WorkerMetrics()

    # 6. BatchFlusher + StreamConsumer
    flusher = BatchFlusher(session_factory=session_factory, redis=redis, metrics=metrics)
    consumer = StreamConsumer(
        redis=redis, flusher=flusher, settings=settings, metrics=metrics
    )

    # 7. APScheduler — daily aggregator cron + startup backfill
    scheduler = AsyncIOScheduler(timezone="Asia/Seoul")
    scheduler.add_job(
        run_daily_aggregation,
        CronTrigger.from_crontab(settings.daily_usage_agg_cron, timezone="Asia/Seoul"),
        id="daily_usage_aggregation",
        args=[session_factory],
        replace_existing=True,
    )
    scheduler.start()
    # Backfill은 비차단으로 이벤트 루프에 스케줄 — startup 블로킹 금지.
    asyncio.create_task(run_startup_backfill(session_factory))
    logger.info(
        "daily_aggregator_scheduled",
        cron=settings.daily_usage_agg_cron,
        timezone="Asia/Seoul",
    )

    # 8. TaskSupervisor
    supervisor = TaskSupervisor()
    supervisor.register("stream_consumer", consumer.run)

    # 9. Signal handlers
    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, OSError):
            pass

    # 10. Start
    await supervisor.start_all()
    logger.info("worker_started")

    # 11. Wait for shutdown
    await stop_event.wait()
    logger.info("worker_shutting_down")

    # 12. Graceful shutdown — pending batch flush 는 supervisor cancel 시점에
    # asyncio.CancelledError 가 stream_consumer 의 await 지점에서 발생.
    # 현재 배치가 flush 진행 중이면 try/finally 없이도 SQLAlchemy commit 이
    # 원자적이므로 반 쓰다 만 상태는 없음.
    await supervisor.stop_all(grace_period=settings.shutdown_grace_period_sec)

    scheduler.shutdown(wait=False)
    await redis.aclose()
    await db_engine.dispose()
    await shutdown_otel()

    logger.info("worker_stopped")


if __name__ == "__main__":
    asyncio.run(main())
