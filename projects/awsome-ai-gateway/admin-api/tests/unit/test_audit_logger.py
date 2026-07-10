# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid

import pytest

from app.core.audit import AuditLogger


class TestAuditLogger:
    async def test_log_enqueues_entry(self):
        """log() puts an entry into the queue (non-blocking) and returns None."""
        logger = AuditLogger()

        result = await logger.log(
            None,
            actor_user_id=uuid.uuid4(),
            actor_role="ADMIN",
            action="CREATE_KEY",
            resource_type="VirtualKey",
            resource_id=str(uuid.uuid4()),
            changes={"after": {"key_prefix": "vk-abc123"}},
            ip_address="10.0.0.1",
            request_id="req-001",
        )

        assert result is None
        assert logger.queue_size == 1

    async def test_log_default_result_success(self):
        """log() with defaults puts an entry with result=SUCCESS into the queue."""
        logger = AuditLogger()

        await logger.log(
            None,
            actor_user_id=uuid.uuid4(),
            actor_role="ADMIN",
            action="TEST_ACTION",
            resource_type="Test",
            resource_id="123",
        )

        assert logger.queue_size == 1
        # Peek at the queued entry to verify defaults
        entry = logger._queue.get_nowait()
        assert entry["result"] == "SUCCESS"
        assert entry["ip_address"] == "0.0.0.0"
        assert entry["request_id"] == ""

    async def test_log_preserves_changes_dict(self):
        """log() preserves the changes dict in the queued entry."""
        logger = AuditLogger()

        changes = {"before": {"status": "ACTIVE"}, "after": {"status": "REVOKED"}}
        await logger.log(
            None,
            actor_user_id=uuid.uuid4(),
            actor_role="ADMIN",
            action="REVOKE_KEY",
            resource_type="VirtualKey",
            resource_id="key-1",
            changes=changes,
        )

        entry = logger._queue.get_nowait()
        assert entry["changes"] == changes
