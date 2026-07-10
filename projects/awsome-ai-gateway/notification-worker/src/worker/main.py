# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Notification Worker 진입점.

초기화 순서:
  Settings → OTel → Redis → DB → Instrumentation → Metrics →
  ConfigCache → NotificationBuffer → EmailSender → TemplateEngine →
  RecipientResolver/RetryExecutor → Handlers → TaskSupervisor → run

종료 순서 (SIGTERM/SIGINT):
  Supervisor.stop_all(30s) → Redis.aclose → DB.dispose → OTel.shutdown
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time

import structlog
from opentelemetry.metrics import Observation

from worker.config import get_settings
from worker.db import create_db_engine, create_session_factory, set_db_session_factory
from worker.handlers.budget_handler import BudgetHandler
from worker.handlers.key_handler import KeyHandler
from worker.handlers.security_handler import SecurityHandler
from worker.handlers.system_handler import SystemHandler
from worker.listeners.channel_listener import ChannelListener
from worker.listeners.config_reload_task import ConfigReloadTask
from worker.listeners.health_check_task import HealthCheckTask
from worker.observability import WorkerMetrics, init_otel, shutdown_otel
from worker.redis_client import create_redis_client, set_redis_client
from worker.senders.factory import create_email_sender
from worker.schemas.events import EventType, NotificationEvent
from worker.services.config_cache import get_config_cache, init_config_cache
from worker.services.notification_buffer import (
    get_notification_buffer,
    init_notification_buffer,
)
from worker.services.recipient_resolver import RecipientResolver
from worker.services.retry_executor import RetryExecutor
from worker.services.template_engine import get_template_engine, init_template_engine
from worker.worker import TaskSupervisor

# Pub/Sub 채널 → 처리할 EventType 목록 매핑
_CHANNEL_EVENT_TYPES: dict[str, list[EventType]] = {
    "notifications:budget": [EventType.BUDGET_THRESHOLD],
    "notifications:key": [
        EventType.KEY_EXPIRING,
        EventType.KEY_EXPIRED,
        EventType.KEY_REVOKED,
    ],
    "notifications:security": [
        EventType.AUTH_FAILURE_SPIKE,
        EventType.PERMISSION_VIOLATION,
        EventType.SUSPICIOUS_USAGE,
    ],
    "notifications:system": [
        EventType.DEGRADATION_MODE,
        EventType.PROVIDER_ERROR,
        EventType.SERVICE_HEALTH_CHANGE,
    ],
}

logger = structlog.get_logger(__name__)


