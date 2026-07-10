# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.budget import BudgetScope, DowngradePolicy

logger = structlog.get_logger(__name__)


CACHE_KEY_FMT = "budget:downgrade:team:{team_id}"
CACHE_TTL_SECONDS = 60


@dataclass(frozen=True)
class DowngradeRule:
    from_alias: str
    to_alias: str
    threshold_pct: int


def apply_chain(
    alias: str,
    rules: list[DowngradeRule],
    current_pct: int,
    max_depth: int = 5,
) -> tuple[str, int]:
    """체인 적용: from_alias 매칭 + 임계치 도달 시 반복적으로 to_alias로 교체.

    Returns: (effective_alias, hops_applied)
    - visited set으로 사이클 방지
    - max_depth로 무한 체인 방어
    - 매칭 규칙 없으면 (alias, 0) 반환
    """
    visited = {alias}
    hops = 0
    for _ in range(max_depth):
        rule = next(
            (r for r in rules if r.from_alias == alias and current_pct >= r.threshold_pct),
            None,
        )
        if rule is None or rule.to_alias in visited:
            break
        visited.add(rule.to_alias)
        alias = rule.to_alias
        hops += 1
    return alias, hops


class DowngradePolicyLoader:
    """TEAM scope 다운그레이드 정책 조회 — Redis 캐시 우선, DB fallback.

    캐시 invalidate는 admin-api budget_service가 담당
    (budget_service.py:525, 558). 본 loader는 read-only.
    """

    async def get_active_rules(
        self,
        redis,
        db: AsyncSession | None,
        team_id: uuid.UUID,
    ) -> list[DowngradeRule]:
        cache_key = CACHE_KEY_FMT.format(team_id=team_id)

        # 1) Redis HIT 시도
        if redis is not None:
            try:
                cached = await redis.get(cache_key)
                if cached is not None:
                    return self._deserialize(cached)
            except Exception:
                logger.warning("downgrade_redis_get_failed", team_id=str(team_id))

        # 2) DB fallback
        if db is None:
            return []

        rules = await self._query_db(db, team_id)

        # 3) 캐시 write-through
        if redis is not None:
            try:
                payload = json.dumps(
                    [
                        {
                            "from_alias": r.from_alias,
                            "to_alias": r.to_alias,
                            "threshold_pct": r.threshold_pct,
                        }
                        for r in rules
                    ]
                ).encode()
                await redis.setex(cache_key, CACHE_TTL_SECONDS, payload)
            except Exception:
                logger.warning("downgrade_redis_setex_failed", team_id=str(team_id))

        return rules

    @staticmethod
    def _deserialize(raw: bytes | str) -> list[DowngradeRule]:
        data = json.loads(raw)
        return [
            DowngradeRule(
                from_alias=item["from_alias"],
                to_alias=item["to_alias"],
                threshold_pct=int(item["threshold_pct"]),
            )
            for item in data
        ]

    @staticmethod
    async def _query_db(db: AsyncSession, team_id: uuid.UUID) -> list[DowngradeRule]:
        stmt = (
            select(DowngradePolicy)
            .where(DowngradePolicy.scope == BudgetScope.TEAM)
            .where(DowngradePolicy.scope_id == team_id)
            .where(DowngradePolicy.is_active.is_(True))
            .order_by(DowngradePolicy.threshold_pct.asc())
        )
        result = await db.execute(stmt)
        return [
            DowngradeRule(
                from_alias=row.from_model_alias,
                to_alias=row.to_model_alias,
                threshold_pct=row.threshold_pct,
            )
            for row in result.scalars().all()
        ]
