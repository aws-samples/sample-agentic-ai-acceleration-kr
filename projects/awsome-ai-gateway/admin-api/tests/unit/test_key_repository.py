# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Unit tests for KeyRepository.expire_and_create (B: single CTE).

Mock-based: verifies method signature, SQL call shape, and return value parsing.
Actual SQL execution (atomicity, lock semantics, Postgres CTE compatibility) is
covered by Finch e2e in Task B4 — see docs/superpowers/plans/2026-05-08-admin-api-robustness-loadtest.md.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import TextClause

from app.models.auth import KeyStatus, VirtualKey
from app.repositories.key_repository import KeyRepository


def _vk(user_id: uuid.UUID) -> VirtualKey:
    return VirtualKey(
        id=uuid.uuid4(),
        user_id=user_id,
        key_value_encrypted=b"v1:" + b"\x00" * 32,
        key_prefix="vk-aaaaaaaa",
        status=KeyStatus.ACTIVE,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        issued_at=datetime.now(timezone.utc),
    )


def _mock_session_returning(expired_count: int, new_id: uuid.UUID) -> AsyncMock:
    """Build a mock AsyncSession whose execute() returns a row with given fields."""
    row = MagicMock()
    row.expired_count = expired_count
    row.new_id = new_id
    result = MagicMock()
    result.one = MagicMock(return_value=row)
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    return session


@pytest.mark.asyncio
async def test_expire_and_create_first_key():
    """ACTIVE 0개 → CTE returns expired_count=0, INSERT only."""
    user_id = uuid.uuid4()
    new_vk = _vk(user_id)
    session = _mock_session_returning(expired_count=0, new_id=new_vk.id)
    repo = KeyRepository(session)
    expired_count, new_id = await repo.expire_and_create(user_id, new_vk)
    assert expired_count == 0
    assert new_id == new_vk.id


@pytest.mark.asyncio
async def test_expire_and_create_replaces_active():
    """ACTIVE 1개 → CTE returns expired_count=1."""
    user_id = uuid.uuid4()
    new_vk = _vk(user_id)
    session = _mock_session_returning(expired_count=1, new_id=new_vk.id)
    repo = KeyRepository(session)
    expired_count, new_id = await repo.expire_and_create(user_id, new_vk)
    assert expired_count == 1
    assert new_id == new_vk.id


@pytest.mark.asyncio
async def test_expire_and_create_replaces_multi():
    """비정상 ACTIVE 2개 → CTE returns expired_count=2."""
    user_id = uuid.uuid4()
    new_vk = _vk(user_id)
    session = _mock_session_returning(expired_count=2, new_id=new_vk.id)
    repo = KeyRepository(session)
    expired_count, new_id = await repo.expire_and_create(user_id, new_vk)
    assert expired_count == 2


@pytest.mark.asyncio
async def test_expire_and_create_uses_single_sql_call_with_correct_params():
    """Method should issue ONE session.execute() call with a TextClause CTE
    containing the WITH expired/inserted pattern, and the correct bind params."""
    user_id = uuid.uuid4()
    new_vk = _vk(user_id)
    session = _mock_session_returning(expired_count=0, new_id=new_vk.id)
    repo = KeyRepository(session)
    await repo.expire_and_create(user_id, new_vk)

    # Exactly one execute() call
    assert session.execute.await_count == 1
    args, kwargs = session.execute.call_args
    assert len(args) >= 2  # (sql, params)

    # First arg: TextClause CTE containing required keywords
    sql_obj = args[0]
    assert isinstance(sql_obj, TextClause)
    sql_text = str(sql_obj).lower()
    assert "with expired as" in sql_text
    assert "update auth.virtual_keys" in sql_text
    assert "status = 'expired'" in sql_text
    assert "insert into auth.virtual_keys" in sql_text
    assert "returning id" in sql_text  # both CTE branches

    # Second arg: params dict with correct bindings
    params = args[1]
    assert params["user_id"] == user_id
    assert params["new_id"] == new_vk.id
    assert params["enc"] == new_vk.key_value_encrypted
    assert params["prefix"] == new_vk.key_prefix
    assert params["expires_at"] == new_vk.expires_at
    assert params["issued_at"] == new_vk.issued_at
