# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from worker.models.auth import Team, User
from worker.schemas.recipients import Recipient, RecipientRole

logger = structlog.get_logger(__name__)


class RecipientResolver:
    """이벤트 수신자를 역할 기반으로 결정한다 (BR-RCP).

    역할 → 사용자 매핑:
    - affected_user: payload.user_id → auth.users 조회
    - team_leader:   payload.team_id (또는 user의 team_id) → auth.teams.leader_user_id → auth.users 조회
    - admin:         auth.users에서 roles 배열에 'ADMIN' 포함 전체 조회

    중복 이메일 제거 후 반환 (BR-RCP-03).
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def resolve(
        self,
        roles: list[str],
        payload: dict,
    ) -> list[Recipient]:
        """주어진 역할 목록과 이벤트 페이로드로 최종 수신자 목록을 반환한다."""
        seen_emails: set[str] = set()
        recipients: list[Recipient] = []

        async with self._session_factory() as session:
            for role in roles:
                try:
                    new = await self._resolve_role(role, payload, session)
                except Exception:
                    logger.exception("recipient_resolve_error", role=role)
                    continue

                for r in new:
                    if r.email not in seen_emails:
                        seen_emails.add(r.email)
                        recipients.append(r)

        return recipients

    async def _resolve_role(
        self,
        role: str,
        payload: dict,
        session: AsyncSession,
    ) -> list[Recipient]:
        if role == RecipientRole.AFFECTED_USER:
            return await self._resolve_affected_user(payload, session)
        if role == RecipientRole.TEAM_LEADER:
            return await self._resolve_team_leader(payload, session)
        if role == RecipientRole.ADMIN:
            return await self._resolve_admins(session)
        logger.warning("unknown_recipient_role", role=role)
        return []

    async def _resolve_affected_user(
        self, payload: dict, session: AsyncSession
    ) -> list[Recipient]:
        user_id = payload.get("user_id")
        if not user_id:
            return []

        result = await session.execute(
            select(User).where(User.id == str(user_id), User.is_active.is_(True))
        )
        user = result.scalar_one_or_none()

        if user is None:
            logger.warning("affected_user_not_found", user_id=user_id)
            return []

        return [Recipient(email=user.email, name=user.display_name, user_id=user.id, role=RecipientRole.AFFECTED_USER)]

    async def _resolve_team_leader(
        self, payload: dict, session: AsyncSession
    ) -> list[Recipient]:
        team_id = payload.get("team_id")

        # team_id가 payload에 없으면 affected_user의 팀에서 조회
        if not team_id:
            user_id = payload.get("user_id")
            if not user_id:
                return []
            result = await session.execute(select(User.team_id).where(User.id == str(user_id)))
            row = result.scalar_one_or_none()
            if row is None:
                return []
            team_id = row

        result = await session.execute(select(Team).where(Team.id == str(team_id)))
        team = result.scalar_one_or_none()

        if team is None or team.leader_user_id is None:
            # leader 미지정 팀은 오류 아님 (BR-RCP-04)
            logger.debug("team_leader_not_set", team_id=team_id)
            return []

        result = await session.execute(
            select(User).where(User.id == team.leader_user_id, User.is_active.is_(True))
        )
        leader = result.scalar_one_or_none()

        if leader is None:
            logger.warning("team_leader_user_not_found", leader_user_id=team.leader_user_id)
            return []

        return [Recipient(email=leader.email, name=leader.display_name, user_id=leader.id, role=RecipientRole.TEAM_LEADER)]

    async def _resolve_admins(self, session: AsyncSession) -> list[Recipient]:
        result = await session.execute(
            select(User).where(
                User.is_active.is_(True),
                User.role == "ADMIN",
            )
        )
        admins = result.scalars().all()

        if not admins:
            logger.error("no_admin_users_found")
            return []

        return [
            Recipient(email=u.email, name=u.display_name, user_id=u.id, role=RecipientRole.ADMIN)
            for u in admins
        ]
