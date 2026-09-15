# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import re
import uuid
from datetime import date
from decimal import Decimal

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clients import CLIENT_ORDER
from app.core.budget_cache import refresh_user_app_clients, write_user_budget_config
from app.core.audit import audit_logger
from app.core.auth import CurrentUser
from app.core.cache_invalidation import CacheInvalidationManager
from app.core.exceptions import ForbiddenError, NotFoundError, ValidationError
from app.models.auth import UserRole
from app.models.budget import BudgetConfig, BudgetPolicy, BudgetScope, DowngradePolicy, PeriodType
from app.repositories.budget_repository import BudgetRepository, DowngradePolicyRepository
from app.repositories.user_repository import UserRepository
from app.schemas.budgets import (
    AllocationEntry,
    AllocateBudgetRequest,
    AutoDowngradeConfigRequest,
    AutoDowngradeConfigResponse,
    BudgetSummaryItem,
    BudgetSummaryResponse,
    DowngradeRuleResponse,
    SeedSpentItem,
    SeedSpentResponse,
    SeedSpentResult,
    SetBudgetRequest,
    TeamBudgetAllocation,
)

logger = structlog.get_logger()

BUDGET_CONFIG_CACHE_TTL = 300  # 5 min; matches VK_AUTH_CACHE_TTL in key_service

_PERIOD_RE = re.compile(r"^\d{4}-\d{2}$")
#: 단일 출처는 core/clients.py 다 — 앱 추가 시 한 곳만 고치면 되도록.
#: 튜플로 유지하는 이유: 기존 호출부가 순서를 가정한 곳이 있다.
_ALLOWED_CLIENTS = CLIENT_ORDER


def _redis_usage_key(scope: str, scope_id: str, period: str, client: str | None) -> str:
    """Enforcement counter key. MUST match gateway-proxy budget_check.lua keys.

    USER: budget:user:{<id>}:<period>   (triple-brace = Redis Cluster hash tag)
    TEAM: budget:team:{<id>}:<period>
    APP : budget:user:{<id>}:<client>:<period>
    """
    scope_type = scope.lower()
    if client:
        return f"budget:{scope_type}:{{{scope_id}}}:{client}:{period}"
    return f"budget:{scope_type}:{{{scope_id}}}:{period}"


