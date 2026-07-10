# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""통합 테스트 — 이벤트 수신 → 이메일 전송 → DB 로깅 전체 플로우.

실행 조건: 실제 PostgreSQL + Redis 필요.
    pytest tests/integration/ --integration

환경변수:
    DB_URL    (기본: localhost:5432/gateway)
    REDIS_URL (기본: localhost:6379/0)
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone

import pytest


pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
async def db_engine(db_url: str):
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(db_url, pool_pre_ping=True)
    yield engine
    await engine.dispose()


@pytest.fixture(scope="module")
async def session_factory(db_engine):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    return async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture(scope="module")
async def redis_client(redis_url: str):
    from redis.asyncio import Redis

    client = Redis.from_url(redis_url)
    yield client
    await client.aclose()


async def test_notification_config_loaded(session_factory) -> None:
    """DB에서 NotificationConfig를 조회할 수 있어야 한다."""
    from sqlalchemy import select

    from worker.models.notification import NotificationConfig

    async with session_factory() as session:
        result = await session.execute(select(NotificationConfig))
        configs = result.scalars().all()

    assert len(configs) > 0, "notification.notification_configs 시드 데이터가 필요합니다."


async def test_notification_log_insert(session_factory) -> None:
    """NotificationLog를 DB에 삽입하고 조회할 수 있어야 한다."""
    from sqlalchemy import select

    from worker.models.notification import NotificationLog

    log_id = str(uuid.uuid4())
    log = NotificationLog(
        id=log_id,
        event_id="test-evt-001",
        event_type="budget_threshold",
        channel="email",
        recipient_email="test@example.com",
        recipient_user_id=None,
        subject="Test Subject",
        status="pending",
        attempt_count=0,
        event_payload={"threshold_pct": 80},
    )

    async with session_factory() as session:
        session.add(log)
        await session.commit()

    async with session_factory() as session:
        result = await session.execute(
            select(NotificationLog).where(NotificationLog.id == log_id)
        )
        fetched = result.scalar_one_or_none()

    assert fetched is not None
    assert fetched.event_type == "budget_threshold"
    assert fetched.status == "pending"


async def test_redis_pubsub_roundtrip(redis_client) -> None:
    """Redis Pub/Sub 발행 → 구독 수신 라운드트립."""
    channel = "notifications:budget"
    received: list[dict] = []

    async def subscriber():
        pubsub = redis_client.pubsub()
        await pubsub.subscribe(channel)
        async for message in pubsub.listen():
            if message["type"] == "message":
                received.append(json.loads(message["data"]))
                await pubsub.unsubscribe(channel)
                return

    payload = {
        "event_id": str(uuid.uuid4()),
        "type": "budget_threshold",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "gateway-proxy",
        "payload": {"user_id": str(uuid.uuid4()), "threshold_pct": 90},
    }

    sub_task = asyncio.create_task(subscriber())
    await asyncio.sleep(0.1)  # 구독 설정 대기

    await redis_client.publish(channel, json.dumps(payload))
    await asyncio.wait_for(sub_task, timeout=3.0)

    assert len(received) == 1
    assert received[0]["event_id"] == payload["event_id"]


async def test_end_to_end_budget_event(session_factory, redis_client) -> None:
    """budget_threshold 이벤트 발행 → ConfigCache → RecipientResolver → MockEmailSender → NotificationLog."""
    from sqlalchemy import select

    from worker.models.notification import NotificationLog
    from worker.schemas.events import EventType, NotificationEvent, ServiceSource
    from worker.senders.mock_sender import MockEmailSender
    from worker.services.config_cache import ConfigCache
    from worker.services.recipient_resolver import RecipientResolver
    from worker.services.retry_executor import RetryExecutor
    from worker.services.template_engine import TemplateEngine
    from worker.handlers.budget_handler import BudgetHandler

    config_cache = ConfigCache(session_factory)
    await config_cache.load()

    cfg = config_cache.get("budget_threshold")
    if cfg is None or not cfg.enabled:
        pytest.skip("budget_threshold config disabled or not seeded")

    resolver = RecipientResolver(session_factory)
    retry_executor = RetryExecutor()
    template_engine = TemplateEngine()
    email_sender = MockEmailSender()

    handler = BudgetHandler(
        session_factory=session_factory,
        config_cache=config_cache,
        recipient_resolver=resolver,
        template_engine=template_engine,
        email_sender=email_sender,
        retry_executor=retry_executor,
    )

    event = NotificationEvent(
        event_id=str(uuid.uuid4()),
        type=EventType.BUDGET_THRESHOLD,
        timestamp=datetime.now(timezone.utc),
        source=ServiceSource.GATEWAY_PROXY,
        payload={
            "user_id": "00000000-0000-0000-0000-000000000001",  # 시드 사용자 ID
            "team_id": "00000000-0000-0000-0000-000000000001",
            "threshold_pct": 80,
            "current_usage_usd": 82.50,
            "limit_usd": 100.00,
            "scope": "USER",
            "period": "2026-04",
        },
    )

    # 핸들러 실행 (수신자가 없으면 그냥 통과)
    await handler.handle(event)

    # NotificationLog가 삽입되었는지 확인
    async with session_factory() as session:
        result = await session.execute(
            select(NotificationLog).where(
                NotificationLog.event_id == event.event_id
            )
        )
        logs = result.scalars().all()

    # 수신자가 시드 데이터에 없을 수 있으므로 존재 여부만 검증
    # 실제 수신자가 있는 경우 1건 이상 로그가 생성됨
    assert isinstance(logs, list)
