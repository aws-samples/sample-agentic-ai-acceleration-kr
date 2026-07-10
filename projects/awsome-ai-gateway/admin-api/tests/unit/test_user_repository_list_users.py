# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Unit tests for UserRepository.list_users email filter.

Mock-based: verifies the compiled SQL applies a case-insensitive exact-match
condition on email when the filter is given, and omits it otherwise.
Actual row-matching against Postgres is covered by e2e.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.repositories.user_repository import UserRepository


def _session_capturing_stmt() -> tuple[AsyncMock, list]:
    """AsyncSession mock that records the statement passed to execute()."""
    captured: list = []
    result = MagicMock()
    result.scalars.return_value.all.return_value = []

    async def _execute(stmt):
        captured.append(stmt)
        return result

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=_execute)
    return session, captured


@pytest.mark.asyncio
async def test_list_users_email_filter_is_case_insensitive_exact():
    session, captured = _session_capturing_stmt()
    repo = UserRepository(session)

    await repo.list_users(email="Foo@Bar.com")

    sql = str(captured[0].compile(compile_kwargs={"literal_binds": True}))
    # case-insensitive: email is lower()'d on both sides, not a LIKE/contains
    assert "lower(" in sql.lower()
    assert "foo@bar.com" in sql.lower()
    assert "like" not in sql.lower()


@pytest.mark.asyncio
async def test_list_users_no_email_filter_omits_email_condition():
    session, captured = _session_capturing_stmt()
    repo = UserRepository(session)

    await repo.list_users()

    sql = str(captured[0].compile(compile_kwargs={"literal_binds": True}))
    # base select(User) always selects the email column; the discriminator for
    # "no email filter applied" is the absence of the lower()-based WHERE term.
    assert "lower(" not in sql.lower()