def _configure_logging(log_level: str, log_format: str) -> None:
    """structlog과 stdlib logging을 설정한다."""
    level = getattr(logging, log_level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
    ]

    if log_format == "json":
        final_processors = [*shared_processors, structlog.processors.JSONRenderer()]
    else:
        final_processors = [*shared_processors, structlog.dev.ConsoleRenderer()]

    structlog.configure(
        processors=final_processors,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


async def main() -> None:  # noqa: PLR0915  (복잡도 예외: lifespan 특성상 길이 필요)
    settings = get_settings()
    _configure_logging(settings.log_level, settings.log_format)

    logger.info("worker_starting", service=settings.otel_service_name)

    # ── 1. OTel ──────────────────────────────────────────────────────────────
    init_otel(settings)

    # ── 2. Redis ─────────────────────────────────────────────────────────────
    redis = await create_redis_client(settings)
    set_redis_client(redis)

    # ── 3. DB ─────────────────────────────────────────────────────────────────
    db_engine = create_db_engine(settings)
    session_factory = create_session_factory(db_engine)
    set_db_session_factory(session_factory)

    # ── 4. Auto-instrumentation ───────────────────────────────────────────────
    try:
        from opentelemetry.instrumentation.redis import RedisInstrumentor
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        RedisInstrumentor().instrument()
        SQLAlchemyInstrumentor().instrument(engine=db_engine.sync_engine)
    except Exception:
        logger.warning("otel_instrumentation_unavailable")

    # ── 5. WorkerMetrics ─────────────────────────────────────────────────────
    worker_metrics = WorkerMetrics()
    start_time = time.monotonic()

    # Observable gauge: 버퍼 크기 / 업타임 (초기화 후 콜백 등록)
    worker_metrics.register_db_buffer_callback(
        lambda _opts: [Observation(get_notification_buffer().size)]
    )
    worker_metrics.register_uptime_callback(
        lambda _opts: [Observation(time.monotonic() - start_time)]
    )

    # ── 6. ConfigCache ────────────────────────────────────────────────────────
    await init_config_cache(session_factory)
    config_cache = get_config_cache()

    # ── 7. NotificationBufferQueue ────────────────────────────────────────────
    init_notification_buffer(settings.notification_buffer_max)
    notification_buffer = get_notification_buffer()
    notification_buffer.set_metrics(worker_metrics.errors_total)

    # ── 8. EmailSender ────────────────────────────────────────────────────────
    email_sender = create_email_sender(settings)

    # ── 9. TemplateEngine ─────────────────────────────────────────────────────
    init_template_engine()
    template_engine = get_template_engine()

    # ── 10. RecipientResolver / RetryExecutor ─────────────────────────────────
    recipient_resolver = RecipientResolver(session_factory)
    retry_executor = RetryExecutor(retry_counter=worker_metrics.retry_total)

    # ── 11. EventHandler 4종 ─────────────────────────────────────────────────
    handler_kwargs = {
        "session_factory": session_factory,
        "config_cache": config_cache,
        "recipient_resolver": recipient_resolver,
        "template_engine": template_engine,
        "email_sender": email_sender,
        "retry_executor": retry_executor,
        "metrics": worker_metrics,
    }
    budget_handler = BudgetHandler(**handler_kwargs)
    key_handler = KeyHandler(**handler_kwargs)
    security_handler = SecurityHandler(**handler_kwargs)
    system_handler = SystemHandler(**handler_kwargs)

    channel_handler_map = {
        "notifications:budget": budget_handler,
        "notifications:key": key_handler,
        "notifications:security": security_handler,
        "notifications:system": system_handler,
    }

    # EventType → handler 라우팅 테이블 (버퍼 드레인용)
    event_type_handler_map: dict[str, object] = {}
    for channel, event_types in _CHANNEL_EVENT_TYPES.items():
        h = channel_handler_map[channel]
        for et in event_types:
            event_type_handler_map[et.value] = h

    async def process_buffered_event(event: NotificationEvent) -> None:
        h = event_type_handler_map.get(event.type.value)
        if h is None:
            logger.warning("no_handler_for_buffered_event", event_type=event.type.value)
            return
        await h.handle(event)  # type: ignore[union-attr]

    # ── 12. HealthCheckTask ───────────────────────────────────────────────────
    health_check_task = HealthCheckTask(
        session_factory=session_factory,
        redis_client=redis,
        config_cache=config_cache,
        notification_buffer=notification_buffer,
        process_buffered_event=process_buffered_event,
        check_interval=settings.health_check_interval,
        metrics=worker_metrics,
    )

    # ── 13. TaskSupervisor 조립 ───────────────────────────────────────────────
    supervisor = TaskSupervisor()

    for channel, handler in channel_handler_map.items():
        listener = ChannelListener(channel, handler, metrics=worker_metrics)
        supervisor.register(f"listener:{channel}", listener.run)

    config_reload_task = ConfigReloadTask()
    supervisor.register("config_reload", config_reload_task.run)
    supervisor.register("health_check", health_check_task.run)

    # ── 14. Signal 핸들러 ─────────────────────────────────────────────────────
    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, OSError):
            # Windows에서 일부 신호 미지원
            pass

    # ── 15. 시작 ──────────────────────────────────────────────────────────────
    await supervisor.start_all()
    logger.info("worker_started")

    # ── 16. 종료 신호 대기 ────────────────────────────────────────────────────
    await stop_event.wait()
    logger.info("worker_shutting_down")

    # ── 17. Graceful shutdown ─────────────────────────────────────────────────
    await supervisor.stop_all(grace_period=30)

    remaining = notification_buffer.size
    if remaining > 0:
        logger.warning("buffer_events_unprocessed_on_shutdown", count=remaining)

    await redis.aclose()
    await db_engine.dispose()
    await shutdown_otel()

    logger.info("worker_stopped")


if __name__ == "__main__":
    asyncio.run(main())
