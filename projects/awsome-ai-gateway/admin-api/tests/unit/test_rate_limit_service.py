# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.auth import CurrentUser
from app.core.cache_invalidation import CacheInvalidationManager
from app.core.exceptions import ValidationError
from app.models.model import RateLimitScope
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

    ⚠️ 무효화는 **커밋 뒤에** 일어나야 한다. 서비스는 지울 대상을 세션에 쌓기만 하고, 라우터가
       ``session.commit()`` 뒤 ``invalidate_after_commit`` 으로 비운다. 커밋 전에 지우면 그 창의
       게이트웨이 캐시 미스가 아직 커밋 안 된(=옛) 행을 300초 다시 캐시한다(실 PG+Redis 재현:
       test_high_rate_limit_invalidate_after_commit_real.py).
    """

    async def test_drain_runs_once_and_is_a_no_op_when_nothing_is_queued(
        self,
        rate_limit_service: RateLimitService,
        mock_session: AsyncMock,
        mock_redis: AsyncMock,
        admin_user: CurrentUser,
    ):
        await rate_limit_service.invalidate_after_commit(mock_session)  # 쌓인 것 없음
        mock_redis.scan_iter.assert_not_called()
        mock_redis.delete.assert_not_called()

        with patch("app.services.rate_limit_service.RateLimitConfigRepository") as MockRepo, \
             patch("app.services.rate_limit_service.audit_logger") as mock_audit:
            MockRepo.return_value.upsert = AsyncMock()
            mock_audit.log = AsyncMock()
            await rate_limit_service.set_team_rate_limit(
                mock_session, team_id=uuid.uuid4(), data=RateLimitSetRequest(rpm=1), actor=admin_user
            )
        await rate_limit_service.invalidate_after_commit(mock_session)
        await rate_limit_service.invalidate_after_commit(mock_session)  # 두 번째는 비어 있다
        assert mock_redis.scan_iter.call_count == 1

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

        # 커밋 전에는 아무것도 지우지 않는다 — 라우터가 커밋한 뒤 비운다(아래 클래스 docstring).
        mock_redis.scan_iter.assert_not_called()
        await rate_limit_service.invalidate_after_commit(mock_session)

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

        mock_redis.scan_iter.assert_not_called()
        await rate_limit_service.invalidate_after_commit(mock_session)

        match_arg = mock_redis.scan_iter.call_args.kwargs.get("match") \
            or mock_redis.scan_iter.call_args.args[0]
        assert match_arg == f"rl:config:TEAM:{team_id}:*"
        assert cached_keys[0] in [c.args[0] for c in mock_redis.delete.call_args_list]

    async def test_global_set_invalidates_only_that_models_key(
        self,
        rate_limit_service: RateLimitService,
        mock_session: AsyncMock,
        admin_user: CurrentUser,
        mock_redis: AsyncMock,
    ):
        """GLOBAL 저장은 **그 모델의** GLOBAL 캐시 키 하나만 지운다(0046).

        GLOBAL 은 모델별이라 upsert 가 이 alias 의 행만 바꾼다 — 다른 모델(claude-sonnet)의 캐시는
        여전히 DB 와 같다. 단일 슬롯이던 동안에는 저장이 다른 모델의 한도를 해제했으므로 패턴
        (``rl:config:GLOBAL:NULL:*``)으로 전부 지워야 했다. 지금 그렇게 지우면 모든 모델의 다음 요청이
        DB 를 다시 읽을 뿐 해는 없지만, 키 하나면 SCAN 이 필요 없다.
        """
        data = RateLimitSetRequest(rpm=1000, tpm=500000)

        with patch("app.services.rate_limit_service.RateLimitConfigRepository") as MockRepo, \
             patch("app.services.rate_limit_service.audit_logger") as mock_audit:
            MockRepo.return_value.upsert = AsyncMock()
            mock_audit.log = AsyncMock()
            mock_session.scalar = AsyncMock(return_value="claude-opus")  # alias 존재 확인

            await rate_limit_service.set_global_rate_limit(
                mock_session, model_alias="claude-opus", data=data, actor=admin_user
            )

        mock_redis.delete.assert_not_called()  # 커밋 전에는 지우지 않는다
        await rate_limit_service.invalidate_after_commit(mock_session)

        mock_redis.scan_iter.assert_not_called()
        assert [c.args[0] for c in mock_redis.delete.call_args_list] == [
            "rl:config:GLOBAL:NULL:claude-opus"
        ]


class TestGlobalIsPerModel:
    """GLOBAL 한도는 모델별이다(0046). 한 모델의 저장은 다른 모델의 GLOBAL 한도를 해제하지 않으므로
    확인할 것도 없다 — 예전의 슬롯 주인 조회·409·``released`` 감사 항목이 사라졌다.

    실제 행이 공존하는지(두 alias 동시 저장, 같은 alias 동시 저장, 한 모델 비우기)는 실 PG 로 본다:
    tests/regression/test_high_duplicate_active_config.py, test_high_console_contract_real_db.py.
    """

    async def test_saving_one_model_never_consults_or_releases_another(
        self, rate_limit_service, mock_session, admin_user
    ):
        """다른 모델(claude-sonnet)이 실제 한도를 갖고 있어도 동의 없이 저장된다.

        대조군: 단일 슬롯 코드는 ``get_active(GLOBAL, None)`` 으로 그 행을 읽고 409 를 냈다 —
        여기서 ``get_active`` 가 그 행을 돌려주게 해 두었으므로 그 코드는 이 테스트에서 실패한다.
        """
        holder = MagicMock(model_alias="claude-sonnet", rpm_limit=50, tpm_limit=200000)
        with patch("app.services.rate_limit_service.RateLimitConfigRepository") as MockRepo, \
             patch("app.services.rate_limit_service.audit_logger") as mock_audit:
            MockRepo.return_value.upsert = AsyncMock()
            MockRepo.return_value.get_active = AsyncMock(return_value=holder)
            mock_audit.log = AsyncMock()
            mock_session.scalar = AsyncMock(return_value="claude-opus")  # alias 존재 확인

            result = await rate_limit_service.set_global_rate_limit(
                mock_session, model_alias="claude-opus", data=RateLimitSetRequest(rpm=10),
                actor=admin_user,
            )

        MockRepo.return_value.get_active.assert_not_awaited()
        saved = MockRepo.return_value.upsert.await_args.args[0]
        assert (saved.scope, saved.scope_id, saved.model_alias, saved.rpm_limit) == (
            RateLimitScope.GLOBAL, None, "claude-opus", 10,
        )
        # 감사 기록에 모델이 있어야 한다 — resource_id(새 행 id)만으로는 어느 모델인지 모른다.
        assert mock_audit.log.await_args.kwargs["changes"] == {
            "after": {"scope": "GLOBAL", "rpm": 10, "tpm": None, "model_alias": "claude-opus"}
        }
        assert (result.model_alias, result.rpm_limit) == ("claude-opus", 10)
