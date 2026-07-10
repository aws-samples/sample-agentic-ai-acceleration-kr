# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import structlog

from worker.schemas.recipients import RenderedEmail

logger = structlog.get_logger(__name__)


class MockEmailSender:
    """MVP 기본 이메일 센더 — 실제 발송 없이 structlog에 기록 (BR-EMAIL-01).

    개발/테스트 환경에서 EMAIL_SENDER_TYPE=mock 으로 사용.
    이메일 본문은 로그에 포함하지 않음 (BR-SEC-03).
    """

    async def send(self, email: RenderedEmail) -> None:
        # 수신자 이메일 마스킹: user@domain → user@*** (BR-SEC-03)
        masked = _mask_email(email.recipient.email)
        logger.info(
            "mock_email_sent",
            recipient=masked,
            subject=email.subject,
            role=email.recipient.role,
        )


def _mask_email(address: str) -> str:
    if "@" in address:
        local, domain = address.split("@", 1)
        return f"{local}@***"
    return "***"
