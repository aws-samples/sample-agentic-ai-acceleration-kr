# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import json
from decimal import Decimal

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.budget import BudgetConfig, BudgetScope, BudgetUsage
from app.schemas.domain import BudgetPolicy, BudgetStatus
from app.services.lua_loader import LuaScriptLoader

logger = structlog.get_logger(__name__)

BUDGET_CONFIG_TTL = None  # 무기한 (Admin 변경 시 DEL로 무효화)

# per-app(client) 예산 대상 클라이언트. client_identifier 토큰과 동일 집합
# (claude-code / cowork / codex). 새 앱 추가 시 여기 + DB CHECK 제약 + admin-api 동기화.
PER_APP_BUDGET_CLIENTS = ("claude-code", "cowork", "codex")

# 정책 파라미터 기본값 — DB 스키마에 없으므로 Python 기본값 사용.
# 향후 budget_configs에 컬럼 추가 시 (requirements.md §6b 장래 추가 항목) DB 값으로 교체.
DEFAULT_SOFT_LIMIT_PCT = 110
DEFAULT_THROTTLE_RPM_PCT = 50
DEFAULT_THRESHOLDS = [80, 90, 100]


def _policy_to_lua_value(policy: BudgetPolicy | str) -> str:
    """Python BudgetPolicy(lowercase value) → Lua/Redis 내부 표현(lowercase)."""
    return policy.value if isinstance(policy, BudgetPolicy) else str(policy).lower()


def _db_policy_to_domain(db_policy) -> BudgetPolicy:
    """DB enum(UPPERCASE) → domain BudgetPolicy(lowercase value).

    admin-api `BudgetPolicy` ORM과 gateway-proxy `schemas.domain.BudgetPolicy`의
    value 표기가 다름(UPPERCASE vs lowercase). 변환 계층.
    """
    raw = db_policy.value if hasattr(db_policy, "value") else str(db_policy)
    return BudgetPolicy(raw.lower())


