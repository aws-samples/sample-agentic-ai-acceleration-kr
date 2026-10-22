# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""서버가 끊은 풀 연결을 admin-api 엔진이 버리고 새로 연결하는지 — 실 PG 로 증명한다.

증상
----
RDS Proxy 는 IdleClientTimeout(기본 1800s) 동안 쓰지 않은 클라이언트 연결을 끊는다. 차트의
``poolRecycle`` 은 3600s 라 끊긴 연결이 풀에 그대로 남고, 엔진이 연결을 꺼낼 때 확인하지 않으면
한동안 요청이 없다가 들어온 첫 요청이 ``InterfaceError: connection is closed`` 로 500 이 된다.
대시보드에서는 KPI 카드가 전부 "집계 실패" 로 뜬다. gateway-proxy·두 워커는 이미
``pool_pre_ping=True`` 였고 admin-api(와 같은 이미지를 쓰는 scheduler)만 빠져 있었다.

증명 방법
---------
풀에 돌려놓은 연결의 백엔드를 ``pg_terminate_backend`` 로 죽인다 — 프록시가 유휴 연결을 끊은
것과 클라이언트 입장에서 같은 상태다. 대조군(pre-ping 없음)은 다음 쿼리가 실패해야 하고, 앱의
``create_engine()`` 으로 만든 엔진은 새 백엔드로 성공해야 한다. 대조군이 실패하지 않으면 이 검사는
아무것도 증명하지 못하므로 둘을 같이 둔다.

⚠️ 자기 연결의 백엔드만 죽인다. 스키마는 쓰지 않으므로 PROOF_DSN 이 가리키는 DB 를 그대로 쓴다.
"""

from __future__ import annotations

import asyncio
import os

import pytest

PROOF_DSN = os.environ.get("PROOF_DSN")


def test_admin_engine_pings_pooled_connections():
    """정적 가드 — 실 PG 가 없어도 돈다. 플래그를 지우면 여기서 먼저 잡힌다."""
    from app.core import db

    assert db.engine.sync_engine.pool._pre_ping is True
    assert db.create_engine().sync_engine.pool._pre_ping is True


async def _kill_backend(admin, pid: int) -> None:
    from sqlalchemy import text

    async with admin.connect() as c:
        assert (await c.execute(text("SELECT pg_terminate_backend(:p)"), {"p": pid})).scalar_one()
        for _ in range(50):
            gone = (
                await c.execute(text("SELECT count(*) = 0 FROM pg_stat_activity WHERE pid = :p"), {"p": pid})
            ).scalar_one()
            if gone:
                break
            await asyncio.sleep(0.1)
        else:
            pytest.fail(f"backend {pid} did not exit")
    # 클라이언트 쪽 이벤트 루프가 소켓 종료를 처리할 틈을 준다.
    await asyncio.sleep(0.2)


async def _pooled_pid(engine) -> int:
    from sqlalchemy import text

    async with engine.connect() as c:
        return (await c.execute(text("SELECT pg_backend_pid()"))).scalar_one()


@pytest.fixture
async def admin_engine():
    pytest.importorskip("asyncpg")
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    eng = create_async_engine(PROOF_DSN, poolclass=NullPool)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.mark.skipif(not PROOF_DSN, reason="PROOF_DSN 미설정 — 서버가 끊은 연결은 실 PG 에서만 만들 수 있다.")
async def test_control_without_pre_ping_fails_on_killed_connection(admin_engine):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(PROOF_DSN, pool_size=1, max_overflow=0, pool_pre_ping=False)
    try:
        pid = await _pooled_pid(eng)
        await _kill_backend(admin_engine, pid)
        with pytest.raises(DBAPIError):
            async with eng.connect() as c:
                await c.execute(text("SELECT 1"))
    finally:
        await eng.dispose()


@pytest.mark.skipif(not PROOF_DSN, reason="PROOF_DSN 미설정 — 서버가 끊은 연결은 실 PG 에서만 만들 수 있다.")
async def test_admin_engine_replaces_killed_connection(admin_engine, monkeypatch):
    from sqlalchemy import text

    from app.core import db

    settings = db.get_settings().model_copy(
        update={"DATABASE_URL": PROOF_DSN, "DB_POOL_SIZE": 1, "DB_MAX_OVERFLOW": 0, "DB_STATEMENT_CACHE_SIZE": 0}
    )
    monkeypatch.setattr(db, "get_settings", lambda: settings)
    eng = db.create_engine()
    try:
        pid = await _pooled_pid(eng)
        await _kill_backend(admin_engine, pid)
        async with eng.connect() as c:
            assert (await c.execute(text("SELECT 1"))).scalar_one() == 1
            assert (await c.execute(text("SELECT pg_backend_pid()"))).scalar_one() != pid
    finally:
        await eng.dispose()
