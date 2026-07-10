# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import structlog

from worker.config import Settings
from worker.senders.base import EmailSendError
from worker.schemas.recipients import RenderedEmail

logger = structlog.get_logger(__name__)


class SESEmailSender:
    """AWS SES를 통한 이메일 전송 (Post-MVP, optional-deps: boto3).

    boto3는 optional-deps[ses]에 포함되어 있으며 런타임에 import한다.
    """

    def __init__(self, settings: Settings) -> None:
        try:
            import boto3
        except ImportError as exc:
            raise RuntimeError(
                "boto3 is required for SES sender. Install with: uv sync --extra ses"
            ) from exc

        self._client = boto3.client("ses", region_name=settings.aws_ses_region or "us-east-1")
        self._sender_address = settings.email_sender_address
        self._sender_name = settings.email_sender_name

    async def send(self, email: RenderedEmail) -> None:
        import asyncio

        source = f"{self._sender_name} <{self._sender_address}>"
        try:
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._client.send_email(
                    Source=source,
                    Destination={"ToAddresses": [email.recipient.email]},
                    Message={
                        "Subject": {"Data": email.subject, "Charset": "UTF-8"},
                        "Body": {"Html": {"Data": email.html_body, "Charset": "UTF-8"}},
                    },
                ),
            )
        except Exception as exc:
            error_code = getattr(getattr(exc, "response", {}).get("Error", {}), "get", lambda k: None)("Code")
            # 주소 오류 계열은 영구 실패
            permanent_codes = {"MessageRejected", "InvalidParameterValue", "MailFromDomainNotVerified"}
            retryable = error_code not in permanent_codes
            raise EmailSendError(str(exc), retryable=retryable) from exc
