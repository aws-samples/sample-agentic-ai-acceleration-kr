# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.model import RateLimitScope
from app.repositories.model_repository import RateLimitConfigRepository


async def test_deactivate_configs_marks_active_user_rows_inactive(mock_session: AsyncMock):
    user_id = uuid.uuid4()

    mock_result = MagicMock()
    mock_result.rowcount = 1
    mock_session.execute.return_value = mock_result

    repo = RateLimitConfigRepository(mock_session)
    affected = await repo.deactivate_configs(RateLimitScope.USER, user_id)

    assert affected == 1
    mock_session.execute.assert_called_once()

    stmt_arg = mock_session.execute.call_args.args[0]
    compiled = str(stmt_arg.compile(compile_kwargs={"literal_binds": True}))
    assert "is_active" in compiled
    # PostgreSQL UUID literal is rendered without dashes in SQLAlchemy compile output
    assert user_id.hex in compiled or str(user_id) in compiled
    assert "USER" in compiled


async def test_deactivate_configs_returns_zero_when_none(mock_session: AsyncMock):
    mock_result = MagicMock()
    mock_result.rowcount = 0
    mock_session.execute.return_value = mock_result

    repo = RateLimitConfigRepository(mock_session)
    affected = await repo.deactivate_configs(RateLimitScope.USER, uuid.uuid4())

    assert affected == 0
    mock_session.execute.assert_called_once()

    stmt_arg = mock_session.execute.call_args.args[0]
    compiled = str(stmt_arg.compile(compile_kwargs={"literal_binds": True}))
    assert "is_active" in compiled
    assert "WHERE" in compiled
