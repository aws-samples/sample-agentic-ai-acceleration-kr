# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.auth import CurrentUser
from app.core.cache_invalidation import CacheInvalidationManager
from app.core.exceptions import NotFoundError
from app.models.auth import Department, Team, User, UserRole
from app.services.key_service import KeyService
from app.services.user_team_service import UserTeamService


@pytest.fixture
def user_team_service() -> UserTeamService:
    cache_mgr = MagicMock(spec=CacheInvalidationManager)
    cache_mgr._redis = MagicMock()
    key_service = MagicMock(spec=KeyService)
    return UserTeamService(cache_mgr=cache_mgr, key_service=key_service)


class TestSetTeamLeader:
    async def test_set_team_leader_updates_role(
        self, user_team_service: UserTeamService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        team_id = uuid.uuid4()
        user_id = uuid.uuid4()

        team = MagicMock(spec=Team)
        team.id = team_id
        team.name = "Test Team"
        team.dept_id = uuid.uuid4()
        team.leader_user_id = user_id
        team.created_at = MagicMock()

        with patch("app.services.user_team_service.UserRepository") as MockRepo, \
             patch("app.services.user_team_service.audit_logger") as mock_audit:
            repo = MockRepo.return_value
            repo.set_leader = AsyncMock(return_value=team)
            repo.update_user_role = AsyncMock()
            mock_audit.log = AsyncMock()

            result = await user_team_service.set_team_leader(
                mock_session, team_id=team_id, user_id=user_id, actor=admin_user
            )

        repo.update_user_role.assert_called_once_with(user_id, UserRole.TEAM_LEADER)
        assert result.leader_user_id == str(user_id)

    async def test_set_team_leader_not_found(
        self, user_team_service: UserTeamService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        with patch("app.services.user_team_service.UserRepository") as MockRepo:
            MockRepo.return_value.set_leader = AsyncMock(return_value=None)

            with pytest.raises(NotFoundError):
                await user_team_service.set_team_leader(
                    mock_session, team_id=uuid.uuid4(), user_id=uuid.uuid4(), actor=admin_user
                )


class TestTransferUser:
    async def test_transfer_deactivates_budget_and_invalidates_cache(
        self, user_team_service: UserTeamService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        user_id = uuid.uuid4()
        old_team_id = uuid.uuid4()
        new_team_id = uuid.uuid4()

        user = MagicMock(spec=User)
        user.id = user_id
        user.team_id = old_team_id
        user.email = "dev@test.com"
        user.display_name = "Dev"
        user.role = UserRole.DEVELOPER
        user.is_active = True
        user.created_at = MagicMock()
        user.team = None

        transferred_user = MagicMock(spec=User)
        transferred_user.id = user_id
        transferred_user.team_id = new_team_id
        transferred_user.email = "dev@test.com"
        transferred_user.display_name = "Dev"
        transferred_user.role = UserRole.DEVELOPER
        transferred_user.is_active = True
        transferred_user.created_at = MagicMock()
        transferred_user.team = None

        with patch("app.services.user_team_service.UserRepository") as MockUserRepo, \
             patch("app.services.user_team_service.BudgetRepository") as MockBudgetRepo, \
             patch("app.services.user_team_service.RateLimitConfigRepository") as MockRLRepo, \
             patch("app.services.user_team_service.audit_logger") as mock_audit:
            user_repo = MockUserRepo.return_value
            user_repo.get_user = AsyncMock(return_value=user)
            user_repo.update_user_team = AsyncMock(return_value=transferred_user)
            MockBudgetRepo.return_value.deactivate_configs = AsyncMock()
            MockRLRepo.return_value.deactivate_configs = AsyncMock()
            mock_audit.log = AsyncMock()

            # Stub KeyService: no VK hashes (minimal case)
            user_team_service._key_service.list_active_vk_hashes_for_user = AsyncMock(
                return_value=[]
            )
            cache_mgr = user_team_service._cache_mgr
            cache_mgr.invalidate = AsyncMock()
            cache_mgr.swap_reverse_index_membership = AsyncMock()

            result = await user_team_service.transfer_user(
                mock_session, user_id=user_id, new_team_id=new_team_id, actor=admin_user
            )

        # Budget deactivated
        MockBudgetRepo.return_value.deactivate_configs.assert_awaited_once()
        # Cache invalidation called
        cache_mgr.invalidate.assert_awaited_once()
        assert result.team_id == str(new_team_id)

    async def test_transfer_user_not_found(
        self, user_team_service: UserTeamService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        with patch("app.services.user_team_service.UserRepository") as MockRepo:
            MockRepo.return_value.get_user = AsyncMock(return_value=None)

            with pytest.raises(NotFoundError):
                await user_team_service.transfer_user(
                    mock_session, user_id=uuid.uuid4(), new_team_id=uuid.uuid4(), actor=admin_user
                )

    @pytest.mark.asyncio
    async def test_transfer_user_invalidates_caches_and_swaps_reverse_index(
        self, user_team_service: UserTeamService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        user_id = uuid.uuid4()
        old_team_id = uuid.uuid4()
        new_team_id = uuid.uuid4()

        user_mock = MagicMock(team_id=old_team_id, id=user_id)
        user_mock_after = MagicMock(
            team_id=new_team_id, id=user_id,
            email="x@y", display_name="X",
            role=UserRole.DEVELOPER,
            is_active=True,
            created_at=MagicMock(),
            team=None,
        )

        with patch("app.services.user_team_service.UserRepository") as URepo, \
             patch("app.services.user_team_service.BudgetRepository") as BRepo, \
             patch("app.services.user_team_service.RateLimitConfigRepository") as RLRepo, \
             patch("app.services.user_team_service.audit_logger") as mock_audit:

            URepo.return_value.get_user = AsyncMock(return_value=user_mock)
            URepo.return_value.update_user_team = AsyncMock(return_value=user_mock_after)
            BRepo.return_value.deactivate_configs = AsyncMock(return_value=1)
            RLRepo.return_value.deactivate_configs = AsyncMock(return_value=1)
            mock_audit.log = AsyncMock()

            # Stub KeyService method on the injected instance
            user_team_service._key_service.list_active_vk_hashes_for_user = AsyncMock(
                return_value=["h1", "h2"]
            )

            cache_mgr = user_team_service._cache_mgr
            cache_mgr.invalidate = AsyncMock()
            cache_mgr.swap_reverse_index_membership = AsyncMock()

            await user_team_service.transfer_user(
                mock_session, user_id=user_id,
                new_team_id=new_team_id, actor=admin_user,
            )

            # Verify USER scope deactivations
            BRepo.return_value.deactivate_configs.assert_awaited_once()
            RLRepo.return_value.deactivate_configs.assert_awaited_once()

            # Verify cache invalidate keys
            invalidated = set(cache_mgr.invalidate.call_args.args[0])
            assert f"user_context:{user_id}" in invalidated
            assert f"budget:config:user:{{{user_id}}}" in invalidated
            assert "key:cache:vk:h1" in invalidated
            assert "key:cache:vk:h2" in invalidated

            # Verify reverse index swap
            swap_kwargs = cache_mgr.swap_reverse_index_membership.call_args.kwargs
            assert swap_kwargs["old_key"] == f"team:vk_hashes:{old_team_id}"
            assert swap_kwargs["new_key"] == f"team:vk_hashes:{new_team_id}"
            assert swap_kwargs["members"] == ["h1", "h2"]


class TestListUsers:
    async def test_list_users_pagination(
        self, user_team_service: UserTeamService, mock_session: AsyncMock
    ):
        users = [MagicMock(spec=User) for _ in range(3)]
        for i, u in enumerate(users):
            u.id = uuid.uuid4()
            u.email = f"user{i}@test.com"
            u.display_name = f"User {i}"
            u.role = UserRole.DEVELOPER
            u.team_id = None
            u.is_active = True
            u.created_at = MagicMock()
            u.team = None

        with patch("app.services.user_team_service.UserRepository") as MockRepo:
            MockRepo.return_value.list_users = AsyncMock(return_value=users)

            result, has_more = await user_team_service.list_users(mock_session, limit=2)

        assert len(result) == 2
        assert has_more is True
