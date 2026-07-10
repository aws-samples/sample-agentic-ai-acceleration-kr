# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import structlog

from worker.config import Settings
from worker.senders.base import EmailSendError
from worker.schemas.recipients import RenderedEmail

logger = structlog.get_logger(__name__)


class SMTPEmailSender:
    """SMTP를 통한 이메일 전송 (Post-MVP, optional-deps: aiosmtplib)."""

    def __init__(self, settings: Settings) -> None:
        try:
            import aiosmtplib  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "aiosmtplib is required for SMTP sender. Install with: uv sync --extra smtp"
            ) from exc

        if not settings.smtp_host:
            raise ValueError("SMTP_HOST is required for smtp sender type")

        self._host = settings.smtp_host
        self._port = settings.smtp_port or 587
        self._sender_address = settings.email_sender_address
        self._sender_name = settings.email_sender_name

    async def send(self, email: RenderedEmail) -> None:
        import aiosmtplib
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText

        msg = MIMEMultipart("alternative")
        msg["Subject"] = email.subject
        msg["From"] = f"{self._sender_name} <{self._sender_address}>"
        msg["To"] = email.recipient.email
        msg.attach(MIMEText(email.html_body, "html", "utf-8"))

        try:
            await aiosmtplib.send(
                msg,
                hostname=self._host,
                port=self._port,
                use_tls=True,
            )
        except aiosmtplib.SMTPRecipientsRefused as exc:
            raise EmailSendError(str(exc), retryable=False) from exc
        except aiosmtplib.SMTPAuthenticationError as exc:
            raise EmailSendError(str(exc), retryable=False) from exc
        except Exception as exc:
            raise EmailSendError(str(exc), retryable=True) from exc
