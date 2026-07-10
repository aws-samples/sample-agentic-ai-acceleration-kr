# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Redis Stream `cost:stream` XREADGROUP 소비자.

Flow:
  1. Startup: consumer group MKSTREAM 생성 (존재하면 skip)
  2. Loop:
     - Unacked backlog 먼저 소비 (id='0')
     - 완료되면 신규 메시지 (id='>') XREADGROUP BLOCK 5000ms COUNT batch_max_size
     - 배치 누적 → flush → XACK
  3. 타임아웃 시 현재 누적분만 flush (count < batch_max_size 이어도 OK).
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import structlog
from redis.exceptions import ResponseError

from worker.batch_flusher import BatchFlusher
from worker.config import Settings
from worker.schemas.cost_stream import CostStreamEntry

logger = structlog.get_logger(__name__)


class StreamConsumer:
    """cost:stream → BatchFlusher pipeline."""

    def __init__(
        self,
        redis: Any,
        flusher: BatchFlusher,
        settings: Settings,
        metrics: Any = None,
    ) -> None:
        self._redis = redis
        self._flusher = flusher
        self._stream = settings.cost_stream_key
        self._group = settings.cost_stream_group
        self._consumer = settings.cost_stream_consumer
        self._batch_max = settings.batch_max_size
        self._batch_interval = settings.batch_max_interval_sec
        self._block_ms = settings.xread_block_ms
        self._metrics = metrics

    async def ensure_group(self) -> None:
        """Consumer group MKSTREAM create. BUSYGROUP은 정상 상황."""
        try:
            await self._redis.xgroup_create(
                self._stream, self._group, id="0", mkstream=True
            )
            logger.info(
                "consumer_group_created", stream=self._stream, group=self._group
            )
        except ResponseError as e:
            if "BUSYGROUP" in str(e):
                logger.info(
                    "consumer_group_exists", stream=self._stream, group=self._group
                )
            else:
                raise

    async def run(self) -> None:
        """메인 소비 루프. TaskSupervisor가 예외 시 재시작."""
        await self.ensure_group()
        logger.info(
            "stream_consumer_started",
            stream=self._stream,
            group=self._group,
            consumer=self._consumer,
        )

        # 기동 시 unacked backlog 먼저 처리 (이전 인스턴스 크래시 복구)
        await self._drain_backlog()

        # 정상 소비 루프
        await self._consume_live()

    async def _drain_backlog(self) -> None:
        """이 consumer 이름으로 남아있는 unacked 메시지 재처리 (at-least-once)."""
        while True:
            msgs = await self._redis.xreadgroup(
                groupname=self._group,
                consumername=self._consumer,
                streams={self._stream: "0"},
                count=self._batch_max,
                block=0,  # no block for backlog
            )
            if not msgs:
                break

            entries, ids = self._decode(msgs)
            if not entries:
                # 파싱 실패 메시지들도 ACK하여 무한 루프 방지.
                if ids:
                    await self._redis.xack(self._stream, self._group, *ids)
                break

            await self._flusher.flush(entries)
            await self._redis.xack(self._stream, self._group, *ids)
            logger.info("backlog_batch_processed", count=len(ids))

    async def _consume_live(self) -> None:
        """신규 메시지 XREADGROUP(>) + batch 누적 + time/count 기준 flush."""
        while True:
            batch_entries: list[CostStreamEntry] = []
            batch_ids: list[str] = []
            deadline = time.monotonic() + self._batch_interval

            while len(batch_entries) < self._batch_max:
                remaining_ms = max(
                    100, int((deadline - time.monotonic()) * 1000)
                )
                if remaining_ms <= 0:
                    break

                msgs = await self._redis.xreadgroup(
                    groupname=self._group,
                    consumername=self._consumer,
                    streams={self._stream: ">"},
                    count=self._batch_max - len(batch_entries),
                    block=min(remaining_ms, self._block_ms),
                )
                if not msgs:
                    # timeout → 현재 배치 flush 이후 새 배치로.
                    break

                entries, ids = self._decode(msgs)
                batch_entries.extend(entries)
                batch_ids.extend(ids)

            if not batch_entries:
                # yield back to event loop — xreadgroup 호출이 이미 blocking이지만
                # 예외 없이 빈 결과 온 경우에도 tight loop 방지.
                await asyncio.sleep(0)
                continue

            await self._flusher.flush(batch_entries)
            await self._redis.xack(self._stream, self._group, *batch_ids)

    def _decode(
        self, raw_msgs: list[Any]
    ) -> tuple[list[CostStreamEntry], list[str]]:
        """XREADGROUP 응답 → (entries, ids). 파싱 실패 메시지는 ID만 반환하여 ACK."""
        entries: list[CostStreamEntry] = []
        ids: list[str] = []
        for _stream_name, records in raw_msgs:
            for msg_id, fields in records:
                raw_id = msg_id.decode() if isinstance(msg_id, bytes) else msg_id
                ids.append(raw_id)
                payload = fields.get(b"payload") or fields.get("payload")
                if payload is None:
                    logger.warning("cost_stream_entry_missing_payload", msg_id=raw_id)
                    continue
                try:
                    raw = (
                        payload.decode("utf-8") if isinstance(payload, bytes) else payload
                    )
                    data = json.loads(raw)
                    entries.append(CostStreamEntry(**data))
                except Exception as exc:
                    logger.warning(
                        "cost_stream_entry_parse_failed",
                        msg_id=raw_id,
                        error=str(exc),
                    )
        return entries, ids
