# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Rate Limit Config 로더 (FR-4.1 Step 5).

`model.rate_limit_configs` 테이블을 Redis-first + DB fallback 으로 조회해
스코프별 RPM/TPM 한도를 반환. Admin 변경 시 Redis 키 DEL로 즉시 반영.

조회 우선순위 (스코프 × 2 세분성):
    1. (scope, scope_id, model_alias=X)  — 모델별 한도
    2. (scope, scope_id, model_alias=NULL) — 전체 모델 합산 한도

1이 존재하면 그 값을 사용, 없으면 2 fallback. GLOBAL 스코프는 항상 1만 사용.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal

import structlog
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.model import RateLimitConfig

logger = structlog.get_logger(__name__)

# Redis 캐시 TTL — Admin이 변경 시 DEL로 즉시 무효화하지만 fallback으로 짧은 TTL도 유지
_CONFIG_CACHE_TTL_SEC = 300


@dataclass
class ScopeLimits:
    """한 스코프(USER/TEAM/GLOBAL)의 RPM/TPM/CPM/CPH 한도.

    ``None`` = 한도 미설정(unlimited). 해당 metric 체크 스킵.
    CPM/CPH는 USER/TEAM 스코프만 유효 (GLOBAL은 월 예산 엔진이 담당).
    """

    rpm: int | None = None
    tpm: int | None = None
    cpm: Decimal | None = None
    cph: Decimal | None = None


@dataclass
class AllScopeLimits:
    """한 요청에 적용되는 3-스코프 한도 번들."""

    user: ScopeLimits = field(default_factory=ScopeLimits)
    team: ScopeLimits = field(default_factory=ScopeLimits)
    global_: ScopeLimits = field(default_factory=ScopeLimits)


def _cache_key(scope: str, scope_id: str | None, model_alias: str) -> str:
    sid = scope_id if scope_id is not None else "NULL"
    return f"rl:config:{scope}:{sid}:{model_alias}"


async def _load_scope_from_cache(
    redis, scope: str, scope_id: str | None, model_alias: str
) -> ScopeLimits | None:
    if redis is None:
        return None
    try:
        raw = await redis.get(_cache_key(scope, scope_id, model_alias))
        if raw is None:
            return None
        data = json.loads(raw)
        cpm_raw = data.get("cpm")
        cph_raw = data.get("cph")
        return ScopeLimits(
            rpm=data.get("rpm"),
            tpm=data.get("tpm"),
            cpm=Decimal(str(cpm_raw)) if cpm_raw is not None else None,
            cph=Decimal(str(cph_raw)) if cph_raw is not None else None,
        )
    except Exception:
        logger.warning("rl_config_cache_read_failed", scope=scope, scope_id=scope_id)
        return None


async def _save_scope_to_cache(
    redis, scope: str, scope_id: str | None, model_alias: str, limits: ScopeLimits
) -> None:
    if redis is None:
        return
    try:
        await redis.set(
            _cache_key(scope, scope_id, model_alias),
            json.dumps(
                {
                    "rpm": limits.rpm,
                    "tpm": limits.tpm,
                    "cpm": str(limits.cpm) if limits.cpm is not None else None,
                    "cph": str(limits.cph) if limits.cph is not None else None,
                }
            ),
            ex=_CONFIG_CACHE_TTL_SEC,
        )
    except Exception:
        logger.warning("rl_config_cache_write_failed", scope=scope, scope_id=scope_id)


async def _query_scope_from_db(
    db: AsyncSession,
    scope: str,
    scope_id: str | None,
    model_alias: str,
) -> ScopeLimits:
    """DB에서 스코프 한도 조회 (모델별 우선, NULL fallback).

    GLOBAL 스코프는 ``scope_id IS NULL`` + ``model_alias=X`` 만 유효 — Bedrock 쿼터.
    """
    conditions = [RateLimitConfig.scope == scope, RateLimitConfig.is_active.is_(True)]

    if scope == "GLOBAL":
        conditions.append(RateLimitConfig.scope_id.is_(None))
        conditions.append(RateLimitConfig.model_alias == model_alias)
    else:
        conditions.append(RateLimitConfig.scope_id == scope_id)
        # 모델별 (우선) OR 전체 (fallback)
        conditions.append(
            or_(
                RateLimitConfig.model_alias == model_alias,
                RateLimitConfig.model_alias.is_(None),
            )
        )

    result = await db.execute(select(RateLimitConfig).where(and_(*conditions)))
    rows = result.scalars().all()

    if not rows:
        return ScopeLimits()

    # 모델별 먼저, 없으면 NULL 사용
    model_specific = next((r for r in rows if r.model_alias == model_alias), None)
    chosen = model_specific or rows[0]
    return ScopeLimits(
        rpm=chosen.rpm_limit,
        tpm=chosen.tpm_limit,
        cpm=chosen.cpm_limit_usd if scope != "GLOBAL" else None,
        cph=chosen.cph_limit_usd if scope != "GLOBAL" else None,
    )


async def load_all_scope_limits(
    *,
    redis,
    db: AsyncSession | None,
    user_id: str,
    team_id: str | None,
    model_alias: str,
) -> AllScopeLimits:
    """3-스코프 한도 한꺼번에 로드 (Redis → DB fallback).

    Redis miss 시 DB 조회 → Redis 캐시 주입 (TTL 5분).
    DB 없음/실패 시 해당 스코프는 unlimited (None) 처리.
    """
    result = AllScopeLimits()

    scope_specs: list[tuple[str, str | None, str]] = [
        ("USER", user_id, "user"),
    ]
    if team_id is not None:
        scope_specs.append(("TEAM", team_id, "team"))
    scope_specs.append(("GLOBAL", None, "global_"))

    for scope_name, scope_id, attr in scope_specs:
        cached = await _load_scope_from_cache(redis, scope_name, scope_id, model_alias)
        if cached is not None:
            setattr(result, attr, cached)
            continue

        if db is None:
            # DB fallback 불가 → unlimited
            continue

        try:
            limits = await _query_scope_from_db(db, scope_name, scope_id, model_alias)
        except Exception:
            logger.exception(
                "rl_config_db_query_failed",
                scope=scope_name,
                scope_id=scope_id,
                model_alias=model_alias,
            )
            limits = ScopeLimits()

        setattr(result, attr, limits)
        await _save_scope_to_cache(redis, scope_name, scope_id, model_alias, limits)

    return result


async def invalidate_scope_cache(
    redis,
    scope: str,
    scope_id: str | None,
    model_alias: str,
) -> None:
    """Admin이 한도 변경 시 호출 — 해당 스코프 캐시 즉시 무효화."""
    if redis is None:
        return
    try:
        await redis.delete(_cache_key(scope, scope_id, model_alias))
    except Exception:
        logger.warning("rl_config_cache_invalidate_failed", scope=scope, scope_id=scope_id)
