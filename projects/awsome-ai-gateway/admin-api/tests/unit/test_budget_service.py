# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.auth import CurrentUser
from app.core.cache_invalidation import CacheInvalidationManager
from app.core.exceptions import ForbiddenError, NotFoundError, ValidationError
from app.models.auth import Team, User, UserRole
from app.models.budget import BudgetConfig, BudgetPolicy, BudgetScope
from app.schemas.budgets import AllocateBudgetItem, AllocateBudgetRequest, SetBudgetRequest
from app.services.budget_service import BUDGET_CONFIG_CACHE_TTL, BudgetService

# ── BUDGET_CONFIG_CACHE_TTL 값 확인 ──
assert BUDGET_CONFIG_CACHE_TTL == 300, "BUDGET_CONFIG_CACHE_TTL 은 300초 (5분) 여야 함"


@pytest.fixture
def budget_service(cache_mgr: CacheInvalidationManager) -> BudgetService:
    return BudgetService(cache_mgr=cache_mgr)


class TestSetTeamBudget:
    async def test_set_team_budget_success(
        self, budget_service: BudgetService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        team_id = uuid.uuid4()
        data = SetBudgetRequest(max_budget_usd=Decimal("1000.00"))

        with patch("app.services.budget_service.UserRepository") as MockUserRepo, \
             patch("app.services.budget_service.BudgetRepository") as MockBudgetRepo, \
             patch("app.services.budget_service.audit_logger") as mock_audit:
            MockUserRepo.return_value.get_team = AsyncMock(return_value=MagicMock(spec=Team))
            MockBudgetRepo.return_value.upsert_config = AsyncMock()
            mock_audit.log = AsyncMock()

            await budget_service.set_team_budget(mock_session, team_id=team_id, data=data, actor=admin_user)

        MockBudgetRepo.return_value.upsert_config.assert_called_once()

    async def test_set_team_budget_not_found(
        self, budget_service: BudgetService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        team_id = uuid.uuid4()
        data = SetBudgetRequest(max_budget_usd=Decimal("1000.00"))

        with patch("app.services.budget_service.UserRepository") as MockUserRepo:
            MockUserRepo.return_value.get_team = AsyncMock(return_value=None)

            with pytest.raises(NotFoundError):
                await budget_service.set_team_budget(mock_session, team_id=team_id, data=data, actor=admin_user)


class TestSetUserBudget:
    async def test_team_leader_cannot_set_other_team_budget(
        self, budget_service: BudgetService, mock_session: AsyncMock, team_leader_user: CurrentUser
    ):
        user_id = uuid.uuid4()
        data = SetBudgetRequest(max_budget_usd=Decimal("100.00"))

        other_team_id = uuid.uuid4()
        user = MagicMock(spec=User)
        user.team_id = other_team_id  # Different team

        with patch("app.services.budget_service.UserRepository") as MockUserRepo:
            MockUserRepo.return_value.get_user = AsyncMock(return_value=user)

            with pytest.raises(ForbiddenError):
                await budget_service.set_user_budget(mock_session, user_id=user_id, data=data, actor=team_leader_user)

    async def test_user_budget_sum_exceeds_team_budget(
        self, budget_service: BudgetService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        user_id = uuid.uuid4()
        team_id = uuid.uuid4()
        data = SetBudgetRequest(max_budget_usd=Decimal("600.00"))

        user = MagicMock(spec=User)
        user.team_id = team_id

        team_config = MagicMock(spec=BudgetConfig)
        team_config.max_budget_usd = Decimal("1000.00")

        with patch("app.services.budget_service.UserRepository") as MockUserRepo, \
             patch("app.services.budget_service.BudgetRepository") as MockBudgetRepo:
            MockUserRepo.return_value.get_user = AsyncMock(return_value=user)
            repo = MockBudgetRepo.return_value
            repo.get_active_config = AsyncMock(side_effect=[team_config, None])
            repo.sum_member_budgets = AsyncMock(return_value=Decimal("500.00"))

            with pytest.raises(ValidationError, match="exceeds team budget"):
                await budget_service.set_user_budget(mock_session, user_id=user_id, data=data, actor=admin_user)

    async def test_user_budget_replaces_existing(
        self, budget_service: BudgetService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        user_id = uuid.uuid4()
        team_id = uuid.uuid4()
        data = SetBudgetRequest(max_budget_usd=Decimal("200.00"))

        user = MagicMock(spec=User)
        user.team_id = team_id

        team_config = MagicMock(spec=BudgetConfig)
        team_config.max_budget_usd = Decimal("1000.00")

        existing_config = MagicMock(spec=BudgetConfig)
        existing_config.max_budget_usd = Decimal("150.00")

        with patch("app.services.budget_service.UserRepository") as MockUserRepo, \
             patch("app.services.budget_service.BudgetRepository") as MockBudgetRepo, \
             patch("app.services.budget_service.audit_logger") as mock_audit:
            MockUserRepo.return_value.get_user = AsyncMock(return_value=user)
            repo = MockBudgetRepo.return_value
            repo.get_active_config = AsyncMock(side_effect=[team_config, existing_config])
            repo.sum_member_budgets = AsyncMock(return_value=Decimal("300.00"))
            repo.upsert_config = AsyncMock()
            mock_audit.log = AsyncMock()

            # current_sum(300) - existing(150) + new(200) = 350 <= 1000 → OK
            await budget_service.set_user_budget(mock_session, user_id=user_id, data=data, actor=admin_user)

        repo.upsert_config.assert_called_once()


class TestAllocateTeamBudget:
    async def test_team_leader_cannot_allocate_other_team(
        self, budget_service: BudgetService, mock_session: AsyncMock, team_leader_user: CurrentUser
    ):
        other_team_id = uuid.uuid4()
        data = AllocateBudgetRequest(allocations=[
            AllocateBudgetItem(user_id=str(uuid.uuid4()), allocated_usd=Decimal("100.00")),
        ])

        with pytest.raises(ForbiddenError):
            await budget_service.allocate_team_budget(
                mock_session, team_id=other_team_id, data=data, actor=team_leader_user
            )

    async def test_allocation_exceeds_team_budget(
        self, budget_service: BudgetService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        team_id = uuid.uuid4()
        data = AllocateBudgetRequest(allocations=[
            AllocateBudgetItem(user_id=str(uuid.uuid4()), allocated_usd=Decimal("600.00")),
            AllocateBudgetItem(user_id=str(uuid.uuid4()), allocated_usd=Decimal("600.00")),
        ])

        team_config = MagicMock(spec=BudgetConfig)
        team_config.max_budget_usd = Decimal("1000.00")

        with patch("app.services.budget_service.BudgetRepository") as MockBudgetRepo:
            MockBudgetRepo.return_value.get_active_config = AsyncMock(return_value=team_config)

            with pytest.raises(ValidationError, match="exceeds team budget"):
                await budget_service.allocate_team_budget(
                    mock_session, team_id=team_id, data=data, actor=admin_user
                )

    async def test_allocation_requires_team_budget_first(
        self, budget_service: BudgetService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        team_id = uuid.uuid4()
        data = AllocateBudgetRequest(allocations=[
            AllocateBudgetItem(user_id=str(uuid.uuid4()), allocated_usd=Decimal("100.00")),
        ])

        with patch("app.services.budget_service.BudgetRepository") as MockBudgetRepo:
            MockBudgetRepo.return_value.get_active_config = AsyncMock(return_value=None)

            with pytest.raises(ValidationError, match="Team budget must be set"):
                await budget_service.allocate_team_budget(
                    mock_session, team_id=team_id, data=data, actor=admin_user
                )


class TestGetBudgetSummary:
    @pytest.mark.asyncio
    async def test_budget_summary_returns_user_and_team_rows(
        self, budget_service: BudgetService, mock_session: AsyncMock
    ):
        team_id = uuid.uuid4()
        user_id = uuid.uuid4()

        team_cfg = MagicMock(spec=BudgetConfig)
        team_cfg.scope = BudgetScope.TEAM
        team_cfg.scope_id = team_id
        team_cfg.max_budget_usd = Decimal("1000")

        user_obj = MagicMock()
        user_obj.id = user_id
        user_obj.display_name = "Alice"
        user_obj.email = "a@b"
        team_obj = MagicMock()
        team_obj.id = team_id
        team_obj.name = "Eng"
        team_obj.department = None

        # _resolve_used falls through to session.execute when redis=None;
        # configure the awaited result so scalar_one() returns a Decimal-safe string
        execute_result = MagicMock()
        execute_result.scalar_one = MagicMock(return_value="0")
        mock_session.execute = AsyncMock(return_value=execute_result)

        with patch("app.services.budget_service.BudgetRepository") as BRepo, \
             patch("app.repositories.user_repository.UserRepository") as URepo:
            BRepo.return_value.list_configs = AsyncMock(return_value=[team_cfg])
            URepo.return_value.list_users = AsyncMock(return_value=[user_obj])
            URepo.return_value.list_all_teams = AsyncMock(return_value=[team_obj])

            result = await budget_service.get_budget_summary(
                mock_session, scope=None, target_id=None, period="2026-04"
            )

        target_types = sorted({i.target_type for i in result.summary})
        assert target_types == ["team", "user"]
        team_row = next(i for i in result.summary if i.target_type == "team")
        user_row = next(i for i in result.summary if i.target_type == "user")
        assert team_row.limit_usd == Decimal("1000")
        assert user_row.limit_usd is None  # 미설정 user → limit 없음


@pytest.mark.asyncio
async def test_sync_redis_thresholds_sets_5min_ttl(budget_service):
    fake_redis = MagicMock()
    fake_redis.set = AsyncMock()
    budget_service._cache_mgr._redis = fake_redis

    data = SetBudgetRequest(
        max_budget_usd=Decimal("100"),
        policy=BudgetPolicy.HARD_BLOCK,
    )
    await budget_service._sync_redis_thresholds(
        "user", uuid.UUID("00000000-0000-4000-a000-000000000001"), data
    )

    fake_redis.set.assert_called_once()
    call = fake_redis.set.call_args
    assert call.kwargs.get("ex") == BUDGET_CONFIG_CACHE_TTL, (
        "USER budget config cache 는 5분 TTL 이어야 함 (Z 정책)"
    )


@pytest.mark.asyncio
async def test_warm_team_budget_cache_writes_all_active_team_configs(
    budget_service: BudgetService, mock_session: AsyncMock
):
    """startup warmup: 활성 TEAM BudgetConfig 전체를 Redis에 캐싱."""
    team_id_1 = uuid.uuid4()
    team_id_2 = uuid.uuid4()

    cfg1 = MagicMock(spec=BudgetConfig)
    cfg1.scope = BudgetScope.TEAM
    cfg1.scope_id = team_id_1
    cfg1.max_budget_usd = Decimal("5000")
    cfg1.policy = BudgetPolicy.HARD_BLOCK

    cfg2 = MagicMock(spec=BudgetConfig)
    cfg2.scope = BudgetScope.TEAM
    cfg2.scope_id = team_id_2
    cfg2.max_budget_usd = Decimal("1000")
    cfg2.policy = BudgetPolicy.HARD_BLOCK

    fake_redis = MagicMock()
    fake_redis.set = AsyncMock()
    budget_service._cache_mgr._redis = fake_redis

    with patch("app.services.budget_service.BudgetRepository") as BRepo:
        BRepo.return_value.list_configs = AsyncMock(return_value=[cfg1, cfg2])
        count = await budget_service.warm_team_budget_cache(mock_session)

    assert count == 2
    assert fake_redis.set.call_count == 2

    # Redis Cluster hash-tag braces 포함 여부 확인
    keys_called = [c.args[0] for c in fake_redis.set.call_args_list]
    assert f"budget:config:team:{{{team_id_1}}}" in keys_called
    assert f"budget:config:team:{{{team_id_2}}}" in keys_called

    # EX TTL = BUDGET_CONFIG_CACHE_TTL (300s) 확인
    for c in fake_redis.set.call_args_list:
        assert c.kwargs.get("ex") == BUDGET_CONFIG_CACHE_TTL, (
            f"warm_team_budget_cache 는 {BUDGET_CONFIG_CACHE_TTL}초 TTL 이어야 함"
        )


@pytest.mark.asyncio
async def test_warm_team_budget_cache_empty_returns_zero(
    budget_service: BudgetService, mock_session: AsyncMock
):
    """활성 TEAM BudgetConfig 없으면 0 반환, Redis SET 없음."""
    fake_redis = MagicMock()
    fake_redis.set = AsyncMock()
    budget_service._cache_mgr._redis = fake_redis

    with patch("app.services.budget_service.BudgetRepository") as BRepo:
        BRepo.return_value.list_configs = AsyncMock(return_value=[])
        count = await budget_service.warm_team_budget_cache(mock_session)

    assert count == 0
    fake_redis.set.assert_not_called()
