# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

import structlog

logger = structlog.get_logger(__name__)

T = TypeVar("T")

# BR-RETRY-01: 최대 3회, 지수 백오프 1s → 2s → 4s
_MAX_ATTEMPTS = 3
_BACKOFF_DELAYS = (1, 2, 4)  # 초


class RetryExecutor:
    """이메일 전송 호출에 대한 동기적(await) 재시도 실행기 (BR-RETRY-01).

    U1 RetryWorker와의 차이:
    - U1은 백그라운드 큐에 넣고 나중에 재시도 (fire-and-forget)
    - U3는 호출 지점에서 await하며 결과를 즉시 확인 (핸들러가 NotificationLog를 정확히 업데이트하기 위해)

    retryable 구분 (BR-RETRY-01):
    - 재시도 대상: 네트워크 오류, 타임아웃, 5xx
    - 재시도 제외: EmailSendError(retryable=False) — 잘못된 주소, 인증 실패 (영구 오류)
    """

    def __init__(self, retry_counter=None) -> None:
        self._retry_counter = retry_counter  # WorkerMetrics.retry_total

    async def execute(
        self,
        fn: Callable[[], Awaitable[T]],
        event_type: str = "unknown",
    ) -> T:
        """fn을 최대 3회 시도하고 최종 결과를 반환한다.

        모든 시도가 실패하면 마지막 예외를 re-raise한다.
        """
        from worker.senders.base import EmailSendError

        last_exc: BaseException | None = None

        for attempt in range(_MAX_ATTEMPTS):
            try:
                result = await fn()
                if attempt > 0:
                    logger.info("retry_succeeded", attempt=attempt + 1, event_type=event_type)
                return result

            except EmailSendError as exc:
                if not exc.retryable:
                    logger.warning(
                        "email_send_permanent_failure",
                        attempt=attempt + 1,
                        event_type=event_type,
                        error=str(exc),
                    )
                    raise

                last_exc = exc
                logger.warning(
                    "email_send_retry",
                    attempt=attempt + 1,
                    max_attempts=_MAX_ATTEMPTS,
                    event_type=event_type,
                    error=str(exc),
                )

            except Exception as exc:
                # 그 외 예외는 모두 retryable로 간주
                last_exc = exc
                logger.warning(
                    "email_send_retry",
                    attempt=attempt + 1,
                    max_attempts=_MAX_ATTEMPTS,
                    event_type=event_type,
                    error=str(exc),
                )

            if self._retry_counter is not None:
                self._retry_counter.add(1, {"event_type": event_type})

            if attempt < _MAX_ATTEMPTS - 1:
                await asyncio.sleep(_BACKOFF_DELAYS[attempt])

        assert last_exc is not None
        raise last_exc