class BudgetService:
    def __init__(self, cache_mgr: CacheInvalidationManager) -> None:
        self._cache_mgr = cache_mgr

    _SEED_UPSERT = text(
        """
        INSERT INTO budget.budget_usages
            (id, scope, scope_id, period, client, used_usd, limit_usd, last_updated)
        VALUES (
            gen_random_uuid(),
            CAST(:scope AS budget.budget_scope),
            CAST(:scope_id AS uuid),
            :period,
            CAST(:client AS varchar),
            :spent,
            COALESCE((
                SELECT max_budget_usd FROM budget.budget_configs
                WHERE scope = CAST(:scope AS budget.budget_scope)
                  AND scope_id = CAST(:scope_id AS uuid)
                  AND client IS NOT DISTINCT FROM CAST(:client AS varchar)
                  AND is_active = true
                ORDER BY effective_from DESC LIMIT 1
            ), 0),
            now()
        )
        ON CONFLICT (scope, scope_id, period, COALESCE(client,''))
        DO UPDATE SET used_usd = EXCLUDED.used_usd, last_updated = now()
        """
    )

    _SEED_SELECT_BEFORE = text(
        """
        SELECT used_usd FROM budget.budget_usages
        WHERE scope = CAST(:scope AS budget.budget_scope)
          AND scope_id = CAST(:scope_id AS uuid)
          AND period = :period
          AND client IS NOT DISTINCT FROM CAST(:client AS varchar)
        """
    )

    async def seed_spent(
        self,
        session: AsyncSession,
        *,
        items: list[SeedSpentItem],
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> SeedSpentResponse:
        """Overwrite-inject absolute spent (USD) per item into DB + Redis.

        Migration burn-rate continuation. Overwrite (not accumulate) → idempotent.
        Per-item partial failure: failed items reported, others proceed.
        """
        results: list[SeedSpentResult] = []
        user_repo = UserRepository(session)

        for item in items:
            scope = item.scope.upper()
            client = item.client
            res = SeedSpentResult(
                scope=scope, scope_id=item.scope_id, client=client,
                period=item.period, status="ok",
            )
            try:
                # --- validation ---
                if scope not in ("USER", "TEAM"):
                    raise ValueError(f"invalid scope: {item.scope}")
                if not _PERIOD_RE.match(item.period):
                    raise ValueError(f"invalid period format: {item.period} (expected YYYY-MM)")
                if client is not None:
                    if scope != "USER":
                        raise ValueError("client is only valid with scope=USER")
                    if client not in _ALLOWED_CLIENTS:
                        raise ValueError(f"invalid client: {client}")
                sid = uuid.UUID(item.scope_id)
                if scope == "USER":
                    if await user_repo.get_user(sid) is None:
                        raise ValueError("user not found")
                else:
                    if await user_repo.get_team(sid) is None:
                        raise ValueError("team not found")

                # --- before value ---
                before_row = await session.execute(
                    self._SEED_SELECT_BEFORE,
                    {"scope": scope, "scope_id": str(sid), "period": item.period, "client": client},
                )
                before = before_row.scalar_one_or_none()
                res.before_usd = Decimal(str(before)) if before is not None else Decimal("0")

                # --- DB upsert (overwrite) ---
                await session.execute(
                    self._SEED_UPSERT,
                    {"scope": scope, "scope_id": str(sid), "period": item.period,
                     "client": client, "spent": item.spent_usd},
                )

                # --- Redis SET (best-effort; DB is source of truth) ---
                redis_key = _redis_usage_key(scope, str(sid), item.period, client)
                try:
                    await self._cache_mgr._redis.set(redis_key, str(item.spent_usd))
                except Exception:
                    logger.warning("seed_spent.redis_set_failed", key=redis_key)

                # Per-app seed must also refresh the total key (client=None) so
                # enforcement sees the correct aggregate.
                if client is not None and scope == "USER":
                    from sqlalchemy import text as sa_text
                    total_row = await session.execute(
                        sa_text(
                            "SELECT COALESCE(SUM(used_usd), 0) "
                            "FROM budget.budget_usages "
                            "WHERE scope = 'USER' AND scope_id = :sid AND period = :period"
                        ),
                        {"sid": str(sid), "period": item.period},
                    )
                    total_val = total_row.scalar() or 0
                    total_key = _redis_usage_key(scope, str(sid), item.period, None)
                    try:
                        await self._cache_mgr._redis.set(total_key, str(total_val))
                    except Exception:
                        logger.warning("seed_spent.redis_total_refresh_failed", key=total_key)

                res.after_usd = item.spent_usd

                await audit_logger.log(
                    session,
                    actor_user_id=actor.user_id,
                    actor_role=actor.role.value,
                    action="SEED_BUDGET_SPENT",
                    resource_type="BudgetUsage",
                    resource_id=f"{scope}:{sid}:{client or ''}:{item.period}",
                    changes={"before": str(res.before_usd), "after": str(item.spent_usd)},
                    ip_address=ip_address,
                    request_id=request_id,
                )
            except Exception as exc:
                res.status = "error"
                res.error = str(exc)
            results.append(res)

        succeeded = sum(1 for r in results if r.status == "ok")
        return SeedSpentResponse(
            total=len(results),
            succeeded=succeeded,
            failed=len(results) - succeeded,
            results=results,
        )

    async def set_team_budget(
        self,
        session: AsyncSession,
        *,
        team_id: uuid.UUID,
        data: SetBudgetRequest,
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> None:
        user_repo = UserRepository(session)
        team = await user_repo.get_team(team_id)
        if team is None:
            raise NotFoundError("Team", str(team_id))

        repo = BudgetRepository(session)
        config = BudgetConfig(
            id=uuid.uuid4(),
            scope=BudgetScope.TEAM,
            scope_id=team_id,
            max_budget_usd=data.max_budget_usd,
            period_type=PeriodType.MONTHLY,
            policy=BudgetPolicy(data.policy.value),
            allocated_by=actor.user_id,
            effective_from=date.today(),
            # ⚠️ migration 0037 이전에는 이 값을 담을 컬럼이 없어서 Redis 설정 키에만
            #    써졌다 — 그 키의 TTL 은 300초이고, 만료되면 gateway-proxy 의 재수화가
            #    기본값으로 되돌렸다. 즉 운영자 설정이 5분만 살아 있었다.
            alert_thresholds=sorted(set(data.alert_thresholds)),
            is_active=True,
        )
        await repo.upsert_config(config)

        await self._cache_mgr.invalidate(
            [f"budget:config:team:{{{team_id}}}"],
            session=session,
        )

        await self._sync_redis_thresholds("team", team_id, data)

        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            action="SET_TEAM_BUDGET",
            resource_type="BudgetConfig",
            resource_id=str(config.id),
            changes={"after": {"team_id": str(team_id), "max_budget_usd": str(data.max_budget_usd), "policy": data.policy.value, "alert_thresholds": data.alert_thresholds}},
            ip_address=ip_address,
            request_id=request_id,
        )

    async def set_user_budget(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        data: SetBudgetRequest,
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> None:
        user_repo = UserRepository(session)
        user = await user_repo.get_user(user_id)
        if user is None:
            raise NotFoundError("User", str(user_id))

        # BR-BUD-03: Team leader can only set budgets for own team
        if actor.role == UserRole.TEAM_LEADER:
            if user.team_id != actor.team_id:
                raise ForbiddenError("Team leaders can only set budgets for their own team members")

        # BR-BUD-01: Validate member budget sum <= team budget
        if user.team_id:
            repo = BudgetRepository(session)
            team_config = await repo.get_active_config(BudgetScope.TEAM, user.team_id)
            if team_config:
                current_sum = await repo.sum_member_budgets(user.team_id)
                # Subtract existing user budget if any
                existing = await repo.get_active_config(BudgetScope.USER, user_id)
                if existing:
                    current_sum -= existing.max_budget_usd
                new_sum = current_sum + data.max_budget_usd
                if new_sum > team_config.max_budget_usd:
                    raise ValidationError(
                        f"Member budget sum ({new_sum}) exceeds team budget ({team_config.max_budget_usd})"
                    )

        repo = BudgetRepository(session)
        config = BudgetConfig(
            id=uuid.uuid4(),
            scope=BudgetScope.USER,
            scope_id=user_id,
            max_budget_usd=data.max_budget_usd,
            period_type=PeriodType.MONTHLY,
            policy=BudgetPolicy(data.policy.value),
            allocated_by=actor.user_id,
            effective_from=date.today(),
            # ⚠️ migration 0037 이전에는 이 값을 담을 컬럼이 없어서 Redis 설정 키에만
            #    써졌다 — 그 키의 TTL 은 300초이고, 만료되면 gateway-proxy 의 재수화가
            #    기본값으로 되돌렸다. 즉 운영자 설정이 5분만 살아 있었다.
            alert_thresholds=sorted(set(data.alert_thresholds)),
            is_active=True,
        )
        await repo.upsert_config(config)

        await self._cache_mgr.invalidate(
            [f"budget:config:user:{{{user_id}}}"],
            session=session,
        )

        await self._sync_redis_thresholds("user", user_id, data)

        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            action="SET_USER_BUDGET",
            resource_type="BudgetConfig",
            resource_id=str(config.id),
            changes={"after": {"user_id": str(user_id), "max_budget_usd": str(data.max_budget_usd), "policy": data.policy.value, "alert_thresholds": data.alert_thresholds}},
            ip_address=ip_address,
            request_id=request_id,
        )

    async def delete_user_budget(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> None:
        repo = BudgetRepository(session)
        existing = await repo.get_active_config(BudgetScope.USER, user_id)
        if existing is None:
            return

        existing.is_active = False
        await session.flush()

        await self._cache_mgr.invalidate(
            [f"budget:config:user:{{{user_id}}}"],
            session=session,
        )

        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            action="DELETE_USER_BUDGET",
            resource_type="BudgetConfig",
            resource_id=str(existing.id),
            changes={"before": {"user_id": str(user_id), "max_budget_usd": str(existing.max_budget_usd)}},
            ip_address=ip_address,
            request_id=request_id,
        )

    async def set_user_client_budget(
        self,
        session,
        *,
        user_id: uuid.UUID,
        client: str,
        data: SetBudgetRequest,
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> None:
        """Set a per-app budget for (user_id, client).

        Guards:
          - client must be one of _ALLOWED_CLIENTS (claude-code / cowork / codex)
          - if the user has a non-empty allowed_clients list, client must be in it
        """
        if client not in _ALLOWED_CLIENTS:
            raise ValueError("invalid client")

        # Verify user exists before any further checks (fail-fast, avoids a
        # wasted allowed_clients query for a non-existent user).
        user_repo = UserRepository(session)
        user = await user_repo.get_user(user_id)
        if user is None:
            raise NotFoundError("User", str(user_id))

        # BR-BUD-03: Team leaders can only set budgets for their own team members.
        if actor.role == UserRole.TEAM_LEADER:
            if user.team_id != actor.team_id:
                raise ForbiddenError("Team leaders can only set budgets for their own team members")

        from app.services.user_allowed_client_service import UserAllowedClientService

        allowed = await UserAllowedClientService(session).get(user_id)
        if allowed and client not in allowed:
            raise ValueError(f"client '{client}' not allowed for this user")

        repo = BudgetRepository(session)

        # P0-③ invariant: a per-app budget is an additive SUB-limit UNDER the
        # user's total budget (see README). It is only enforced on the gateway
        # hot path via the parent USER-config's app_clients gate, so a per-app
        # budget without a parent USER total budget would be silently bypassed.
        # Reject it here to keep the documented invariant (parent must exist).
        parent = await repo.get_active_config(BudgetScope.USER, user_id)
        if parent is None:
            raise ValueError(
                "cannot set a per-app budget before the user's total budget is set "
                "(per-app budget is a sub-limit of the user total)"
            )

        config = BudgetConfig(
            id=uuid.uuid4(),
            scope=BudgetScope.USER,
            scope_id=user_id,
            client=client,
            max_budget_usd=data.max_budget_usd,
            period_type=PeriodType.MONTHLY,
            policy=BudgetPolicy(data.policy.value),
            allocated_by=actor.user_id,
            effective_from=date.today(),
            # ⚠️ migration 0037 이전에는 이 값을 담을 컬럼이 없어서 Redis 설정 키에만
            #    써졌다 — 그 키의 TTL 은 300초이고, 만료되면 gateway-proxy 의 재수화가
            #    기본값으로 되돌렸다. 즉 운영자 설정이 5분만 살아 있었다.
            alert_thresholds=sorted(set(data.alert_thresholds)),
            is_active=True,
        )
        await repo.upsert_config(config)

        # P0-③ durability: DURABLY invalidate (DEL via retry infra → recorded to
        # cache_invalidation_failures on failure) BOTH the per-app config key AND
        # the parent user-config key (whose app_clients list just changed). The
        # subsequent _sync_redis_app_config/_refresh_user_app_clients SETs are
        # best-effort cache-warmers ONLY; if they fail, the durable DEL guarantees
        # the gateway sees a miss and rehydrates from DB (ensure_config_cached),
        # rather than enforcing a stale app_clients that silently bypasses the
        # per-app limit forever.
        await self._cache_mgr.invalidate(
            [
                f"budget:config:user:{{{user_id}}}:{client}",
                f"budget:config:user:{{{user_id}}}",
            ],
            session=session,
        )

        await self._sync_redis_app_config(user_id, client, data)
        await self._refresh_user_app_clients(session, user_id)

        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            action="SET_USER_CLIENT_BUDGET",
            resource_type="BudgetConfig",
            resource_id=str(config.id),
            changes={
                "after": {
                    "user_id": str(user_id),
                    "client": client,
                    "max_budget_usd": str(data.max_budget_usd),
                    "policy": data.policy.value,
                    "alert_thresholds": data.alert_thresholds,
                }
            },
            ip_address=ip_address,
            request_id=request_id,
        )

    async def clear_user_client_budget(
        self,
        session,
        *,
        user_id: uuid.UUID,
        client: str,
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> None:
        """Deactivate the per-app budget for (user_id, client) and clean up Redis."""
        if client not in _ALLOWED_CLIENTS:
            raise ValueError("invalid client")

        # BR-BUD-03: Team leaders can only clear budgets for their own team members.
        user_repo = UserRepository(session)
        user = await user_repo.get_user(user_id)
        if user is None:
            raise NotFoundError("User", str(user_id))
        if actor.role == UserRole.TEAM_LEADER:
            if user.team_id != actor.team_id:
                raise ForbiddenError("Team leaders can only set budgets for their own team members")

        repo = BudgetRepository(session)
        existing = await repo.get_active_app_config(BudgetScope.USER, user_id, client)
        if existing is None:
            return

        existing.is_active = False
        await session.flush()

        # P0-③ durability: durably DEL both the per-app key and the parent
        # user-config key (app_clients list shrank). See set_user_client_budget.
        await self._cache_mgr.invalidate(
            [
                f"budget:config:user:{{{user_id}}}:{client}",
                f"budget:config:user:{{{user_id}}}",
            ],
            session=session,
        )

        await self._delete_redis_app_config(user_id, client)
        await self._refresh_user_app_clients(session, user_id)

        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            action="CLEAR_USER_CLIENT_BUDGET",
            resource_type="BudgetConfig",
            resource_id=str(existing.id),
            changes={
                "before": {
                    "user_id": str(user_id),
                    "client": client,
                    "max_budget_usd": str(existing.max_budget_usd),
                }
            },
            ip_address=ip_address,
            request_id=request_id,
        )

    async def get_user_app_budgets(self, session, *, user_id: uuid.UUID, actor) -> list[dict]:
        """Return active per-app budget configs for a user (read-only, for UI prefill).

        BR-BUD-03: team leaders may only read budgets for their own team members.
        """
        user_repo = UserRepository(session)
        user = await user_repo.get_user(user_id)
        if user is None:
            raise NotFoundError("User", str(user_id))
        if actor.role == UserRole.TEAM_LEADER and user.team_id != actor.team_id:
            raise ForbiddenError("Team leaders can only read budgets for their own team members")

        repo = BudgetRepository(session)
        out = []
        for client in _ALLOWED_CLIENTS:
            cfg = await repo.get_first_active_app_config(BudgetScope.USER, user_id, client)
            if cfg is not None:
                out.append({
                    "client": client,
                    "max_budget_usd": cfg.max_budget_usd,
                    "policy": cfg.policy,  # DB enum; pydantic coerces via BudgetPolicy
                })
        return out

    async def allocate_team_budget(
        self,
        session: AsyncSession,
        *,
        team_id: uuid.UUID,
        data: AllocateBudgetRequest,
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> None:
        # BR-BUD-03: Team leader can only allocate within own team
        if actor.role == UserRole.TEAM_LEADER and actor.team_id != team_id:
            raise ForbiddenError("Team leaders can only allocate budgets within their own team")

        repo = BudgetRepository(session)
        team_config = await repo.get_active_config(BudgetScope.TEAM, team_id)
        if team_config is None:
            raise ValidationError("Team budget must be set before allocation")

        # BR-BUD-01: Validate total allocation <= team budget
        total_allocation = sum(a.allocated_usd for a in data.allocations)
        if total_allocation > team_config.max_budget_usd:
            raise ValidationError(
                f"Total allocation ({total_allocation}) exceeds team budget ({team_config.max_budget_usd})"
            )

        # Batch upsert user budgets
        cache_keys: list[str] = []
        for alloc in data.allocations:
            uid = uuid.UUID(alloc.user_id)
            config = BudgetConfig(
                id=uuid.uuid4(),
                scope=BudgetScope.USER,
                scope_id=uid,
                max_budget_usd=alloc.allocated_usd,
                period_type=PeriodType.MONTHLY,
                policy=team_config.policy,
                allocated_by=actor.user_id,
                effective_from=date.today(),
                # 팀에서 파생된 사용자 예산은 팀의 임계값을 물려받는다 — 여기서 기본값을
                # 다시 쓰면 팀 설정과 어긋난 알림이 나간다.
                alert_thresholds=list(team_config.alert_thresholds or []),
                is_active=True,
            )
            await repo.upsert_config(config)
            cache_keys.append(f"budget:config:user:{{{uid}}}")

        await self._cache_mgr.invalidate(cache_keys, session=session)

        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            action="ALLOCATE_TEAM_BUDGET",
            resource_type="BudgetConfig",
            resource_id=str(team_id),
            changes={"after": {"allocations": [a.model_dump() for a in data.allocations]}},
            ip_address=ip_address,
            request_id=request_id,
        )

    async def get_team_allocation(
        self,
        session: AsyncSession,
        *,
        team_id: uuid.UUID,
        period: str,
    ) -> TeamBudgetAllocation | None:
        user_repo = UserRepository(session)
        team = await user_repo.get_team(team_id)
        if team is None:
            return None

        repo = BudgetRepository(session)
        team_config = await repo.get_first_active_config(BudgetScope.TEAM, team_id)
        total_budget = team_config.max_budget_usd if team_config else Decimal("0")

        # Team-level entry
        team_usage = await repo.get_usage(BudgetScope.TEAM, team_id, period)
        team_used = team_usage.used_usd if team_usage else Decimal("0")
        team_remaining = total_budget - team_used
        team_pct = (team_used / total_budget * 100) if total_budget > 0 else Decimal("0")

        def _alert_level(pct: Decimal) -> str:
            if pct >= 90:
                return "CRITICAL"
            if pct >= 70:
                return "WARNING"
            return "NORMAL"

        entries: list[AllocationEntry] = [
            AllocationEntry(
                target_id=str(team_id),
                target_name=team.name,
                target_type="TEAM",
                allocated_usd=total_budget,
                used_usd=team_used,
                remaining_usd=team_remaining,
                alert_level=_alert_level(team_pct),
            )
        ]

        # Member entries
        for member in team.members:
            member_config = await repo.get_first_active_config(BudgetScope.USER, member.id)
            member_alloc = member_config.max_budget_usd if member_config else Decimal("0")
            member_usage = await repo.get_usage(BudgetScope.USER, member.id, period)
            member_used = member_usage.used_usd if member_usage else Decimal("0")
            member_remaining = member_alloc - member_used
            member_pct = (member_used / member_alloc * 100) if member_alloc > 0 else Decimal("0")
            entries.append(
                AllocationEntry(
                    target_id=str(member.id),
                    target_name=member.display_name,
                    target_type="USER",
                    allocated_usd=member_alloc,
                    used_usd=member_used,
                    remaining_usd=member_remaining,
                    alert_level=_alert_level(member_pct),
                )
            )

        return TeamBudgetAllocation(
            team_id=str(team_id),
            team_name=team.name,
            total_budget_usd=total_budget,
            entries=entries,
        )

    async def get_budget_summary(
        self,
        session: AsyncSession,
        *,
        redis=None,
        scope: str | None = None,
        target_id: uuid.UUID | None = None,
        period: str,
    ) -> BudgetSummaryResponse:
        if not re.match(r'^\d{4}-\d{2}$', period):
            raise ValidationError(f"Invalid period format: {period}. Expected YYYY-MM")

        repo = BudgetRepository(session)
        budget_scope = BudgetScope(scope.upper()) if scope else None

        # Active configs indexed by (scope, scope_id_str). Users/teams without an
        # active config are still listed (limit=0) so admins can set/re-set budgets
        # — e.g., right after transfer_user deactivates the user-scope config.
        active_configs = await repo.list_configs(scope=budget_scope, scope_id=target_id)
        cfg_by_target: dict[tuple[BudgetScope, str], BudgetConfig] = {
            (cfg.scope, str(cfg.scope_id)): cfg for cfg in active_configs
        }

        from app.repositories.user_repository import UserRepository
        user_repo = UserRepository(session)
        # ⚠️ limit=500 이었다. `list_users` 는 created_at desc 로 정렬한 뒤 앞에서
        #    자르므로, 가입이 오래된 사용자의 예산 행이 **조용히 빠진 채** 사용률이
        #    계산됐다(오류 없이 틀린 비율). 커서 페이징으로 우회할 수도 없다 —
        #    정렬 키(created_at)와 커서 키(id)가 달라 행을 건너뛴다. 전수 조회를 쓴다.
        users = await user_repo.iter_all_users()
        teams = await user_repo.list_all_teams()

        # team_id(str) → (dept_id, dept_name). teams are loaded with
        # selectinload(Team.department), so this needs no extra query.
        dept_by_team: dict[str, tuple[str, str]] = {
            str(t.id): (str(t.department.id), t.department.name)
            for t in teams
            if getattr(t, "department", None) is not None
        }

        target_id_str = str(target_id) if target_id else None

        async def _resolve_used(cfg: BudgetConfig) -> Decimal:
            sid = str(cfg.scope_id)
            scope_type = cfg.scope.value.lower()
            used = Decimal("0")
            if redis is not None:
                redis_key = f"budget:{scope_type}:{{{sid}}}:{period}"
                try:
                    raw = await redis.get(redis_key)
                    if raw:
                        used = Decimal(raw.decode() if isinstance(raw, bytes) else raw)
                except Exception:
                    pass
            if used == 0:
                from sqlalchemy import func, select as sa_select
                from app.models.usage import UsageLog
                from app.core.usage_filters import cost_period_filter
                col = UsageLog.user_id if cfg.scope == BudgetScope.USER else UsageLog.team_id
                # 비용 집계 표준(§59): SUCCESS 만 + KST 월 경계. 대시보드 Top 사용자/팀·
                # chat 과 동일 기준으로 통일(실패 호출 비용 제외, UTC 9시간 오차 제거).
                stmt = sa_select(func.coalesce(func.sum(UsageLog.cost_usd), 0)).where(
                    col == cfg.scope_id,
                    cost_period_filter(period),
                )
                result = await session.execute(stmt)
                used = Decimal(str(result.scalar_one()))
            return used

        items: list[BudgetSummaryItem] = []

        async def _append(
            scope_enum: BudgetScope,
            sid: str,
            name: str,
            team_id: str | None = None,
            is_active: bool = True,
            department_id: str | None = None,
            department_name: str | None = None,
        ) -> None:
            cfg = cfg_by_target.get((scope_enum, sid))
            if cfg is not None:
                used = await _resolve_used(cfg)
                limit = cfg.max_budget_usd
                remaining = limit - used
                pct = (used / limit * 100) if limit > 0 else Decimal("0")
                thresholds = list(cfg.alert_thresholds or [])
            else:
                used = Decimal("0")
                limit = None
                remaining = None
                pct = None
                thresholds = None
            items.append(
                BudgetSummaryItem(
                    target_type=scope_enum.value.lower(),
                    target_id=sid,
                    target_name=name,
                    team_id=team_id,
                    is_active=is_active,
                    limit_usd=limit,
                    used_usd=used,
                    remaining_usd=remaining,
                    usage_pct=pct,
                    department_id=department_id,
                    department_name=department_name,
                    alert_thresholds=thresholds,
                )
            )

        if budget_scope is None or budget_scope == BudgetScope.USER:
            for u in users:
                uid = str(u.id)
                if target_id_str and uid != target_id_str:
                    continue
                u_team_id = getattr(u, "team_id", None)
                u_dept = dept_by_team.get(str(u_team_id)) if u_team_id else None
                await _append(
                    BudgetScope.USER,
                    uid,
                    u.display_name or u.email,
                    team_id=str(u_team_id) if u_team_id else None,
                    is_active=u.is_active,
                    department_id=u_dept[0] if u_dept else None,
                    department_name=u_dept[1] if u_dept else None,
                )

        if budget_scope is None or budget_scope == BudgetScope.TEAM:
            for t in teams:
                tid = str(t.id)
                if target_id_str and tid != target_id_str:
                    continue
                has_active_members = any(m.is_active for m in t.members)
                t_dept = dept_by_team.get(tid)
                await _append(
                    BudgetScope.TEAM,
                    tid,
                    t.name,
                    is_active=has_active_members,
                    department_id=t_dept[0] if t_dept else None,
                    department_name=t_dept[1] if t_dept else None,
                )

        return BudgetSummaryResponse(period=period, summary=items)

    async def _write_team_config_cache(
        self,
        scope_id: uuid.UUID,
        max_budget_usd: Decimal,
        policy: BudgetPolicy,
        alert_thresholds: list[int],
    ) -> None:
        """budget:config:team:{<scope_id>} 를 Redis에 SET.

        budget_check.lua 가 기대하는 JSON shape:
          limit_usd, policy (lowercase), thresholds
        Lua / gateway-proxy 기본값(soft_limit_pct, throttle_rpm_pct)은
        DB 스키마에 없으므로 Python 기본값은 포함하지 않음 — Lua 내 기본값 사용.
        """
        import json
        try:
            redis = self._cache_mgr._redis
            config_key = f"budget:config:team:{{{scope_id}}}"
            config_data = {
                "limit_usd": str(max_budget_usd),
                "policy": policy.value.lower(),
                "thresholds": sorted(alert_thresholds),
            }
            await redis.set(config_key, json.dumps(config_data), ex=BUDGET_CONFIG_CACHE_TTL)
        except Exception:
            logger.warning("redis_team_config_cache_write_failed", scope_id=str(scope_id))

    async def _sync_redis_thresholds(
        self, scope_type: str, scope_id: uuid.UUID, data: SetBudgetRequest
    ) -> None:
        import json
        try:
            redis = self._cache_mgr._redis
            config_key = f"budget:config:{scope_type}:{{{scope_id}}}"
            # budget_check.lua / budget_deduct.lua compare policy against lowercase
            # constants ('hard_block', 'soft_warning', 'throttle'). admin-api's
            # BudgetPolicy enum stores UPPERCASE values; convert here so enforcement
            # actually fires on the Redis fast path. Other admin-api paths that
            # write this key (cli_service, internal.py) already use .lower().
            config_data = {
                "limit_usd": str(data.max_budget_usd),
                "policy": data.policy.value.lower(),
                "thresholds": sorted(data.alert_thresholds),
            }
            # ⚠️ app_clients 보존을 **애플리케이션에서** GET-modify-SET 으로 하면 안 된다.
            #    `await redis.get` 이 이벤트 루프를 양보하므로 uvicorn 워커 하나 안에서도
            #    두 요청(set_user_budget / set_user_client_budget /
            #    clear_user_client_budget)이 교차하고, 나중에 SET 하는 쪽이 상대의 필드를
            #    지운다. 그리고 그 손실은 조용하다 — budget_check.lua 는 없는 필드를
            #    빈 테이블로 읽고, 게이트웨이는 앱별 예산 평가를 통째로 건너뛴다.
            #    Lua 로 Redis 안에서 병합한다(core/budget_cache.py).
            if scope_type == "user":
                await write_user_budget_config(
                    redis, scope_id, config_data, BUDGET_CONFIG_CACHE_TTL
                )
            else:
                await redis.set(
                    config_key, json.dumps(config_data), ex=BUDGET_CONFIG_CACHE_TTL
                )
        except Exception:
            logger.warning("redis_threshold_sync_failed", scope_type=scope_type, scope_id=str(scope_id))

    async def _sync_redis_app_config(
        self, user_id: uuid.UUID, client: str, data: SetBudgetRequest
    ) -> None:
        """Write the per-app Redis config key that the gateway Lua reads.

        Key: budget:config:user:{<user_id>}:{client}
        Shape: {limit_usd, policy (lowercase), thresholds}
        """
        import json
        try:
            redis = self._cache_mgr._redis
            config_key = f"budget:config:user:{{{user_id}}}:{client}"
            config_data = {
                "limit_usd": str(data.max_budget_usd),
                "policy": data.policy.value.lower(),
                "thresholds": sorted(data.alert_thresholds),
            }
            await redis.set(config_key, json.dumps(config_data), ex=BUDGET_CONFIG_CACHE_TTL)
        except Exception:
            logger.warning(
                "redis_app_config_sync_failed", user_id=str(user_id), client=client
            )

    async def _delete_redis_app_config(self, user_id: uuid.UUID, client: str) -> None:
        """Delete the per-app Redis config key (on clear)."""
        try:
            redis = self._cache_mgr._redis
            config_key = f"budget:config:user:{{{user_id}}}:{client}"
            await redis.delete(config_key)
        except Exception:
            logger.warning(
                "redis_app_config_delete_failed", user_id=str(user_id), client=client
            )

    async def _refresh_user_app_clients(
        self, session, user_id: uuid.UUID
    ) -> None:
        """Keep the user-config JSON's app_clients list in sync with DB.

        Reads active per-app BudgetConfig rows, then updates the user-config
        Redis key's app_clients field WITHOUT clobbering other fields.
        If the user-config key is absent, does nothing (gateway re-derives on miss).
        """
        try:
            repo = BudgetRepository(session)
            active_clients = await repo.list_active_app_clients(user_id)

            # ⚠️ 여기가 가장 아픈 GET-modify-SET 이었다. 이 쓰기는 **새로 추가된
            #    client 를 싣는** 쓰기이므로, 경합에서 지면 단순 staleness 가 아니라
            #    그 앱의 예산 한도가 적용되지 않는 상태가 된다(우회).
            #    키가 없으면 아무것도 하지 않는다 — 총액 필드를 모르는 채로 키를 만들면
            #    게이트웨이가 한도 없는 설정으로 읽는다. 게이트웨이가 DB 에서 재도출한다.
            await refresh_user_app_clients(
                self._cache_mgr._redis, user_id, active_clients, BUDGET_CONFIG_CACHE_TTL
            )
        except Exception:
            logger.warning("redis_refresh_user_app_clients_failed", user_id=str(user_id))

    async def warm_team_budget_cache(self, session: AsyncSession) -> int:
        """startup 시 활성 TEAM 예산 설정을 Redis에 일괄 동기화.

        admin-api 시작 전 init SQL / alembic backfill 로 DB에 삽입된
        TEAM BudgetConfig 행이 Redis에 존재하지 않아 gateway-proxy 가
        team_budget_unset 429 를 반환하는 cold-cache 문제를 봉합한다.

        Returns:
            synced 건수
        """
        repo = BudgetRepository(session)
        configs = await repo.list_configs(scope=BudgetScope.TEAM)
        count = 0
        for cfg in configs:
            await self._write_team_config_cache(
                scope_id=cfg.scope_id,
                max_budget_usd=cfg.max_budget_usd,
                policy=cfg.policy,
                # migration 0037 이후 DB 가 진실의 원천이다(예전 하드코딩은 운영자
                # 설정을 워밍업이 덮어쓰게 만들었다).
                alert_thresholds=list(cfg.alert_thresholds or []),
            )
            count += 1
        logger.info("team_budget_cache.warmed", count=count)
        return count

    async def detect_orphan_app_budgets(self, session: AsyncSession) -> int:
        """startup 시 '부모 USER 총예산 없는 per-app 예산'(orphan) 행을 탐지·로깅.

        P0-③ review(MF4): 신규 orphan 은 set_user_client_budget 가드가 막지만,
        가드 도입 이전에 생성된 **기존 orphan 행**은 gateway hot path 에서 여전히
        우회된다(app_clients 게이트가 부모 config 를 읽으므로). 자동 마이그레이션은
        위험(임의로 부모 예산을 만들거나 per-app 을 끄는 건 정책 결정)하므로,
        여기서는 **read-only 로 탐지해 WARN 로그**만 남겨 운영자가 수동 조치하게 한다.

        Returns: orphan 건수 (0 이면 clean).
        """
        from sqlalchemy import text as _text

        result = await session.execute(
            _text(
                """
                SELECT c.scope_id, c.client
                FROM budget.budget_configs c
                WHERE c.scope = 'USER' AND c.client IS NOT NULL AND c.is_active = true
                  AND NOT EXISTS (
                    SELECT 1 FROM budget.budget_configs p
                    WHERE p.scope = 'USER' AND p.scope_id = c.scope_id
                      AND p.client IS NULL AND p.is_active = true
                  )
                """
            )
        )
        orphans = result.fetchall()
        if orphans:
            logger.warning(
                "orphan_app_budgets_detected",
                count=len(orphans),
                note="per-app budgets without a parent USER total budget bypass the "
                "gateway hot path; set a parent USER budget or clear these per-app rows",
                samples=[(str(r[0]), r[1]) for r in orphans[:20]],
            )
        else:
            logger.info("orphan_app_budgets_none")
        return len(orphans)

    # ── Auto-Downgrade Config ──

    async def get_downgrade_config(
        self,
        session: AsyncSession,
        *,
        scope: BudgetScope,
        scope_id: uuid.UUID,
    ) -> AutoDowngradeConfigResponse:
        rule_repo = DowngradePolicyRepository(session)
        rules = await rule_repo.get_rules(scope, scope_id)

        return AutoDowngradeConfigResponse(
            scope=scope.value,
            scope_id=str(scope_id),
            enabled=len(rules) > 0,
            rules=[
                DowngradeRuleResponse(
                    id=str(r.id),
                    from_model_alias=r.from_model_alias,
                    to_model_alias=r.to_model_alias,
                    threshold_pct=r.threshold_pct,
                    is_active=r.is_active,
                    created_at=r.created_at.isoformat(),
                )
                for r in rules
            ],
        )

    async def set_downgrade_config(
        self,
        session: AsyncSession,
        *,
        scope: BudgetScope,
        scope_id: uuid.UUID,
        data: AutoDowngradeConfigRequest,
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> AutoDowngradeConfigResponse:
        from app.repositories.model_repository import ModelRepository

        model_repo = ModelRepository(session)
        all_aliases = {alias for rule in data.rules for alias in (rule.from_model_alias, rule.to_model_alias)}
        # ⚠️ 행을 버리지 말고 들고 있는다 — 아래 provider 대조에 필요하다(존재 확인만 하고
        #    버리면 alias 당 조회를 두 번 하게 된다).
        alias_rows: dict[str, object] = {}
        for alias in all_aliases:
            row = await model_repo.get_by_alias(alias)
            if row is None:
                raise NotFoundError("ModelAlias", alias)
            alias_rows[alias] = row

        for rule in data.rules:
            if rule.from_model_alias == rule.to_model_alias:
                raise ValidationError(f"Source and target model cannot be the same: {rule.from_model_alias}")

            # ⚠️ provider 가 다른 규칙은 **저장 자체를 거부한다.**
            #
            #    강등은 요청 본문의 model 을 그대로 바꿔치기한다. 그런데 각 라우트는 자기
            #    provider 로 필터해서 alias 를 해석한다 — /v1/messages 는
            #    resolve_bedrock_model(provider == BEDROCK)이다. 그래서 BEDROCK alias 를
            #    BEDROCK_MANTLE/RUNTIME_OPENAI alias 로 바꾸는 규칙은 임계값을 넘는 순간
            #    LookupError → **404** 가 되고, 그 스코프의 모든 사용자가 한꺼번에 끊긴다.
            #    비용 절감 설정이 팀을 오프라인으로 만드는 것이고, 404 본문에는 강등 규칙이
            #    원인이라는 단서가 없다.
            #
            #    반대 방향(mantle → runtime plane)은 더 조용하고 더 나쁘다: 두 provider 가
            #    같은 리졸버를 통과하므로 HTTP 200 인 채로 인증 방식(bearer vs SigV4), 단가,
            #    AWS 쪽 invocation 로깅이 함께 바뀐다.
            #
            #    저장 시점이 막을 수 있는 유일한 지점이다 — 요청 시점에는 이미 늦었고
            #    (그 요청은 실패한다) 화면은 200 을 받은 뒤다.
            from_provider = getattr(alias_rows[rule.from_model_alias], "provider", None)
            to_provider = getattr(alias_rows[rule.to_model_alias], "provider", None)
            if from_provider != to_provider:
                raise ValidationError(
                    f"Downgrade target must use the same provider as the source: "
                    f"'{rule.from_model_alias}' is {getattr(from_provider, 'value', from_provider)} "
                    f"but '{rule.to_model_alias}' is {getattr(to_provider, 'value', to_provider)}. "
                    f"A cross-provider rewrite does not resolve on the serving route, so every "
                    f"request in this scope would fail once the threshold is crossed."
                )

        budget_repo = BudgetRepository(session)
        config = await budget_repo.get_first_active_config(scope, scope_id)
        if config is None:
            raise ValidationError("Budget must be configured before setting downgrade rules")
        if config.max_budget_usd <= 0:
            raise ValidationError("Budget max_budget_usd must be greater than 0 for downgrade rules")

        rule_repo = DowngradePolicyRepository(session)
        new_rules = [
            DowngradePolicy(
                scope=scope,
                scope_id=scope_id,
                from_model_alias=r.from_model_alias,
                to_model_alias=r.to_model_alias,
                threshold_pct=r.threshold_pct,
                is_active=True,
                created_by=actor.user_id,
            )
            for r in data.rules
        ]
        await rule_repo.set_rules(scope, scope_id, new_rules)

        cache_key = f"budget:downgrade:{scope.value.lower()}:{scope_id}"
        await self._cache_mgr.invalidate([cache_key], session=session)

        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            action="SET_AUTO_DOWNGRADE",
            resource_type="DowngradePolicy",
            resource_id=str(scope_id),
            changes={"after": {
                "enabled": data.enabled,
                "rules": [r.model_dump() for r in data.rules],
            }},
            ip_address=ip_address,
            request_id=request_id,
        )

        return await self.get_downgrade_config(session, scope=scope, scope_id=scope_id)

    async def delete_downgrade_config(
        self,
        session: AsyncSession,
        *,
        scope: BudgetScope,
        scope_id: uuid.UUID,
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> None:
        rule_repo = DowngradePolicyRepository(session)
        await rule_repo.delete_rules(scope, scope_id)

        cache_key = f"budget:downgrade:{scope.value.lower()}:{scope_id}"
        await self._cache_mgr.invalidate([cache_key], session=session)

        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            action="DELETE_AUTO_DOWNGRADE",
            resource_type="DowngradePolicy",
            resource_id=str(scope_id),
            changes={"after": {"disabled": True}},
            ip_address=ip_address,
            request_id=request_id,
        )
