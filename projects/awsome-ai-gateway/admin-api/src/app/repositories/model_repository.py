# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.model import (
    ModelAlias,
    ModelPricing,
    ModelStatus,
    RateLimitConfig,
    RateLimitScope,
    TeamAllowedModel,
)
from app.repositories._locks import advisory_xact_lock


class ModelRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── ModelAlias ──

    async def create_model(self, model: ModelAlias) -> ModelAlias:
        self._session.add(model)
        await self._session.flush()
        return model

    async def get_by_alias(self, alias: str) -> ModelAlias | None:
        return await self._session.get(ModelAlias, alias)

    async def alias_exists_ci(self, alias: str) -> bool:
        stmt = select(ModelAlias).where(func.lower(ModelAlias.alias) == alias.lower())
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def list_all(self) -> list[ModelAlias]:
        stmt = select(ModelAlias).order_by(ModelAlias.alias)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_alias_names(self, *, status: ModelStatus | None = None) -> list[str]:
        """alias 문자열만 (정렬됨). rate-limit 트리의 GLOBAL 루트 목록용.

        `list_all()` 은 행 전체(가격 관계 포함)를 읽는다 — 트리는 이름만 쓰므로 컬럼 하나만 읽는다.
        """
        stmt = select(ModelAlias.alias).order_by(ModelAlias.alias)
        if status is not None:
            stmt = stmt.where(ModelAlias.status == status)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def update_model(self, alias: str, **kwargs) -> ModelAlias | None:
        model = await self.get_by_alias(alias)
        if model is None:
            return None
        for key, value in kwargs.items():
            if value is not None:
                setattr(model, key, value)
        model.updated_at = datetime.now(timezone.utc)
        return model

    async def patch_status(self, alias: str, status: ModelStatus) -> ModelAlias | None:
        model = await self.get_by_alias(alias)
        if model is None:
            return None
        model.status = status
        model.updated_at = datetime.now(timezone.utc)
        return model

    # ── ModelPricing ──

    async def create_pricing(self, pricing: ModelPricing) -> ModelPricing:
        self._session.add(pricing)
        await self._session.flush()
        return pricing

    async def close_current_pricing(self, model_alias: str, effective_until: datetime) -> int:
        stmt = (
            update(ModelPricing)
            .where(
                ModelPricing.model_alias == model_alias,
                ModelPricing.effective_until.is_(None),
            )
            .values(effective_until=effective_until)
        )
        result = await self._session.execute(stmt)
        return result.rowcount  # type: ignore[return-value]

    async def get_current_pricing(self, model_alias: str) -> ModelPricing | None:
        stmt = select(ModelPricing).where(
            ModelPricing.model_alias == model_alias,
            ModelPricing.effective_until.is_(None),
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()


class TeamAllowedModelRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_by_team(self, team_id: uuid.UUID) -> list[str]:
        stmt = (
            select(TeamAllowedModel.model_alias)
            .where(TeamAllowedModel.team_id == team_id)
            .order_by(TeamAllowedModel.model_alias)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_by_user(self, user_id: uuid.UUID) -> list[str]:
        """주어진 user가 속한 team의 허용 모델 목록. team 없거나 엔트리 0개면 []."""
        from app.models.auth import User

        stmt = (
            select(TeamAllowedModel.model_alias)
            .join(User, User.team_id == TeamAllowedModel.team_id)
            .where(User.id == user_id)
            .order_by(TeamAllowedModel.model_alias)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def clear_for_team(self, team_id: uuid.UUID) -> int:
        from sqlalchemy import delete

        stmt = delete(TeamAllowedModel).where(TeamAllowedModel.team_id == team_id)
        result = await self._session.execute(stmt)
        return result.rowcount  # type: ignore[return-value]

    async def set_for_team(
        self,
        team_id: uuid.UUID,
        aliases: list[str],
        created_by: uuid.UUID,
    ) -> list[str]:
        """Replace-all: 기존 엔트리 삭제 후 주어진 aliases 전체 삽입.

        aliases가 빈 리스트면 엔트리만 모두 삭제 (전체 허용으로 복귀).
        """
        await self.clear_for_team(team_id)
        unique_aliases = sorted(set(aliases))
        for alias in unique_aliases:
            self._session.add(
                TeamAllowedModel(
                    team_id=team_id,
                    model_alias=alias,
                    created_by=created_by,
                )
            )
        await self._session.flush()
        return unique_aliases


class RateLimitConfigRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, config: RateLimitConfig) -> RateLimitConfig:
        # Serialize concurrent upserts of the same logical key, or two overlapping writes each
        # deactivate-then-insert and leave two is_active=true rows behind — after which
        # get_active() raises MultipleResultsFound forever (regression #53).
        #
        # USER/TEAM are alias-blind: the UPDATE below deactivates every active row of the
        # subject, so at most one active row per (scope, scope_id) exists. GLOBAL keeps one
        # active row per model alias (migration 0046), so its UPDATE is scoped to the alias and
        # saving one model never releases another.
        #
        # The lock key stays (scope, scope_id) WITHOUT model_alias for GLOBAL too. A coarser key
        # than the predicate is still correct; it only serializes GLOBAL saves of different
        # models against each other, which are rare admin writes. In exchange, during a rolling
        # deploy, the previous release's pods (whose GLOBAL upsert deactivates every GLOBAL row
        # under this same key) and this release's pods never interleave, so neither side can
        # trip the partial unique index of the other's write.
        await advisory_xact_lock(
            self._session, "rate_limit_configs", config.scope.value, config.scope_id
        )
        stmt = (
            update(RateLimitConfig)
            .where(
                RateLimitConfig.scope == config.scope,
                RateLimitConfig.scope_id == config.scope_id,
                RateLimitConfig.is_active.is_(True),
            )
            .values(is_active=False)
        )
        if config.scope == RateLimitScope.GLOBAL:
            if config.model_alias is not None:
                stmt = stmt.where(RateLimitConfig.model_alias == config.model_alias)
            else:
                stmt = stmt.where(RateLimitConfig.model_alias.is_(None))
        await self._session.execute(stmt)
        self._session.add(config)
        await self._session.flush()
        return config

    async def get_active(self, scope: RateLimitScope, scope_id: uuid.UUID | None) -> RateLimitConfig | None:
        """The one active row of a USER/TEAM subject. Not for GLOBAL: since 0046 every model alias
        has its own active GLOBAL row, so (GLOBAL, None) can match several and this raises."""
        stmt = select(RateLimitConfig).where(
            RateLimitConfig.scope == scope,
            RateLimitConfig.scope_id == scope_id,
            RateLimitConfig.is_active.is_(True),
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_active_by_scope(self, scope: RateLimitScope) -> list[RateLimitConfig]:
        stmt = select(RateLimitConfig).where(
            RateLimitConfig.scope == scope,
            RateLimitConfig.is_active.is_(True),
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def deactivate_configs(
        self, scope: RateLimitScope, scope_id: uuid.UUID
    ) -> int:
        stmt = (
            update(RateLimitConfig)
            .where(
                RateLimitConfig.scope == scope,
                RateLimitConfig.scope_id == scope_id,
                RateLimitConfig.is_active.is_(True),
            )
            .values(is_active=False)
        )
        result = await self._session.execute(stmt)
        return result.rowcount  # type: ignore[return-value]
