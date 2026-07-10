# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Key management integration tests — router + service + mocked DB."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

from app.models.auth import KeyStatus, VirtualKey


class TestKeyRevokeFlow:
    async def test_revoke_key_returns_204(self, client: AsyncClient, admin_headers: dict):
        key_id = str(uuid.uuid4())

        from tests.unit.conftest import TEST_ENCRYPTION_KEY
        from app.core.encryption import AESEncryptionService

        enc = AESEncryptionService(TEST_ENCRYPTION_KEY)
        raw_key = "vk-" + "a" * 64
        encrypted = enc.encrypt(raw_key)

        vk = MagicMock(spec=VirtualKey)
        vk.key_value_encrypted = encrypted

        with patch("app.services.key_service.KeyRepository") as MockRepo, \
             patch("app.services.key_service.audit_logger") as mock_audit:
            MockRepo.return_value.revoke = AsyncMock(return_value=vk)
            mock_audit.log = AsyncMock()

            resp = await client.delete(
                f"/admin/keys/{key_id}",
                headers=admin_headers,
            )

        assert resp.status_code == 204

    async def test_revoke_nonexistent_key_returns_404(self, client: AsyncClient, admin_headers: dict):
        key_id = str(uuid.uuid4())

        with patch("app.services.key_service.KeyRepository") as MockRepo:
            MockRepo.return_value.revoke = AsyncMock(return_value=None)

            resp = await client.delete(
                f"/admin/keys/{key_id}",
                headers=admin_headers,
            )

        assert resp.status_code == 404


class TestKeyListFlow:
    async def test_list_keys_returns_paginated(self, client: AsyncClient, admin_headers: dict):
        vk1 = MagicMock(spec=VirtualKey)
        vk1.id = uuid.uuid4()
        vk1.key_prefix = "vk-abc1234"
        vk1.user_id = uuid.uuid4()
        vk1.status = KeyStatus.ACTIVE
        vk1.issued_at = datetime.now(timezone.utc)
        vk1.expires_at = datetime.now(timezone.utc) + timedelta(days=90)
        vk1.last_used_at = None
        vk1.created_at = datetime.now(timezone.utc)

        with patch("app.services.key_service.KeyRepository") as MockRepo:
            MockRepo.return_value.list_keys = AsyncMock(return_value=[vk1])

            resp = await client.get("/admin/keys?limit=10", headers=admin_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 1
        assert "pagination" in data
        assert data["pagination"]["has_more"] is False
