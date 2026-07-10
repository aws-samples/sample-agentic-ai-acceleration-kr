# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import time
from abc import ABC
from datetime import datetime, timezone
from typing import Protocol

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from worker.models.notification import NotificationLog
from worker.observability.metrics import WorkerMetrics
from worker.schemas.events import NotificationEvent
from worker.schemas.recipients import RenderedEmail
from worker.senders.base import EmailSender
from worker.services.config_cache import ConfigCache
from worker.services.recipient_resolver import RecipientResolver
from worker.services.retry_executor import RetryExecutor
from worker.services.template_engine import TemplateEngine

logger = structlog.get_logger(__name__)


class EventHandler(Protocol):
    """이벤트 핸들러 인터페이스."""

    async def handle(self, event: NotificationEvent) -> None: ...


class BaseHandler(ABC):
    """이메일 알림 이벤트 처리 오케스트레이터 (Wave 4).

    config 조회 → 수신자 결정 → 템플릿 렌더링 → DB 로그 생성 →
    이메일 전송(재시도 포함) → 결과 기록 순서로 처리한다.

    DB 장애 시 이메일 전송은 계속 진행하며, 전송 결과는 최선(best-effort)으로 기록한다.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        config_cache: ConfigCache,
        recipient_resolver: RecipientResolver,
        template_engine: TemplateEngine,
        email_sender: EmailSender,
        retry_executor: RetryExecutor,
        metrics: WorkerMetrics | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._config_cache = config_cache
        self._recipient_resolver = recipient_resolver
        self._template_engine = template_engine
        self._email_sender = email_sender
        self._retry_executor = retry_executor
        self._metrics = metrics

    async def handle(self, event: NotificationEvent) -> None:
        """NotificationEvent 하나를 end-to-end로 처리한다."""
        start = time.monotonic()

        # 1. 설정 조회
        config = self._config_cache.get(event.type.value)
        if config is None:
            logger.warning("event_type_not_configured", event_type=event.type.value)
            return
        if not config.enabled:
            logger.info("notification_disabled", event_type=event.type.value)
            return

        # 2. 수신자 결정
        recipients = await self._recipient_resolver.resolve(
            roles=config.recipient_roles,
            payload=event.payload,
        )
        if not recipients:
            logger.warning("no_recipients_resolved", event_type=event.type.value)
            return

        # 3. 수신자별 처리
        for recipient in recipients:
            async with self._session_factory() as session:
                # 3a. 템플릿 컨텍스트 구성
                context = TemplateEngine.build_context(
                    event, recipient.name, recipient.email
                )

                # 3b. 렌더링
                subject, html_body = self._template_engine.render(
                    event.type.value, context
                )

                # 3c. NotificationLog 생성
                log = NotificationLog(
                    event_id=event.event_id,
                    event_type=event.type.value,
                    channel="email",
                    recipient_email=recipient.email,
                    recipient_user_id=recipient.user_id,
                    subject=subject,
                    status="pending",
                    attempt_count=0,
                    event_payload=event.payload,
                )

                # 3d. DB 저장 (실패해도 전송 계속)
                try:
                    session.add(log)
                    await session.commit()
                except Exception as exc:
                    await session.rollback()
                    logger.error(
                        "db_write_failed",
                        event_type=event.type.value,
                        recipient=recipient.email,
                        error=str(exc),
                    )
                    if self._metrics:
                        self._metrics.errors_total.add(1, {"error_type": "db"})

                # 3e. 렌더링 결과 래핑
                rendered = RenderedEmail(
                    subject=subject,
                    html_body=html_body,
                    recipient=recipient,
                )

                # 3f. 전송 (재시도 포함)
                try:
                    await self._retry_executor.execute(
                        fn=lambda r=rendered: self._email_sender.send(r),
                        event_type=event.type.value,
                    )
                    log.status = "sent"
                    log.resolved_at = datetime.now(timezone.utc)
                    if self._metrics:
                        self._metrics.emails_sent_total.add(
                            1, {"event_type": event.type.value, "status": "sent"}
                        )
                except Exception as exc:
                    log.status = "failed"
                    log.error_message = str(exc)
                    log.resolved_at = datetime.now(timezone.utc)
                    if self._metrics:
                        self._metrics.emails_sent_total.add(
                            1, {"event_type": event.type.value, "status": "failed"}
                        )
                    logger.error(
                        "email_send_failed",
                        event_type=event.type.value,
                        recipient=recipient.email,
                        error=str(exc),
                    )

                # 3g. 최종 상태 DB 반영
                log.last_attempt_at = datetime.now(timezone.utc)
                try:
                    await session.commit()
                except Exception as exc:
                    logger.error(
                        "db_write_failed",
                        event_type=event.type.value,
                        recipient=recipient.email,
                        error=str(exc),
                    )
                    if self._metrics:
                        self._metrics.errors_total.add(1, {"error_type": "db"})

        # 4. 처리 완료 메트릭
        if self._metrics:
            self._metrics.events_processed_total.add(
                1, {"event_type": event.type.value, "status": "success"}
            )
            duration = time.monotonic() - start
            self._metrics.event_processing_duration.record(
                duration, {"event_type": event.type.value}
            )
