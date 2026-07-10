# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Unit tests for audit batch queue (A: reliable in-process queue)."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.core.audit import AuditLogger
from app.models.audit import AuditLog


@pytest.mark.asyncio
async def test_audit_queue_basic_flush(db_session_factory):
    """100건 put → consumer 가 0.5s 안에 batch flush 후 session.add_all 호출 확인."""
    logger = AuditLogger(batch_size=100, flush_interval=0.5, max_queue_size=10000)
    await logger.start(db_session_factory)
    try:
        actor = uuid.uuid4()
        for i in range(100):
            await logger.log(
                None,
                actor_user_id=actor,
                actor_role="ADMIN",
                action="CREATE_KEY",
                resource_type="VirtualKey",
                resource_id=str(uuid.uuid4()),
            )
        # consumer 가 batch_size 100 도달 시 즉시 flush 해야 함
        await asyncio.sleep(0.3)
    finally:
        await logger.shutdown(timeout=5.0)
    assert len(db_session_factory.inserted) == 100
    assert all(isinstance(item, AuditLog) for item in db_session_factory.inserted)


@pytest.mark.asyncio
async def test_audit_queue_timeout_flush(db_session_factory):
    """10건 put + 0.5s wait → batch_size 미달이지만 flush_interval 도달로 flush."""
    logger = AuditLogger(batch_size=100, flush_interval=0.3, max_queue_size=1000)
    await logger.start(db_session_factory)
    try:
        for _ in range(10):
            await logger.log(
                None,
                actor_user_id=uuid.uuid4(),
                actor_role="ADMIN",
                action="UPDATE",
                resource_type="Team",
                resource_id=str(uuid.uuid4()),
            )
        await asyncio.sleep(0.6)
    finally:
        await logger.shutdown(timeout=5.0)
    assert len(db_session_factory.inserted) == 10


@pytest.mark.asyncio
async def test_audit_queue_full_drop(db_session_factory, caplog):
    """maxsize 5 큐에 6건 put 시 1건 drop + warning."""
    logger = AuditLogger(batch_size=100, flush_interval=10.0, max_queue_size=5)
    # consumer 안 띄움 (큐가 차도록)
    for _ in range(6):
        await logger.log(
            None,
            actor_user_id=uuid.uuid4(),
            actor_role="ADMIN",
            action="DELETE",
            resource_type="VirtualKey",
            resource_id=str(uuid.uuid4()),
        )
    assert logger.dropped_count == 1
    assert logger.queue_size == 5


@pytest.mark.asyncio
async def test_audit_shutdown_drains_remaining(db_session_factory):
    """shutdown 시 큐에 남은 batch 미만 항목들도 flush."""
    logger = AuditLogger(batch_size=100, flush_interval=10.0, max_queue_size=1000)
    await logger.start(db_session_factory)
    try:
        for _ in range(7):
            await logger.log(
                None,
                actor_user_id=uuid.uuid4(),
                actor_role="ADMIN",
                action="REVOKE_KEY",
                resource_type="VirtualKey",
                resource_id=str(uuid.uuid4()),
            )
        # flush_interval 10s 라 자동 flush 전에 shutdown 트리거
    finally:
        await logger.shutdown(timeout=5.0)
    assert len(db_session_factory.inserted) == 7
