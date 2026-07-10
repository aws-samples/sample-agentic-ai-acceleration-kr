# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""배치된 CostStreamEntry 를 DB + Redis에 기록하는 writer.

단일 flush() 호출 단위:
  1. usage.usage_logs bulk INSERT (ON CONFLICT (request_id) DO NOTHING — dedup)
  2. budget.budget_usages per-scope UPSERT (user + team 합산 누적)
  3. usage:daily:* Redis 카운터 pipeline INCRBY
  4. threshold_triggered 레코드는 notifications:budget 채널에 PUBLISH

모두 성공적으로 커밋된 후 호출자가 XACK 하여 at-least-once 보장. 실패 시
worker는 예외를 상위로 전파 → Supervisor가 백오프 재시작 → XREADGROUP이
unacked 메시지(``>`` 대신 ``0`` 스트림 id로)로 복구.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from worker.schemas.cost_stream import CostStreamEntry

logger = structlog.get_logger(__name__)


_INSERT_USAGE_LOGS = text(
    """
    INSERT INTO usage.usage_logs (
        id, request_id, user_id, team_id, dept_id, model_alias, provider,
        input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
        reasoning_tokens, web_search_count,
        cost_usd, latency_ms, ttft_ms, status, requested_at, completed_at,
        is_streaming, estimated_usage, downgraded_from, availability_fallback_from,
        sso_subject, bedrock_request_id, client
    ) VALUES (
        gen_random_uuid(),
        :request_id,
        CAST(:user_id AS uuid),
        CAST(:team_id AS uuid),
        CAST(:dept_id AS uuid),
        :model_alias,
        :provider,
        :input_tokens,
        :output_tokens,
        :cache_creation_tokens,
        :cache_read_tokens,
        :reasoning_tokens,
        :web_search_count,
        :cost_usd,
        :latency_ms,
        :ttft_ms,
        CAST(:status AS usage.usage_status),
        CAST(:requested_at AS timestamptz),
        CAST(:completed_at AS timestamptz),
        :is_streaming,
        :estimated_usage,
        :downgraded_from,
        :availability_fallback_from,
        :sso_subject,
        :bedrock_request_id,
        :client
    )
    ON CONFLICT (request_id) DO NOTHING
    """
)


_UPSERT_BUDGET_USAGE = text(
    """
    INSERT INTO budget.budget_usages
        (id, scope, scope_id, period, client, used_usd, limit_usd, last_updated)
    VALUES (
        gen_random_uuid(),
        CAST(:scope AS budget.budget_scope),
        CAST(:scope_id AS uuid),
        :period,
        NULL,
        :cost,
        COALESCE((
            SELECT max_budget_usd
            FROM budget.budget_configs
            WHERE scope = CAST(:scope AS budget.budget_scope)
              AND scope_id = CAST(:scope_id AS uuid)
              AND is_active = true
            ORDER BY effective_from DESC
            LIMIT 1
        ), 0),
        now()
    )
    -- Conflict target MUST match the unique index from migration 0011:
    -- (scope, scope_id, period, COALESCE(client,'')). The pre-0011 3-col target
    -- no longer matches any index → ON CONFLICT would raise on migrated DBs.
    -- This worker writes only the client=NULL total rows (COALESCE -> '').
    ON CONFLICT (scope, scope_id, period, COALESCE(client, ''))
    DO UPDATE SET used_usd = budget.budget_usages.used_usd + EXCLUDED.used_usd,
                  last_updated = now()
    """
)


