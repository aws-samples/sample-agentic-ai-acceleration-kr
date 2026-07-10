# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import structlog

from worker.config import Settings
from worker.senders.base import EmailSender

logger = structlog.get_logger(__name__)


def create_email_sender(settings: Settings) -> EmailSender:
    """EMAIL_SENDER_TYPE 환경변수에 따라 적절한 EmailSender 구현체를 반환한다 (BR-EMAIL-01).

    지원 타입:
    - mock         (기본값) — 로그만 출력, 실제 발송 없음
    - ses          — AWS SES (optional-deps[ses] 필요)
    - smtp         — SMTP (optional-deps[smtp] 필요)
    - internal_api — 사내 메일 API (optional-deps[http] 필요)
    """
    sender_type = settings.email_sender_type.lower()
    logger.info("email_sender_created", type=sender_type)

    if sender_type == "mock":
        from worker.senders.mock_sender import MockEmailSender
        return MockEmailSender()

    if sender_type == "ses":
        from worker.senders.ses_sender import SESEmailSender
        return SESEmailSender(settings)

    if sender_type == "smtp":
        from worker.senders.smtp_sender import SMTPEmailSender
        return SMTPEmailSender(settings)

    if sender_type == "internal_api":
        from worker.senders.internal_api_sender import InternalAPIEmailSender
        return InternalAPIEmailSender(settings)

    raise ValueError(
        f"Unknown EMAIL_SENDER_TYPE: '{sender_type}'. "
        "Supported values: mock, ses, smtp, internal_api"
    )