class BudgetService:
    """예산 정책 확인 서비스."""

    async def check_budget(
        self,
        redis,
        db: AsyncSession | None,
        user_id: str,
        team_id: str,
        period: str,
        client: str | None = None,
    ) -> BudgetStatus:
        """사용자 예산 확인 (Redis 우선, DB fallback).
        예산 미설정 시 PermissionError 발생 (429 no_budget_assigned).
        client 가 'claude-code' 또는 'cowork' 이면 앱별 예산도 추가로 확인한다.
        """
        # Redis Cluster hash tag: {<user_id>} co-locates usage/config on same slot.
        user_key = f"budget:user:{{{user_id}}}:{period}"
        user_config_key = f"budget:config:user:{{{user_id}}}"
        team_key = f"budget:team:{{{team_id}}}:{period}"
        team_config_key = f"budget:config:team:{{{team_id}}}"

        # Redis 예산 설정 확인
        if redis is not None:
            # TEAM config cold-cache fallback: init SQL / alembic backfill 로 DB에
            # 삽입된 TEAM 예산이 Redis에 없으면 DB에서 조회해 캐시 (admin-api startup
            # warmup 의 안전망).
            if db is not None and not await redis.exists(team_config_key):
                await self._hydrate_team_config_cache(redis, db, team_id)

            # P0-③: USER config cold-cache fallback. If admin's best-effort SET of
            # the user-config key (which carries app_clients) was lost, the per-app
            # budget gate below would silently skip → budget bypass. Rehydrate from
            # DB on miss (ensure_config_cached rebuilds app_clients from active
            # per-app BudgetConfig rows) so the gate stays enforced.
            if db is not None and not await redis.exists(user_config_key):
                await self.ensure_config_cached(redis, db, user_id)

            # Redis 키 없으면 DB에서 복구 후 재캐싱 (LRU 삭제 대비)
            if not await redis.exists(user_key) and db is not None:
                try:
                    # budget_usages에서 복구 (raw SQL — 모델 컬럼명 불일치 방지)
                    # fallback: usage_logs SUM
                    from sqlalchemy import func, select, text

                    result = await db.execute(
                        text(
                            "SELECT used_usd FROM budget.budget_usages "
                            "WHERE scope = 'USER' AND scope_id = :uid AND period = :period"
                        ),
                        {"uid": user_id, "period": period},
                    )
                    row = result.scalar_one_or_none()

                    if row is None:
                        # budget_usages 없으면 usage_logs에서 SUM
                        from app.models.usage import UsageRecord

                        stmt2 = select(func.coalesce(func.sum(UsageRecord.cost_usd), 0)).where(
                            UsageRecord.user_id == user_id,
                            func.to_char(UsageRecord.requested_at, "YYYY-MM") == period,
                        )
                        result2 = await db.execute(stmt2)
                        used_from_db = result2.scalar_one()
                    else:
                        used_from_db = row

                    if used_from_db and used_from_db > 0:
                        await redis.set(user_key, str(used_from_db))
                        logger.info(
                            "budget_counter_restored",
                            user_id=user_id,
                            period=period,
                            used=str(used_from_db),
                        )
                except Exception:
                    logger.exception("budget_counter_restore_failed", user_id=user_id)

            # Redis Cluster CROSSSLOT 대응: USER/TEAM 키는 hash tag 가 달라 slot 이
            # 서로 다르므로 단일 Lua 호출에 묶을 수 없다. scope 별로 2번 호출하고
            # Python 에서 결과를 합친다. Lua 내부 체크는 여전히 각 scope 원자적.
            try:
                script = LuaScriptLoader.get("budget_check")

                user_raw = await redis.eval(script, 2, user_key, user_config_key, "user")
                user_result = json.loads(user_raw)

                # USER config 미설정 → pass-through (Q 정책)
                if user_result.get("config_present") and not user_result["allowed"]:
                    raise PermissionError(user_result.get("reason", "user_budget_exceeded"))

                team_raw = await redis.eval(script, 2, team_key, team_config_key, "team")
                team_result = json.loads(team_raw)

                # TEAM config 미설정 → deny (C-1 정책)
                if not team_result.get("config_present"):
                    raise PermissionError("team_budget_unset")

                if not team_result["allowed"]:
                    raise PermissionError(team_result.get("reason", "team_budget_exceeded"))

                # 앱(client) 예산 확인 — 'claude-code' / 'cowork' 이고, USER config 에
                # 이 client 가 app_clients 로 등록된 경우에만 추가 eval (free gate).
                # USER check 결과가 이미 config.app_clients 를 echo 하므로 추가 RTT 없이
                # 게이팅 — 앱 예산 없는 유저는 3번째 eval 자체를 건너뛴다.
                # 불변식(P0-③ review): per-app 예산은 USER 총예산의 하위 서브-리밋이므로
                # 항상 부모 USER 예산이 존재한다(admin-api 가 부모 없는 per-app 생성 거부).
                # → app_clients 게이트는 부모 config 가 있다는 전제에서 신뢰 가능.
                user_app_clients = user_result.get("app_clients")
                if (
                    client in PER_APP_BUDGET_CLIENTS
                    and isinstance(user_app_clients, list)
                    and client in user_app_clients
                ):
                    client_key = f"budget:user:{{{user_id}}}:{client}:{period}"
                    client_config_key = f"budget:config:user:{{{user_id}}}:{client}"
                    # P0-③: per-app config cold-cache fallback. If admin's
                    # best-effort SET of this key was lost, the Lua would see
                    # config_present=false → pass-through (bypass). Rehydrate from
                    # DB on miss so the per-app limit stays enforced.
                    if db is not None and not await redis.exists(client_config_key):
                        await self._hydrate_client_config_cache(
                            redis, db, user_id, client
                        )
                    client_raw = await redis.eval(
                        script, 2, client_key, client_config_key, "client"
                    )
                    client_result = json.loads(client_raw)
                    if client_result.get("config_present") and not client_result["allowed"]:
                        raise PermissionError(
                            client_result.get("reason", "client_budget_exceeded")
                        )

                # 둘 다 통과 — TEAM 결과를 우선 반환 (limit/used 정보가 더 의미있음)
                final = team_result
                return BudgetStatus(
                    remaining_usd=Decimal(str(final["remaining_usd"])),
                    limit_usd=Decimal(str(final.get("limit_usd", 0))),
                    used_usd=Decimal(str(final["used_usd"])),
                    policy=BudgetPolicy(final["policy"]),
                    throttle_rpm_pct=final.get("throttle_rpm_pct", 50),
                    threshold_pct=final.get("threshold_pct", 0),
                    throttle_active=final.get("throttle_active", False),
                    soft_warning=final.get("soft_warning", False),
                )
            except PermissionError:
                raise
            except Exception:
                logger.exception("redis_budget_check_failed", user_id=user_id)
                # Redis 실패 시 DB fallback
                pass

        # DB fallback (REDIS_DEGRADED)
        try:
            return await self._check_budget_db(db, user_id, team_id, period, client=client)
        except PermissionError:
            raise
        except Exception:
            logger.exception("budget_db_fallback_failed", user_id=user_id)
            raise PermissionError("no_budget_assigned")

    async def _check_budget_db(
        self,
        db: AsyncSession,
        user_id: str,
        team_id: str,
        period: str,
        client: str | None = None,
    ) -> BudgetStatus:
        """DB SELECT 기반 예산 확인 (Redis fallback 경로).

        DB 컬럼: scope / scope_id / max_budget_usd (KI-09 수정 반영).
        soft_limit_pct, throttle_rpm_pct, thresholds는 현재 DB 스키마에 없으므로
        Python 기본값 사용.
        client 가 설정된 경우 앱별 BudgetConfig/BudgetUsage 도 확인한다.
        앱 예산 미설정(config=None) → pass-through.
        """
        if db is None:
            raise PermissionError("no_budget_assigned")

        # Q 정책: USER 예산 미설정 → pass-through (차단 안 함)
        # client IS NULL 로 한정 — per-app(client) row 가 생기면 (scope,scope_id) 당
        # 행이 2개 이상이 되어 scalar_one_or_none() 가 MultipleResultsFound 로 터진다.
        # (REDIS_DEGRADED 시 app 예산 유저가 전부 차단되는 잠복 장애 방지.)
        user_cfg_result = await db.execute(
            select(BudgetConfig)
            .where(BudgetConfig.scope == BudgetScope.USER)
            .where(BudgetConfig.scope_id == user_id)
            .where(BudgetConfig.client.is_(None))
            .where(BudgetConfig.is_active == True)  # noqa: E712
        )
        user_config = user_cfg_result.scalar_one_or_none()

        if user_config is not None:
            user_usage_result = await db.execute(
                select(BudgetUsage)
                .where(BudgetUsage.scope == BudgetScope.USER)
                .where(BudgetUsage.scope_id == user_id)
                .where(BudgetUsage.client.is_(None))
                .where(BudgetUsage.period == period)
            )
            user_usage = user_usage_result.scalar_one_or_none()
            user_used = user_usage.used_usd if user_usage else Decimal("0")
            if user_used >= user_config.max_budget_usd:
                raise PermissionError("user_budget_exceeded")

        # C-1 정책: TEAM 예산 미설정 → 차단
        team_cfg_result = await db.execute(
            select(BudgetConfig)
            .where(BudgetConfig.scope == BudgetScope.TEAM)
            .where(BudgetConfig.scope_id == team_id)
            .where(BudgetConfig.is_active == True)  # noqa: E712
        )
        team_config = team_cfg_result.scalar_one_or_none()
        if team_config is None:
            raise PermissionError("team_budget_unset")

        team_usage_result = await db.execute(
            select(BudgetUsage)
            .where(BudgetUsage.scope == BudgetScope.TEAM)
            .where(BudgetUsage.scope_id == team_id)
            .where(BudgetUsage.period == period)
        )
        team_usage = team_usage_result.scalar_one_or_none()
        team_used = team_usage.used_usd if team_usage else Decimal("0")

        max_budget = team_config.max_budget_usd
        if team_used >= max_budget:
            raise PermissionError("team_budget_exceeded")

        remaining = max_budget - team_used
        threshold_pct = int(team_used / max_budget * 100) if max_budget > 0 else 0

        policy = _db_policy_to_domain(team_config.policy)
        if policy == BudgetPolicy.HARD_BLOCK and team_used >= max_budget:
            raise PermissionError("hard_block")

        # SOFT_WARNING: used ≥ limit × soft_limit_pct/100 이면 차단 (soft_limit_exceeded),
        # limit ≤ used < effective_limit 이면 통과 + soft_warning=true 플래그.
        # Redis `budget_check.lua`의 SOFT_WARNING 분기와 동일 의미. Redis 다운 시 DB
        # fallback 경로가 enforcement를 빠뜨리지 않도록 이 분기를 추가해야 함.
        soft_warning = False
        if policy == BudgetPolicy.SOFT_WARNING and max_budget > 0:
            effective_limit = max_budget * Decimal(DEFAULT_SOFT_LIMIT_PCT) / Decimal(100)
            if team_used >= effective_limit:
                raise PermissionError("team_soft_limit_exceeded")
            if team_used >= max_budget:
                soft_warning = True

        throttle_active = False
        if policy == BudgetPolicy.THROTTLE:
            for threshold in sorted(DEFAULT_THRESHOLDS, reverse=True):
                if threshold_pct >= threshold:
                    throttle_active = True
                    break

        # 앱(client) 예산 확인 (REDIS_DEGRADED 경로) — 미설정 시 pass-through.
        if client in PER_APP_BUDGET_CLIENTS:
            client_cfg_result = await db.execute(
                select(BudgetConfig)
                .where(BudgetConfig.scope == BudgetScope.USER)
                .where(BudgetConfig.scope_id == user_id)
                .where(BudgetConfig.client == client)
                .where(BudgetConfig.is_active == True)  # noqa: E712
            )
            client_config = client_cfg_result.scalar_one_or_none()
            if client_config is not None:
                client_usage_result = await db.execute(
                    select(BudgetUsage)
                    .where(BudgetUsage.scope == BudgetScope.USER)
                    .where(BudgetUsage.scope_id == user_id)
                    .where(BudgetUsage.client == client)
                    .where(BudgetUsage.period == period)
                )
                client_usage = client_usage_result.scalar_one_or_none()
                client_used = client_usage.used_usd if client_usage else Decimal("0")
                if client_used >= client_config.max_budget_usd:
                    raise PermissionError("client_budget_exceeded")

        return BudgetStatus(
            remaining_usd=remaining,
            limit_usd=max_budget,
            used_usd=team_used,
            policy=policy,
            soft_limit_pct=DEFAULT_SOFT_LIMIT_PCT,
            throttle_rpm_pct=DEFAULT_THROTTLE_RPM_PCT,
            threshold_pct=threshold_pct,
            thresholds=list(DEFAULT_THRESHOLDS),
            throttle_active=throttle_active,
            soft_warning=soft_warning,
        )

    async def ensure_config_cached(
        self,
        redis,
        db: AsyncSession,
        user_id: str,
    ) -> None:
        """예산 설정이 Redis에 없으면 DB에서 조회하여 캐시.

        Lua 스크립트는 lowercase policy 값('hard_block' 등)을 기대하므로
        DB UPPERCASE enum을 변환해서 저장. 정책 파라미터(soft/throttle/thresholds)는
        Python 기본값 사용 (DB 스키마에 없음).
        """
        config_key = f"budget:config:user:{{{user_id}}}"
        if redis is None:
            return
        cached = await redis.get(config_key)
        if cached:
            return

        # client IS NULL = USER total config (per-app rows는 별도 키로 캐시됨).
        # 필터 없으면 app 예산 유저에서 MultipleResultsFound.
        result = await db.execute(
            select(BudgetConfig)
            .where(BudgetConfig.scope == BudgetScope.USER)
            .where(BudgetConfig.scope_id == user_id)
            .where(BudgetConfig.client.is_(None))
            .where(BudgetConfig.is_active == True)  # noqa: E712
        )
        config = result.scalar_one_or_none()
        if config:
            # app_clients 는 DB의 활성 per-app budget row에서 재구성 — 이 캐시 재생성이
            # admin-api가 user-config JSON에 써둔 app_clients 를 덮어쓰지 않도록(free gate 보존).
            app_rows = await db.execute(
                select(BudgetConfig.client)
                .where(BudgetConfig.scope == BudgetScope.USER)
                .where(BudgetConfig.scope_id == user_id)
                .where(BudgetConfig.client.isnot(None))
                .where(BudgetConfig.is_active == True)  # noqa: E712
            )
            app_clients = [c for c in app_rows.scalars().all() if c]
            config_data = {
                "limit_usd": str(config.max_budget_usd),
                "policy": _policy_to_lua_value(_db_policy_to_domain(config.policy)),
                "soft_limit_pct": DEFAULT_SOFT_LIMIT_PCT,
                "throttle_rpm_pct": DEFAULT_THROTTLE_RPM_PCT,
                "thresholds": list(DEFAULT_THRESHOLDS),
                "app_clients": app_clients,
            }
            # TTL 300s to match team/client hydrate + admin warmers. Without it a
            # lost invalidation could leave this parent config stale-forever
            # (P0-③ review fix). admin DEL is the durable path; TTL is the backstop.
            await redis.set(config_key, json.dumps(config_data), ex=300)

    async def _hydrate_team_config_cache(
        self,
        redis,
        db: AsyncSession,
        team_id: str,
    ) -> None:
        """TEAM budget config cache miss 시 DB에서 조회해 Redis에 SET (best-effort).

        admin-api startup warmup 의 안전망:
        - init SQL seed 예산 또는 post-startup 신규 팀 등 admin-api warmup 이후에
          생성된 TEAM 예산이 아직 Redis에 없는 경우를 처리.
        - 키가 없으면 do-nothing → Lua 가 team_budget_unset 을 정상 반환.
        """
        try:
            result = await db.execute(
                select(BudgetConfig)
                .where(BudgetConfig.scope == BudgetScope.TEAM)
                .where(BudgetConfig.scope_id == team_id)
                .where(BudgetConfig.is_active == True)  # noqa: E712
                .limit(1)
            )
            config = result.scalar_one_or_none()
            if config is None:
                return

            config_data = {
                "limit_usd": str(config.max_budget_usd),
                "policy": _policy_to_lua_value(_db_policy_to_domain(config.policy)),
                "thresholds": list(DEFAULT_THRESHOLDS),
            }
            config_key = f"budget:config:team:{{{team_id}}}"
            # TTL은 admin-api BUDGET_CONFIG_CACHE_TTL 과 일치 (5분)
            await redis.set(config_key, json.dumps(config_data), ex=300)
            logger.info(
                "team_budget_config_hydrated",
                team_id=team_id,
                limit_usd=str(config.max_budget_usd),
            )
        except Exception:
            logger.warning("team_budget_config_hydrate_failed", team_id=team_id)

    async def _hydrate_client_config_cache(
        self,
        redis,
        db: AsyncSession,
        user_id: str,
        client: str,
    ) -> None:
        """Per-app (user, client) budget config cache miss 시 DB에서 조회해 SET.

        P0-③ 안전망: admin 의 best-effort SET 이 유실되어 per-app config 키가
        없을 때, gateway 가 DB 의 활성 per-app BudgetConfig 행으로 키를 재생성해
        per-app 한도가 우회되지 않도록 한다. 키 shape 은 admin
        `_sync_redis_app_config` 와 동일(limit_usd, lowercase policy, thresholds).
        키가 없으면(활성 행 없음) do-nothing → Lua 가 config_present=false 로
        pass-through (정상: 해당 client 에 per-app 예산이 없는 경우).
        """
        try:
            result = await db.execute(
                select(BudgetConfig)
                .where(BudgetConfig.scope == BudgetScope.USER)
                .where(BudgetConfig.scope_id == user_id)
                .where(BudgetConfig.client == client)
                .where(BudgetConfig.is_active == True)  # noqa: E712
                .limit(1)
            )
            config = result.scalar_one_or_none()
            if config is None:
                return

            config_data = {
                "limit_usd": str(config.max_budget_usd),
                "policy": _policy_to_lua_value(_db_policy_to_domain(config.policy)),
                "thresholds": list(DEFAULT_THRESHOLDS),
            }
            config_key = f"budget:config:user:{{{user_id}}}:{client}"
            await redis.set(config_key, json.dumps(config_data), ex=300)
            logger.info(
                "client_budget_config_hydrated",
                user_id=user_id,
                client=client,
                limit_usd=str(config.max_budget_usd),
            )
        except Exception:
            logger.warning(
                "client_budget_config_hydrate_failed", user_id=user_id, client=client
            )
