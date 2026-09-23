# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""AWS 자동연동 + long-context 티어 승계 — 실 PostgreSQL 왕복.

Mock 으로는 증명 불가능한 것들만 여기서 본다: set_pricing 이 실제로 model_pricings 에 0038 의
여섯 long 컬럼을 쓰고 다시 읽히는지, 그리고 **short 단가만 고치는 PUT 이 272K 티어를 지우지
않는지**(옛 sync 의 NULL-wipe 회귀 — 실 INSERT/SELECT 로만 증명된다).

⚠️ 이 테스트는 PROOF_DSN 이 **alembic head(0038/0039 포함)** 를 마친 DB 를 가리킬 것을 요구한다.
   자기 소유 스크래치 DB(<name>_awsproof)를 TEMPLATE 로 복제해 그 안에서만 쓴다.
"""
from __future__ import annotations

import os
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PROOF_DSN = os.environ.get("PROOF_DSN")
_OWNED = "_awsproof"
_SYS_USER = uuid.UUID("00000000-0000-4000-a000-000000000010")  # 0032 seed system user

_pg = pytest.mark.skipif(
    not PROOF_DSN, reason="PROOF_DSN 미설정 — 실 PG 필요(0038/0039 적용 head)."
)


def _split(dsn):
    pre, _, name = dsn.rpartition("/")
    return pre, name


async def _make_db():
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    pre, name = _split(PROOF_DSN)
    owned = name + _OWNED
    assert any(t in owned for t in ("proof", "test", "scratch", "tmp", "lct")), owned
    admin = create_async_engine(f"{pre}/postgres", isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as c:
            await c.execute(text(f'DROP DATABASE IF EXISTS "{owned}" WITH (FORCE)'))
            await c.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                                 "WHERE datname=:d AND pid<>pg_backend_pid()"), {"d": name})
            await c.execute(text(f'CREATE DATABASE "{owned}" TEMPLATE "{name}"'))
    finally:
        await admin.dispose()
    return f"{pre}/{owned}"


@pytest.fixture(scope="module")
async def dsn():
    pytest.importorskip("asyncpg")
    if not PROOF_DSN:
        pytest.skip("PROOF_DSN 미설정")
    url = await _make_db()
    yield url
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    pre, name = _split(PROOF_DSN)
    admin = create_async_engine(f"{pre}/postgres", isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as c:
            await c.execute(text(f'DROP DATABASE IF EXISTS "{name}{_OWNED}" WITH (FORCE)'))
    finally:
        await admin.dispose()


def _actor():
    from app.core.auth import CurrentUser
    from app.models.auth import UserRole

    return CurrentUser(user_id=_SYS_USER, email="awssync@example.invalid",
                       role=UserRole.ADMIN, team_id=None)


def _svc():
    from app.core.cache_invalidation import CacheInvalidationManager
    from app.services.model_service import ModelService

    cache = MagicMock(spec=CacheInvalidationManager)
    cache.invalidate = AsyncMock()
    return ModelService(cache_mgr=cache)


async def _tiered_alias(session):
    """헤드 시드에 있는 gpt-5.6 티어 alias 하나를 고른다(0032 runtime + 0038 long)."""
    from sqlalchemy import select

    from app.models.model import ModelAlias, ModelPricing

    row = (await session.execute(
        select(ModelPricing).where(ModelPricing.long_context_threshold_tokens.is_not(None))
        .where(ModelPricing.effective_until.is_(None))
    )).scalars().first()
    if row is None:
        pytest.skip("티어 있는 pricing 행 없음 — 0038 미적용")
    alias = (await session.execute(
        select(ModelAlias).where(ModelAlias.alias == row.model_alias)
    )).scalar_one()
    return alias, row


@_pg
async def test_short_only_put_preserves_tier_on_real_db(dsn):
    """⚠️ 핵심. long 필드를 생략한 단가 PUT 뒤에도 272K 티어가 실 DB 에 남아 있어야 한다."""
    from datetime import datetime, timezone

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models.model import ModelPricing
    from app.schemas.models import PricingRequest

    engine = create_async_engine(dsn)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    svc = _svc()
    try:
        async with Session() as db:
            alias, prior = await _tiered_alias(db)
            prior_thr = prior.long_context_threshold_tokens
            prior_long_in = prior.long_context_input_price_per_1k_tokens
            data = PricingRequest(  # long 필드 전부 생략, short 만 새 값
                input_price_per_1k_tokens=Decimal("0.009900"),
                output_price_per_1k_tokens=Decimal("0.011100"),
                effective_from=datetime.now(timezone.utc),
            )
            with patch("app.services.model_service.audit_logger") as audit:
                audit.log = AsyncMock()
                await svc.set_pricing(db, alias=alias.alias, data=data, actor=_actor())
            await db.commit()

        async with Session() as db:
            cur = (await db.execute(
                select(ModelPricing).where(ModelPricing.model_alias == alias.alias)
                .where(ModelPricing.effective_until.is_(None))
            )).scalar_one()
        assert cur.input_price_per_1k_tokens == Decimal("0.009900")  # short 갱신됨
        # ★ 티어는 지워지지 않고 이전 행에서 승계됐다
        assert cur.long_context_threshold_tokens == prior_thr
        assert cur.long_context_input_price_per_1k_tokens == prior_long_in
    finally:
        await engine.dispose()


@_pg
async def test_sync_writes_aws_long_rates_to_real_db(dsn):
    """sync_aws_pricing 이 AWS long 달러 요율을 실제 model_pricings 의 0038 컬럼에 쓴다."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models.model import ModelPricing
    from app.services.aws_price_list import AwsModelPrice, normalize_pmid

    engine = create_async_engine(dsn)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    svc = _svc()
    try:
        async with Session() as db:
            alias, _ = await _tiered_alias(db)
            bare = normalize_pmid(alias.provider_model_id)
        aws = {bare: AwsModelPrice(
            provider_model_id=bare, region_code="us-gov-west-1", endpoint="mantle",
            short={"input": Decimal("0.001100"), "output": Decimal("0.007700"),
                   "cache_read": Decimal("0.000110"), "cache_write": Decimal("0.001375")},
            long={"input": Decimal("0.002200"), "output": Decimal("0.011550"),
                  "cache_read": Decimal("0.000220"), "cache_write": Decimal("0.002750")},
        )}
        async with Session() as db:
            with patch("app.services.model_service.fetch_bedrock_prices", return_value=aws), \
                 patch("app.services.model_service.audit_logger") as audit:
                audit.log = AsyncMock()
                resp = await svc.sync_aws_pricing(
                    db, aliases=[alias.alias], region_code="us-gov-west-1", actor=_actor()
                )
            await db.commit()
        assert resp.synced == [alias.alias], (resp.synced, resp.skipped)

        async with Session() as db:
            cur = (await db.execute(
                select(ModelPricing).where(ModelPricing.model_alias == alias.alias)
                .where(ModelPricing.effective_until.is_(None))
            )).scalar_one()
        assert cur.input_price_per_1k_tokens == Decimal("0.001100")
        assert cur.long_context_threshold_tokens == 272000
        assert cur.long_context_input_price_per_1k_tokens == Decimal("0.002200")
        assert cur.long_context_output_price_per_1k_tokens == Decimal("0.011550")
        # 단일 30m 캐시-쓰기 SKU 를 long 5m·1h 둘 다에 반영
        assert cur.long_context_cache_creation_5m_price_per_1k_tokens == Decimal("0.002750")
        assert cur.long_context_cache_creation_1h_price_per_1k_tokens == Decimal("0.002750")
    finally:
        await engine.dispose()
