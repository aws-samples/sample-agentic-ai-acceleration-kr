# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.auth import CurrentUser
from app.core.cache_invalidation import CacheInvalidationManager
from app.core.exceptions import ValidationError
from app.schemas.rate_limits import RateLimitSetRequest
from app.services.rate_limit_service import RateLimitService


@pytest.fixture
def rate_limit_service(cache_mgr: CacheInvalidationManager) -> RateLimitService:
    return RateLimitService(cache_mgr=cache_mgr)


class TestCPMCPHScope:
    async def test_team_scope_allows_cpm_cph(
        self,
        rate_limit_service: RateLimitService,
        mock_session: AsyncMock,
        admin_user: CurrentUser,
        mock_redis: AsyncMock,
    ):
        data = RateLimitSetRequest(rpm=100, tpm=50000, cpm=Decimal("10.00"), cph=Decimal("50.00"))

        with patch("app.services.rate_limit_service.RateLimitConfigRepository") as MockRepo, \
             patch("app.services.rate_limit_service.audit_logger") as mock_audit:
            MockRepo.return_value.upsert = AsyncMock()
            mock_audit.log = AsyncMock()

            result = await rate_limit_service.set_team_rate_limit(
                mock_session, team_id=uuid.uuid4(), data=data, actor=admin_user
            )

        assert result.cpm_limit_usd == Decimal("10.00")
        assert result.cph_limit_usd == Decimal("50.00")

    async def test_global_scope_rejects_cpm(
        self, rate_limit_service: RateLimitService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        data = RateLimitSetRequest(rpm=100, cpm=Decimal("10.00"))

        with pytest.raises(ValidationError, match="CPM/CPH"):
            await rate_limit_service.set_global_rate_limit(
                mock_session, model_alias="claude-sonnet", data=data, actor=admin_user
            )

    async def test_global_scope_rejects_cph(
        self, rate_limit_service: RateLimitService, mock_session: AsyncMock, admin_user: CurrentUser
    ):
        data = RateLimitSetRequest(rpm=100, cph=Decimal("50.00"))

        with pytest.raises(ValidationError, match="CPM/CPH"):
            await rate_limit_service.set_global_rate_limit(
                mock_session, model_alias="claude-sonnet", data=data, actor=admin_user
            )

    async def test_user_scope_allows_cpm_cph(
        self, rate_limit_service: RateLimitService, mock_session: AsyncMock, admin_user: CurrentUser, mock_redis: AsyncMock
    ):
        data = RateLimitSetRequest(rpm=100, tpm=50000, cpm=Decimal("10.00"), cph=Decimal("50.00"))

        with patch("app.services.rate_limit_service.RateLimitConfigRepository") as MockRepo, \
             patch("app.services.rate_limit_service.audit_logger") as mock_audit:
            MockRepo.return_value.upsert = AsyncMock()
            mock_audit.log = AsyncMock()

            result = await rate_limit_service.set_user_rate_limit(
                mock_session, user_id=uuid.uuid4(), data=data, actor=admin_user
            )

        assert result.rpm_limit == 100
        assert result.cpm_limit_usd == Decimal("10.00")
        assert result.cph_limit_usd == Decimal("50.00")
        assert result.is_active is True


class TestRateLimitCaching:
    async def test_set_rate_limit_writes_to_redis(
        self, rate_limit_service: RateLimitService, mock_session: AsyncMock, admin_user: CurrentUser, mock_redis: AsyncMock
    ):
        data = RateLimitSetRequest(rpm=60, tpm=10000)
        user_id = uuid.uuid4()

        with patch("app.services.rate_limit_service.RateLimitConfigRepository") as MockRepo, \
             patch("app.services.rate_limit_service.audit_logger") as mock_audit:
            MockRepo.return_value.upsert = AsyncMock()
            mock_audit.log = AsyncMock()

            await rate_limit_service.set_user_rate_limit(
                mock_session, user_id=user_id, data=data, actor=admin_user
            )

        mock_redis.set.assert_called_once()
        key = mock_redis.set.call_args[0][0]
        assert key == f"ratelimit:config:user:{user_id}"


class TestGatewayCacheInvalidation:
    """Verify admin-api invalidates gateway-proxy's rl:config:* cache on policy update.

    Without this, gateway can serve stale policies for up to the 5-min TTL
    (`_CONFIG_CACHE_TTL_SEC` in gateway-proxy/.../rate_limit_config_loader.py).
    """

    @staticmethod
    def _scan_iter_returning(keys: list[str]):
        async def _gen(*_args, **_kwargs):
            for k in keys:
                yield k
        return _gen

    async def test_user_set_invalidates_per_model_keys(
        self,
        rate_limit_service: RateLimitService,
        mock_session: AsyncMock,
        admin_user: CurrentUser,
        mock_redis: AsyncMock,
    ):
        data = RateLimitSetRequest(rpm=60, tpm=10000)
        user_id = uuid.uuid4()

        # Simulate gateway-proxy having cached policies for two models.
        cached_keys = [
            f"rl:config:USER:{user_id}:claude-opus",
            f"rl:config:USER:{user_id}:claude-sonnet",
        ]
        mock_redis.scan_iter = MagicMock(
            side_effect=lambda *a, **kw: self._scan_iter_returning(cached_keys)()
        )

        with patch("app.services.rate_limit_service.RateLimitConfigRepository") as MockRepo, \
             patch("app.services.rate_limit_service.audit_logger") as mock_audit:
            MockRepo.return_value.upsert = AsyncMock()
            mock_audit.log = AsyncMock()

            await rate_limit_service.set_user_rate_limit(
                mock_session, user_id=user_id, data=data, actor=admin_user
            )

        mock_redis.scan_iter.assert_called_once()
        match_arg = mock_redis.scan_iter.call_args.kwargs.get("match") \
            or mock_redis.scan_iter.call_args.args[0]
        assert match_arg == f"rl:config:USER:{user_id}:*"

        deleted_keys = [c.args[0] for c in mock_redis.delete.call_args_list]
        for k in cached_keys:
            assert k in deleted_keys

    async def test_team_set_invalidates_per_model_keys(
        self,
        rate_limit_service: RateLimitService,
        mock_session: AsyncMock,
        admin_user: CurrentUser,
        mock_redis: AsyncMock,
    ):
        data = RateLimitSetRequest(rpm=60, tpm=10000)
        team_id = uuid.uuid4()
        cached_keys = [f"rl:config:TEAM:{team_id}:claude-opus"]
        mock_redis.scan_iter = MagicMock(
            side_effect=lambda *a, **kw: self._scan_iter_returning(cached_keys)()
        )

        with patch("app.services.rate_limit_service.RateLimitConfigRepository") as MockRepo, \
             patch("app.services.rate_limit_service.audit_logger") as mock_audit:
            MockRepo.return_value.upsert = AsyncMock()
            mock_audit.log = AsyncMock()

            await rate_limit_service.set_team_rate_limit(
                mock_session, team_id=team_id, data=data, actor=admin_user
            )

        match_arg = mock_redis.scan_iter.call_args.kwargs.get("match") \
            or mock_redis.scan_iter.call_args.args[0]
        assert match_arg == f"rl:config:TEAM:{team_id}:*"
        assert cached_keys[0] in [c.args[0] for c in mock_redis.delete.call_args_list]

    async def test_global_set_invalidates_specific_model_key(
        self,
        rate_limit_service: RateLimitService,
        mock_session: AsyncMock,
        admin_user: CurrentUser,
        mock_redis: AsyncMock,
    ):
        # GLOBAL scope always carries a model_alias → exact-key delete, no SCAN.
        data = RateLimitSetRequest(rpm=1000, tpm=500000)

        with patch("app.services.rate_limit_service.RateLimitConfigRepository") as MockRepo, \
             patch("app.services.rate_limit_service.audit_logger") as mock_audit:
            MockRepo.return_value.upsert = AsyncMock()
            mock_audit.log = AsyncMock()

            await rate_limit_service.set_global_rate_limit(
                mock_session, model_alias="claude-opus", data=data, actor=admin_user
            )

        deleted_keys = [c.args[0] for c in mock_redis.delete.call_args_list]
        assert "rl:config:GLOBAL:NULL:claude-opus" in deleted_keys
        mock_redis.scan_iter.assert_not_called()
