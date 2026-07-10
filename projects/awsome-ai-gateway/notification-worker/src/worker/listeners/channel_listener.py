# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import structlog

from worker.handlers.base import EventHandler
from worker.observability.metrics import WorkerMetrics
from worker.redis_client import get_redis_client
from worker.schemas.events import parse_pubsub_message

logger = structlog.get_logger(__name__)


class ChannelListener:
    """Redis Pub/Sub 채널을 구독하고 수신된 이벤트를 EventHandler로 전달한다.

    TaskSupervisor가 ``run()``을 태스크로 실행한다. 처리 중 예외가 발생하면
    re-raise하여 Supervisor의 재시작 정책(RP-01)이 동작하도록 한다.
    """

    def __init__(
        self,
        channel: str,
        handler: EventHandler,
        metrics: WorkerMetrics | None = None,
    ) -> None:
        self.channel = channel
        self.handler = handler
        self.metrics = metrics

    async def run(self) -> None:
        """메인 구독 루프. 정상 종료 또는 예외 발생 시 finally에서 구독 해제."""
        redis = get_redis_client()
        pubsub = redis.pubsub()
        await pubsub.subscribe(self.channel)
        logger.info("channel_listener_subscribed", channel=self.channel)

        try:
            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue

                try:
                    # 1. 메시지 파싱
                    event = parse_pubsub_message(message["data"])
                    if event is None:
                        # parse 실패는 parse_pubsub_message가 이미 로깅함
                        if self.metrics:
                            self.metrics.errors_total.add(1, {"error_type": "parse"})
                        continue

                    # 2. 메트릭: 수신 카운트
                    if self.metrics:
                        self.metrics.events_received_total.add(
                            1, {"channel": self.channel, "event_type": event.type.value}
                        )

                    # 3. 핸들러 호출 — 예외는 잡지 않고 위로 전파 (Supervisor 재시작 RP-01)
                    await self.handler.handle(event)

                except Exception as exc:
                    # 처리 중 예외: 로그 후 re-raise → TaskSupervisor가 재시작
                    logger.error(
                        "event_processing_failed",
                        channel=self.channel,
                        error=str(exc),
                    )
                    if self.metrics:
                        self.metrics.errors_total.add(1, {"error_type": "processing"})
                    raise

        finally:
            await pubsub.unsubscribe(self.channel)
            await pubsub.aclose()
            logger.info("channel_listener_unsubscribed", channel=self.channel)
