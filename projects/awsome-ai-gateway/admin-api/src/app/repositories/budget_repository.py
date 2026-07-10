# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.budget import BudgetConfig, BudgetScope, BudgetUsage, DowngradePolicy


class BudgetRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_config(self, config: BudgetConfig) -> BudgetConfig:
        # Deactivate existing for same scope+scope_id+(client if given)
        stmt = update(BudgetConfig).where(
            BudgetConfig.scope == config.scope,
            BudgetConfig.scope_id == config.scope_id,
            BudgetConfig.is_active.is_(True),
        )
        if config.client is not None:
            stmt = stmt.where(BudgetConfig.client == config.client)
        else:
            stmt = stmt.where(BudgetConfig.client.is_(None))
        stmt = stmt.values(is_active=False)
        await self._session.execute(stmt)
        self._session.add(config)
        await self._session.flush()
        return config

    async def get_active_config(self, scope: BudgetScope, scope_id: uuid.UUID) -> BudgetConfig | None:
        stmt = select(BudgetConfig).where(
            BudgetConfig.scope == scope,
            BudgetConfig.scope_id == scope_id,
            BudgetConfig.client.is_(None),
            BudgetConfig.is_active.is_(True),
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_active_app_config(
        self, scope: BudgetScope, scope_id: uuid.UUID, client: str
    ) -> BudgetConfig | None:
        """Return the active per-app BudgetConfig for (scope, scope_id, client)."""
        stmt = select(BudgetConfig).where(
            BudgetConfig.scope == scope,
            BudgetConfig.scope_id == scope_id,
            BudgetConfig.client == client,
            BudgetConfig.is_active.is_(True),
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_first_active_app_config(
        self, scope: BudgetScope, scope_id: uuid.UUID, client: str
    ) -> BudgetConfig | None:
        """Like get_active_app_config but tolerates duplicate active rows (read path)."""
        stmt = (
            select(BudgetConfig)
            .where(
                BudgetConfig.scope == scope,
                BudgetConfig.scope_id == scope_id,
                BudgetConfig.client == client,
                BudgetConfig.is_active.is_(True),
            )
            .order_by(BudgetConfig.created_at.desc())
            .limit(1)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def deactivate_app_config(
        self, scope: BudgetScope, scope_id: uuid.UUID, client: str
    ) -> int:
        """Deactivate the active per-app BudgetConfig for (scope, scope_id, client)."""
        stmt = (
            update(BudgetConfig)
            .where(
                BudgetConfig.scope == scope,
                BudgetConfig.scope_id == scope_id,
                BudgetConfig.client == client,
                BudgetConfig.is_active.is_(True),
            )
            .values(is_active=False)
        )
        result = await self._session.execute(stmt)
        return result.rowcount  # type: ignore[return-value]

    async def list_active_app_clients(self, user_id: uuid.UUID) -> list[str]:
        """Return client values of active per-app BudgetConfig rows for user_id."""
        from sqlalchemy import distinct

        stmt = select(distinct(BudgetConfig.client)).where(
            BudgetConfig.scope == BudgetScope.USER,
            BudgetConfig.scope_id == user_id,
            BudgetConfig.client.is_not(None),
            BudgetConfig.is_active.is_(True),
        )
        result = await self._session.execute(stmt)
        return [row[0] for row in result.fetchall()]

    async def get_first_active_config(self, scope: BudgetScope, scope_id: uuid.UUID) -> BudgetConfig | None:
        """Like get_active_config but tolerates duplicate rows. Excludes per-app rows."""
        stmt = (
            select(BudgetConfig)
            .where(
                BudgetConfig.scope == scope,
                BudgetConfig.scope_id == scope_id,
                BudgetConfig.client.is_(None),
                BudgetConfig.is_active.is_(True),
            )
            .order_by(BudgetConfig.created_at.desc())
            .limit(1)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def deactivate_configs(self, scope: BudgetScope, scope_id: uuid.UUID) -> int:
        """Deactivate total-budget configs (client IS NULL) for scope+scope_id."""
        stmt = (
            update(BudgetConfig)
            .where(
                BudgetConfig.scope == scope,
                BudgetConfig.scope_id == scope_id,
                BudgetConfig.client.is_(None),
                BudgetConfig.is_active.is_(True),
            )
            .values(is_active=False)
        )
        result = await self._session.execute(stmt)
        return result.rowcount  # type: ignore[return-value]

    async def sum_member_budgets(self, team_id: uuid.UUID) -> Decimal:
        """Sum of active USER total budgets (client IS NULL) for members of a team.

        Per-app rows (client IS NOT NULL) are intentionally excluded — the team
        cap check (BR-BUD-01) compares the per-user *total* budget, not per-app
        sub-limits, so including them would double-count and cause false rejections.
        """
        from app.models.auth import User

        stmt = (
            select(BudgetConfig)
            .join(User, BudgetConfig.scope_id == User.id)
            .where(
                BudgetConfig.scope == BudgetScope.USER,
                BudgetConfig.client.is_(None),
                BudgetConfig.is_active.is_(True),
                User.team_id == team_id,
            )
        )
        result = await self._session.execute(stmt)
        configs = result.scalars().all()
        return sum((c.max_budget_usd for c in configs), Decimal("0"))

    async def get_usage(self, scope: BudgetScope, scope_id: uuid.UUID, period: str) -> BudgetUsage | None:
        stmt = select(BudgetUsage).where(
            BudgetUsage.scope == scope,
            BudgetUsage.scope_id == scope_id,
            BudgetUsage.period == period,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_configs(
        self,
        scope: BudgetScope | None = None,
        scope_id: uuid.UUID | None = None,
    ) -> list[BudgetConfig]:
        """Return active total-budget configs (client IS NULL only).

        Per-app rows are excluded here because all callers (get_budget_summary,
        warm_team_budget_cache) key results on (scope, scope_id) — a per-app row
        would silently collide with the total row in that dict and corrupt the
        budget summary dashboard.
        """
        stmt = select(BudgetConfig).where(
            BudgetConfig.is_active.is_(True),
            BudgetConfig.client.is_(None),
        )
        if scope:
            stmt = stmt.where(BudgetConfig.scope == scope)
        if scope_id:
            stmt = stmt.where(BudgetConfig.scope_id == scope_id)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_configs_with_usage(
        self,
        scope: BudgetScope | None = None,
        scope_id: uuid.UUID | None = None,
        period: str | None = None,
    ) -> list[tuple[BudgetConfig, BudgetUsage | None]]:
        stmt = select(BudgetConfig).where(BudgetConfig.is_active.is_(True))
        if scope:
            stmt = stmt.where(BudgetConfig.scope == scope)
        if scope_id:
            stmt = stmt.where(BudgetConfig.scope_id == scope_id)
        result = await self._session.execute(stmt)
        configs = result.scalars().all()

        pairs: list[tuple[BudgetConfig, BudgetUsage | None]] = []
        for cfg in configs:
            usage = None
            if period:
                usage = await self.get_usage(cfg.scope, cfg.scope_id, period)
            pairs.append((cfg, usage))
        return pairs


class DowngradePolicyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_rules(self, scope: BudgetScope, scope_id: uuid.UUID) -> list[DowngradePolicy]:
        stmt = (
            select(DowngradePolicy)
            .where(
                DowngradePolicy.scope == scope,
                DowngradePolicy.scope_id == scope_id,
                DowngradePolicy.is_active.is_(True),
            )
            .order_by(DowngradePolicy.threshold_pct)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def set_rules(
        self, scope: BudgetScope, scope_id: uuid.UUID, rules: list[DowngradePolicy]
    ) -> list[DowngradePolicy]:
        await self._session.execute(
            update(DowngradePolicy)
            .where(
                DowngradePolicy.scope == scope,
                DowngradePolicy.scope_id == scope_id,
                DowngradePolicy.is_active.is_(True),
            )
            .values(is_active=False)
        )
        await self._session.flush()
        for rule in rules:
            self._session.add(rule)
        await self._session.flush()
        return rules

    async def delete_rules(self, scope: BudgetScope, scope_id: uuid.UUID) -> int:
        stmt = (
            update(DowngradePolicy)
            .where(
                DowngradePolicy.scope == scope,
                DowngradePolicy.scope_id == scope_id,
                DowngradePolicy.is_active.is_(True),
            )
            .values(is_active=False)
        )
        result = await self._session.execute(stmt)
        return result.rowcount  # type: ignore[return-value]
