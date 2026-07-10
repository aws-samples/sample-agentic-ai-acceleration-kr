# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import Settings


class Base(DeclarativeBase):
    pass


def create_db_engine(settings: Settings) -> AsyncEngine:
    connect_args: dict = {}
    if settings.db_ssl_mode != "disable":
        connect_args["ssl"] = settings.db_ssl_mode
    connect_args["statement_cache_size"] = settings.db_statement_cache_size

    return create_async_engine(
        settings.db_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        # 풀 고갈 시 무한대기 대신 db_pool_timeout(기본 10s) 후 TimeoutError 로 fast-fail.
        # (SQLAlchemy 기본 30s → "CPU 정상인데 느림" 지연 시그니처 회피)
        pool_timeout=settings.db_pool_timeout,
        # 오래된/유휴 끊긴 커넥션 선제 폐기 (RDS Proxy/Aurora idle close 대비).
        pool_recycle=settings.db_pool_recycle,
        pool_pre_ping=True,
        connect_args=connect_args,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


async def get_db_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession, None]:
    async with session_factory() as session:
        yield session
