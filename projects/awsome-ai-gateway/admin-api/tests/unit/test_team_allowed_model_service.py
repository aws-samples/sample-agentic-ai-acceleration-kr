# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""FR-2.6 Team allowed models service unit tests."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.auth import CurrentUser
from app.core.cache_invalidation import CacheInvalidationManager
from app.core.exceptions import NotFoundError, ValidationError
from app.models.auth import Team
from app.models.model import ModelAlias
from app.services.team_allowed_model_service import TeamAllowedModelService


@pytest.fixture
def svc(cache_mgr: CacheInvalidationManager) -> TeamAllowedModelService:
    return TeamAllowedModelService(cache_mgr=cache_mgr)


def _team(team_id: uuid.UUID) -> MagicMock:
    t = MagicMock(spec=Team)
    t.id = team_id
    t.name = "T"
    t.dept_id = uuid.uuid4()
    return t


class TestListForTeam:
    async def test_empty_returns_empty_list(
        self, svc: TeamAllowedModelService, mock_session: AsyncMock
    ):
        team_id = uuid.uuid4()
        with patch("app.services.team_allowed_model_service.UserRepository") as MockUser, \
             patch("app.services.team_allowed_model_service.TeamAllowedModelRepository") as MockTam:
            MockUser.return_value.get_team = AsyncMock(return_value=_team(team_id))
            MockTam.return_value.list_by_team = AsyncMock(return_value=[])

            resp = await svc.list_for_team(mock_session, team_id=team_id)

        assert resp.model_aliases == []
        assert resp.team_id == str(team_id)

    async def test_team_not_found(
        self, svc: TeamAllowedModelService, mock_session: AsyncMock
    ):
        with patch("app.services.team_allowed_model_service.UserRepository") as MockUser:
            MockUser.return_value.get_team = AsyncMock(return_value=None)

            with pytest.raises(NotFoundError):
                await svc.list_for_team(mock_session, team_id=uuid.uuid4())


class TestSetForTeam:
    async def test_whitelist_persisted_and_invalidates_cache(
        self,
        svc: TeamAllowedModelService,
        mock_session: AsyncMock,
        admin_user: CurrentUser,
        mock_redis: AsyncMock,
    ):
        team_id = uuid.uuid4()
        aliases = ["claude-sonnet", "claude-haiku"]

        active_model = MagicMock(spec=ModelAlias)
        active_model.alias = "claude-sonnet"
        active_model.status = "ACTIVE"

        mock_redis.smembers = AsyncMock(return_value=set())
        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = []
        mock_session.execute.return_value = result_mock

        with patch("app.services.team_allowed_model_service.UserRepository") as MockUser, \
             patch("app.services.team_allowed_model_service.ModelRepository") as MockModel, \
             patch("app.services.team_allowed_model_service.TeamAllowedModelRepository") as MockTam, \
             patch("app.services.team_allowed_model_service.audit_logger") as mock_audit:
            MockUser.return_value.get_team = AsyncMock(return_value=_team(team_id))
            MockModel.return_value.get_by_alias = AsyncMock(return_value=active_model)
            MockTam.return_value.list_by_team = AsyncMock(return_value=[])
            MockTam.return_value.set_for_team = AsyncMock(return_value=sorted(aliases))
            mock_audit.log = AsyncMock()

            resp = await svc.set_for_team(
                mock_session,
                team_id=team_id,
                model_aliases=aliases,
                actor=admin_user,
            )

        assert resp.model_aliases == sorted(aliases)
        mock_audit.log.assert_called_once()
        assert mock_audit.log.call_args.kwargs["action"] == "SET_TEAM_ALLOWED_MODELS"

    async def test_empty_list_means_allow_all(
        self,
        svc: TeamAllowedModelService,
        mock_session: AsyncMock,
        admin_user: CurrentUser,
        mock_redis: AsyncMock,
    ):
        team_id = uuid.uuid4()
        mock_redis.smembers = AsyncMock(return_value=set())
        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = []
        mock_session.execute.return_value = result_mock

        with patch("app.services.team_allowed_model_service.UserRepository") as MockUser, \
             patch("app.services.team_allowed_model_service.TeamAllowedModelRepository") as MockTam, \
             patch("app.services.team_allowed_model_service.audit_logger") as mock_audit:
            MockUser.return_value.get_team = AsyncMock(return_value=_team(team_id))
            MockTam.return_value.list_by_team = AsyncMock(return_value=["old-alias"])
            MockTam.return_value.set_for_team = AsyncMock(return_value=[])
            mock_audit.log = AsyncMock()

            resp = await svc.set_for_team(
                mock_session, team_id=team_id, model_aliases=[], actor=admin_user
            )

        assert resp.model_aliases == []

    async def test_invalid_alias_rejected(
        self,
        svc: TeamAllowedModelService,
        mock_session: AsyncMock,
        admin_user: CurrentUser,
    ):
        team_id = uuid.uuid4()
        with patch("app.services.team_allowed_model_service.UserRepository") as MockUser, \
             patch("app.services.team_allowed_model_service.ModelRepository") as MockModel:
            MockUser.return_value.get_team = AsyncMock(return_value=_team(team_id))
            MockModel.return_value.get_by_alias = AsyncMock(return_value=None)

            with pytest.raises(ValidationError):
                await svc.set_for_team(
                    mock_session,
                    team_id=team_id,
                    model_aliases=["nonexistent"],
                    actor=admin_user,
                )


class TestClearForTeam:
    async def test_clears_entries(
        self,
        svc: TeamAllowedModelService,
        mock_session: AsyncMock,
        admin_user: CurrentUser,
        mock_redis: AsyncMock,
    ):
        team_id = uuid.uuid4()
        mock_redis.smembers = AsyncMock(return_value=set())
        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = []
        mock_session.execute.return_value = result_mock

        with patch("app.services.team_allowed_model_service.UserRepository") as MockUser, \
             patch("app.services.team_allowed_model_service.TeamAllowedModelRepository") as MockTam, \
             patch("app.services.team_allowed_model_service.audit_logger") as mock_audit:
            MockUser.return_value.get_team = AsyncMock(return_value=_team(team_id))
            MockTam.return_value.list_by_team = AsyncMock(return_value=["a", "b"])
            MockTam.return_value.clear_for_team = AsyncMock(return_value=2)
            mock_audit.log = AsyncMock()

            resp = await svc.clear_for_team(
                mock_session, team_id=team_id, actor=admin_user
            )

        assert resp.model_aliases == []
        MockTam.return_value.clear_for_team.assert_called_once_with(team_id)
