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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
        sso_subject, bedrock_request_id, client, context_tier
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
        :client, :context_tier
    )
    ON CONFLICT (request_id) DO NOTHING
    """
)


#: per-app 예산 하위 한도를 갖는 client 들. gateway-proxy 의 PER_APP_BUDGET_CLIENTS 와
#: 같아야 한다 — 이쪽이 좁으면 그 앱의 누적 행이 없어 복원이 0 이 되고, 이쪽이 넓으면
#: 아무도 읽지 않는 행이 쌓인다.
_PER_APP_CLIENTS = ("claude-code", "cowork", "codex")

_UPSERT_BUDGET_USAGE = text(
    """
    INSERT INTO budget.budget_usages
        (id, scope, scope_id, period, client, used_usd, limit_usd, last_updated)
    VALUES (
        gen_random_uuid(),
        CAST(:scope AS budget.budget_scope),
        CAST(:scope_id AS uuid),
        :period,
        CAST(:client AS varchar),
        :cost,
        COALESCE((
            SELECT max_budget_usd
            FROM budget.budget_configs
            WHERE scope = CAST(:scope AS budget.budget_scope)
              AND scope_id = CAST(:scope_id AS uuid)
              AND is_active = true
              -- ⚠️ 이 술어가 없으면 per-app config 의 한도가 **총합 행**의 limit_usd 로
              --    박힌다. per-app 과 총액 config 가 같은 테이블에 살고, 정렬이
              --    effective_from DESC 뿐이라 어느 쪽이 잡히는지가 비결정적이다.
              --    admin-api 는 자기 쪽 같은 SQL 에 이미 이 술어를 걸고 있었다
              --    (services/budget_service.py) — 이 워커만 빠져 있었다.
              --    IS NOT DISTINCT FROM 을 쓰는 이유: client 가 NULL 일 때 `= NULL` 은
              --    UNKNOWN 이라 한 행도 매칭되지 않아 한도가 0 이 된다.
              AND client IS NOT DISTINCT FROM CAST(:client AS varchar)
            ORDER BY effective_from DESC
            LIMIT 1
        ), 0),
        now()
    )
    -- Conflict target MUST match the unique index from migration 0011:
    -- (scope, scope_id, period, COALESCE(client,'')). The pre-0011 3-col target
    -- no longer matches any index → ON CONFLICT would raise on migrated DBs.
    --
    -- ⚠️ 이 워커는 client=NULL 총합 행 **과 앱별 행 둘 다** 쓴다. 예전엔 총합만 썼고,
    --    그 결과 per-app 상한이 Redis 카운터로만 존재했다:
    --      * Redis 유실/failover 후 소진된 앱 예산이 조용히 0 으로 되돌아갔다
    --        (게이트웨이 복원 경로가 읽을 행이 아예 없었다),
    --      * REDIS_DEGRADED 시 DB 폴백 분기가 `client_used=0` 을 읽어 앱 한도가
    --        아예 적용되지 않았다.
    --    migration 0011 의 주석은 이미 "worker writes total + per-app rows" 라고
    --    적혀 있었다 — 스키마와 문서가 맞고 워커만 어긋난 상태였다.
    ON CONFLICT (scope, scope_id, period, COALESCE(client, ''))
    DO UPDATE SET used_usd = budget.budget_usages.used_usd + EXCLUDED.used_usd,
                  last_updated = now()
    """
)


_ALREADY_RECORDED_SQL = text(
    "SELECT request_id FROM usage.usage_logs WHERE request_id = ANY(:ids)"
)


def _dedup_in_batch(entries: list[CostStreamEntry]) -> list[CostStreamEntry]:
    """배치 안의 같은 ``request_id`` 를 하나로 접는다(첫 것을 남긴다).

    ⚠️ 크래시 없이도 재현된다: 게이트웨이의 spool 이 Redis 복구 후 페이로드를 다시
       발행하므로 같은 request_id 가 한 flush 배치 안에 두 번 들어올 수 있다.
       ``usage_logs`` 는 ``ON CONFLICT (request_id) DO NOTHING`` 으로 보호되지만
       ``budget_usages`` 는 **가산** UPSERT 라 두 번 더해진다.
    """
    seen: set[str] = set()
    out: list[CostStreamEntry] = []
    dropped = 0
    for e in entries:
        if e.request_id in seen:
            dropped += 1
            continue
        seen.add(e.request_id)
        out.append(e)
    if dropped:
        logger.warning("batch_intra_dedup", dropped=dropped, kept=len(out))
    return out


async def _filter_replays(
    session: AsyncSession, entries: list[CostStreamEntry]
) -> list[CostStreamEntry]:
    """이미 ``usage_logs`` 에 있는 ``request_id`` 를 걸러낸다(재처리 방어).

    ⚠️ 왜 필요한가. 소비자는 DB 커밋을 **먼저** 하고 그 다음 XACK 한다. 그 사이에 파드가
       죽으면(롤아웃 SIGKILL, OOM, 노드 축출) 같은 배치를 다시 읽는다. ``usage_logs`` 는
       ``ON CONFLICT DO NOTHING`` 으로 넘어가지만 ``budget_usages.used_usd`` 는 두 번
       더해진다. $12 짜리 배치면 사용자의 월 사용액이 $24 로 **영구히** 기록된다.

       그 값이 하필 진실의 원천이다: 게이트웨이는 Redis 가 degrade 되면 그것을 읽고,
       Redis 복구 후 카운터를 그것으로 되돌린다. 그래서 $30 한도 사용자가 실제 지출
       $15 에서 남은 달 내내 hard_block 되고, 손으로 SQL 을 고치는 것 외에 되돌릴 방법이
       없다.

    ⚠️ 이 SELECT 는 호출자의 트랜잭션 **안에서** 돈다 — 그래야 "확인 후 삽입" 사이에
       다른 커밋이 끼어들 창이 좁아진다. 완전한 배제는 아니지만(그건 usage_logs 의
       UNIQUE 가 담당한다), 재처리라는 실제 시나리오는 여기서 막힌다.
    """
    if not entries:
        return entries
    ids = [e.request_id for e in entries]
    rows = await session.execute(_ALREADY_RECORDED_SQL, {"ids": ids})
    existing = {r[0] for r in rows}
    if not existing:
        return entries
    logger.warning(
        "batch_replay_filtered",
        already_recorded=len(existing),
        batch_size=len(entries),
    )
    return [e for e in entries if e.request_id not in existing]


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

        # 배치 안 중복을 먼저 접는다 — 크래시 없이도 spool 재발행으로 생긴다.
        entries = _dedup_in_batch(entries)

        # 1. DB 쓰기 — 단일 트랜잭션. FK 위반 시 per-row fallback.
        try:
            async with self._session_factory() as session:
                # ⚠️ budget_usages 는 **가산** UPSERT 다. 재처리된 entry 를 걸러내지 않으면
                #    사용자의 기록 사용액이 영구히 두 배가 된다(_filter_replays 주석 참조).
                fresh = await _filter_replays(session, entries)
                if not fresh:
                    logger.info("batch_all_replays_skipped", batch_size=len(entries))
                    await session.commit()
                    return
                await self._insert_usage_logs(session, fresh)
                await self._upsert_budget_usages(session, fresh)
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
                    # ⚠️ 배치 경로와 **같은** 방어가 필요하다. 여기만 빼면 IntegrityError
                    #    한 번으로 폴백 경로로 넘어간 배치가 계속 이중청구한다.
                    if not await _filter_replays(session, [e]):
                        await session.commit()
                        continue
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
                "context_tier": e.context_tier,
            }
            for e in entries
        ]
        await session.execute(_INSERT_USAGE_LOGS, params)

    async def _upsert_budget_usages(
        self, session: AsyncSession, entries: list[CostStreamEntry]
    ) -> None:
        """USER + TEAM 총합, 그리고 per-app 하위 행을 그룹별 합산 UPSERT.

        ⚠️ 앱별 행이 없으면 per-app 예산은 Redis 카운터로만 존재한다 — 유실되면
           소진된 한도가 0 으로 되돌아가고, Redis 열화 시 DB 폴백이 0 을 읽어 한도가
           적용되지 않는다. 이 행들이 그 두 경로의 진실의 원천이다.
        """
        # GROUP BY user_id + period → sum cost, 같은 로직 team에도 적용.
        user_sums: dict[tuple[str, str], Decimal] = defaultdict(lambda: Decimal("0"))
        team_sums: dict[tuple[str, str], Decimal] = defaultdict(lambda: Decimal("0"))
        # (user_id, period, client) → 앱별 누적. per-app 예산을 갖는 client 만.
        app_sums: dict[tuple[str, str, str], Decimal] = defaultdict(lambda: Decimal("0"))
        for e in entries:
            user_sums[(e.user_id, e.period)] += e.cost_usd
            team_sums[(e.team_id, e.period)] += e.cost_usd
            if e.client in _PER_APP_CLIENTS:
                app_sums[(e.user_id, e.period, e.client)] += e.cost_usd

        user_params = [
            {"scope": "USER", "scope_id": uid, "period": period, "cost": str(cost),
             "client": None}
            for (uid, period), cost in user_sums.items()
        ]
        team_params = [
            {"scope": "TEAM", "scope_id": tid, "period": period, "cost": str(cost),
             "client": None}
            for (tid, period), cost in team_sums.items()
        ]
        app_params = [
            {"scope": "USER", "scope_id": uid, "period": period, "cost": str(cost),
             "client": client}
            for (uid, period, client), cost in app_sums.items()
        ]
        if user_params:
            await session.execute(_UPSERT_BUDGET_USAGE, user_params)
        if team_params:
            await session.execute(_UPSERT_BUDGET_USAGE, team_params)
        # ⚠️ 앱별 행은 총합 행을 **대체하지 않고 더한다.** 총합은 client 무관 전체이고
        #    앱별은 그 하위 집합이다 — 둘을 합산해 읽는 코드가 있으면 이중계상이 된다.
        #    (분석 경로가 client 축으로 파티션해 읽는 이유가 그것이다.)
        if app_params:
            await session.execute(_UPSERT_BUDGET_USAGE, app_params)

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
                # ⚠️ 도메인 필드는 반드시 `payload` 봉투 안에 넣는다. notification-worker 의
                #    NotificationEvent(notification-worker/src/worker/schemas/events.py:39-44)
                #    는 payload 를 필수로 요구하고 핸들러/recipient_resolver 가 그 안을
                #    읽는다. 예전엔 전부 평평해서 worker 가 "payload Field required" 로
                #    전량 폐기했고, 예산 80% 경고가 한 번도 발송되지 않았다.
                #    envelope 4필드(event_id/type/timestamp/source)만 최상위에 둔다.
                event = {
                    "event_id": e.request_id,  # idempotency hint
                    "type": "budget_threshold",
                    "timestamp": e.completed_at,
                    "source": "cost-recorder-worker",
                    "payload": {
                        "user_id": e.user_id,
                        "team_id": e.team_id,
                        "threshold_pct": e.threshold_triggered,
                        "current_used_usd": str(e.cost_usd),
                        "period": e.period,
                        "policy": e.threshold_policy or "hard_block",
                        "target_type": "user",
                    },
                }
                await self._redis.publish("notifications:budget", json.dumps(event))
            except Exception:
                logger.warning(
                    "threshold_publish_failed",
                    user_id=e.user_id,
                    threshold=e.threshold_triggered,
                )
