# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from typing import Protocol, runtime_checkable

from worker.schemas.recipients import RenderedEmail


class EmailSendError(Exception):
    """이메일 전송 오류.

    retryable=True:  네트워크 오류, 타임아웃, 5xx — 재시도 가능
    retryable=False: 잘못된 주소 (4xx), 인증 실패 — 영구 오류
    """

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


@runtime_checkable
class EmailSender(Protocol):
    """이메일 전송 인터페이스 (BR-EMAIL-01)."""

    async def send(self, email: RenderedEmail) -> None:
        """이메일을 전송한다.

        실패 시 EmailSendError를 raise한다.
        성공 시 None을 반환한다.
        """
        ...