class BatchFlusher:
    """Redis Stream에서 가져온 entries를 DB/Redis에 반영."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        redis: Any,
        metrics: Any = None,
    ) -> None:
        self._session_factory = session_factory
        self._redis = redis
        self._metrics = metrics

    async def flush(self, entries: list[CostStreamEntry]) -> None:
        """배치 entry를 DB + Redis에 반영. 성공 시 None, 실패 시 예외.

        FK 위반 (사용자/팀이 gateway 호출 후 삭제된 경우) 은 batch 를 per-row
        재시도해서 문제 row만 스킵하고 ACK. 상위 호출자는 예외 없이 반환 받아
        XACK 진행 가능 — bad row 때문에 전체 배치 crash-loop 방지.
        """
        if not entries:
            return

        # 1. DB 쓰기 — 단일 트랜잭션. FK 위반 시 per-row fallback.
        try:
            async with self._session_factory() as session:
                await self._insert_usage_logs(session, entries)
                await self._upsert_budget_usages(session, entries)
                await session.commit()
        except IntegrityError as ie:
            logger.warning(
                "batch_integrity_error_fallback_per_row",
                batch_size=len(entries),
                error=str(ie)[:200],
            )
            await self._flush_per_row(entries)

        # 2. Redis 당일 카운터 (best-effort, 실패해도 DB는 이미 커밋됨)
        try:
            await self._bump_daily_counters(entries)
        except Exception:
            logger.exception("daily_counter_update_failed", batch_size=len(entries))

        # 3. Threshold 알림 발행
        await self._publish_thresholds(entries)

        if self._metrics:
            self._metrics.entries_flushed.add(
                len(entries), {"worker": "cost-recorder"}
            )

        logger.info(
            "batch_flushed",
            count=len(entries),
            threshold_events=sum(1 for e in entries if e.threshold_triggered),
        )

    async def _flush_per_row(self, entries: list[CostStreamEntry]) -> None:
        """Per-row fallback: FK 위반/기타 integrity 문제가 있는 row만 스킵.

        각 entry 마다 개별 트랜잭션으로 INSERT + UPSERT. 실패는 warn만 하고 스킵
        (Stream ACK는 호출자가 진행하므로 해당 entry는 drop).
        """
        skipped = 0
        for e in entries:
            try:
                async with self._session_factory() as session:
                    await self._insert_usage_logs(session, [e])
                    await self._upsert_budget_usages(session, [e])
                    await session.commit()
            except IntegrityError as ie:
                skipped += 1
                logger.warning(
                    "row_skipped_integrity_error",
                    request_id=e.request_id,
                    user_id=e.user_id,
                    reason=str(ie)[:120],
                )
            except Exception:
                skipped += 1
                logger.exception(
                    "row_skipped_unexpected_error", request_id=e.request_id
                )
        if skipped:
            logger.info(
                "per_row_flush_done",
                total=len(entries),
                skipped=skipped,
                written=len(entries) - skipped,
            )

    async def _insert_usage_logs(
        self, session: AsyncSession, entries: list[CostStreamEntry]
    ) -> None:
        params = [
            {
                "request_id": e.request_id,
                "user_id": e.user_id,
                "team_id": e.team_id,
                "dept_id": e.dept_id,
                "model_alias": e.model_alias,
                "provider": e.provider,
                "input_tokens": e.input_tokens,
                "output_tokens": e.output_tokens,
                "cache_creation_tokens": e.cache_creation_tokens,
                "cache_read_tokens": e.cache_read_tokens,
                "reasoning_tokens": e.reasoning_tokens,
                "web_search_count": e.web_search_count,
                "cost_usd": str(e.cost_usd),
                "latency_ms": e.latency_ms,
                "ttft_ms": e.ttft_ms,
                "status": "SUCCESS",
                # asyncpg requires datetime instances for timestamptz bindings,
                # not ISO strings — parse here rather than letting CAST handle it.
                "requested_at": datetime.fromisoformat(e.requested_at),
                "completed_at": datetime.fromisoformat(e.completed_at),
                "is_streaming": e.is_streaming,
                "estimated_usage": e.estimated_usage,
                "downgraded_from": e.downgraded_from,
                "availability_fallback_from": e.availability_fallback_from,
                "sso_subject": e.sso_subject,
                "bedrock_request_id": e.bedrock_request_id,
                "client": e.client,
            }
            for e in entries
        ]
        await session.execute(_INSERT_USAGE_LOGS, params)

    async def _upsert_budget_usages(
        self, session: AsyncSession, entries: list[CostStreamEntry]
    ) -> None:
        """USER + TEAM 두 스코프 각각에 대해 (scope_id, period) 그룹별 합산 UPSERT."""
        # GROUP BY user_id + period → sum cost, 같은 로직 team에도 적용.
        user_sums: dict[tuple[str, str], Decimal] = defaultdict(lambda: Decimal("0"))
        team_sums: dict[tuple[str, str], Decimal] = defaultdict(lambda: Decimal("0"))
        for e in entries:
            user_sums[(e.user_id, e.period)] += e.cost_usd
            team_sums[(e.team_id, e.period)] += e.cost_usd

        user_params = [
            {"scope": "USER", "scope_id": uid, "period": period, "cost": str(cost)}
            for (uid, period), cost in user_sums.items()
        ]
        team_params = [
            {"scope": "TEAM", "scope_id": tid, "period": period, "cost": str(cost)}
            for (tid, period), cost in team_sums.items()
        ]
        if user_params:
            await session.execute(_UPSERT_BUDGET_USAGE, user_params)
        if team_params:
            await session.execute(_UPSERT_BUDGET_USAGE, team_params)

    async def _bump_daily_counters(self, entries: list[CostStreamEntry]) -> None:
        """usage:daily:* Redis 카운터 배치 INCRBY + TTL 48h."""
        pipe = self._redis.pipeline()
        # 같은 키의 TTL 재설정은 마지막 batch에만 하면 충분 — dedupe용 set.
        ttl_keys: set[str] = set()

        for e in entries:
            daily_prefix = f"usage:daily:user:{{{e.user_id}}}:{e.date}"
            model_prefix = f"{daily_prefix}:model:{e.model_alias}"

            pipe.incrbyfloat(f"{daily_prefix}:cost", float(e.cost_usd))
            pipe.incrby(
                f"{daily_prefix}:tokens",
                e.input_tokens + e.output_tokens + e.cache_creation_tokens + e.cache_read_tokens,
            )
            pipe.sadd(f"{daily_prefix}:models", e.model_alias)
            pipe.incrbyfloat(f"{model_prefix}:cost", float(e.cost_usd))
            pipe.incrby(f"{model_prefix}:input", e.input_tokens)
            pipe.incrby(f"{model_prefix}:output", e.output_tokens)
            pipe.incrby(f"{model_prefix}:cache_write", e.cache_creation_tokens)
            pipe.incrby(f"{model_prefix}:cache_read", e.cache_read_tokens)
            pipe.incrby(f"{model_prefix}:requests", 1)

            ttl_keys.update(
                {
                    f"{daily_prefix}:cost",
                    f"{daily_prefix}:tokens",
                    f"{daily_prefix}:models",
                    f"{model_prefix}:cost",
                    f"{model_prefix}:input",
                    f"{model_prefix}:output",
                    f"{model_prefix}:cache_write",
                    f"{model_prefix}:cache_read",
                    f"{model_prefix}:requests",
                }
            )

        for k in ttl_keys:
            pipe.expire(k, 172_800)

        await pipe.execute()

    async def _publish_thresholds(self, entries: list[CostStreamEntry]) -> None:
        """threshold_triggered 레코드마다 notifications:budget 발행.

        notification-worker는 이 payload를 받아서 DB에서 user_name/team_name/
        max_budget_usd 를 조회해 이메일 템플릿을 렌더링한다.
        """
        for e in entries:
            if e.threshold_triggered is None:
                continue
            try:
                event = {
                    "event_id": e.request_id,  # idempotency hint
                    "type": "budget_threshold",
                    "timestamp": e.completed_at,
                    "source": "cost-recorder-worker",
                    "user_id": e.user_id,
                    "team_id": e.team_id,
                    "threshold_pct": e.threshold_triggered,
                    "current_used_usd": str(e.cost_usd),
                    "period": e.period,
                    "policy": e.threshold_policy or "hard_block",
                    "target_type": "user",
                }
                await self._redis.publish("notifications:budget", json.dumps(event))
            except Exception:
                logger.warning(
                    "threshold_publish_failed",
                    user_id=e.user_id,
                    threshold=e.threshold_triggered,
                )
