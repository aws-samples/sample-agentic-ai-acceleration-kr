# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.auth import Department, Organization, Team, User, UserRole


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── Organization ──

    async def get_default_org(self) -> Organization | None:
        stmt = select(Organization).limit(1)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    # ── Department ──

    async def create_department(self, dept: Department) -> Department:
        self._session.add(dept)
        await self._session.flush()
        return dept

    async def get_department(self, dept_id: uuid.UUID) -> Department | None:
        return await self._session.get(Department, dept_id)

    async def get_department_by_name(
        self, org_id: uuid.UUID, name: str
    ) -> Department | None:
        """동일 org 내에서 이름으로 부서 단건 조회.

        OIDC 그룹 매핑 hot path 에서 사용. ``list_all_orgs`` (모든 org + 부서 +
        팀 + 멤버 selectinload) 의 대체 경로로 도입.
        """
        stmt = (
            select(Department)
            .where(Department.org_id == org_id, Department.name == name)
            .limit(1)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    # ── Team ──

    async def create_team(self, team: Team) -> Team:
        self._session.add(team)
        await self._session.flush()
        return team

    async def get_team(self, team_id: uuid.UUID) -> Team | None:
        return await self._session.get(Team, team_id)

    async def get_team_by_dept_and_name(
        self, dept_id: uuid.UUID, name: str
    ) -> Team | None:
        """(dept_id, name) 으로 팀 단건 조회.

        OIDC 그룹 매핑 hot path 에서 사용. ``list_all_teams`` (selectinload 로
        전 팀 + 멤버 fetch) 의 O(N) 경로를 인덱스 기반 단건 조회로 대체.
        """
        stmt = (
            select(Team)
            .where(Team.dept_id == dept_id, Team.name == name)
            .limit(1)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def set_leader(self, team_id: uuid.UUID, user_id: uuid.UUID) -> Team | None:
        team = await self.get_team(team_id)
        if team is None:
            return None
        team.leader_user_id = user_id
        return team

    # ── User ──

    async def get_user(self, user_id: uuid.UUID) -> User | None:
        return await self._session.get(User, user_id)

    async def get_by_sso_subject(self, sso_subject: str) -> User | None:
        stmt = select(User).where(User.sso_subject == sso_subject)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_email(self, email: str) -> User | None:
        stmt = select(User).where(User.email == email)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def create_user(self, user: User) -> User:
        self._session.add(user)
        await self._session.flush()
        return user

    async def update_user_team(self, user_id: uuid.UUID, team_id: uuid.UUID) -> User | None:
        user = await self.get_user(user_id)
        if user is None:
            return None
        user.team_id = team_id
        return user

    async def update_user_role(self, user_id: uuid.UUID, role: UserRole) -> User | None:
        user = await self.get_user(user_id)
        if user is None:
            return None
        user.role = role
        return user

    async def list_all_orgs(self) -> list[Organization]:
        stmt = (
            select(Organization)
            .options(
                selectinload(Organization.departments)
                .selectinload(Department.teams)
                .selectinload(Team.members)
            )
            .order_by(Organization.name)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().unique().all())

    async def list_all_teams(self) -> list[Team]:
        stmt = (
            select(Team)
            .options(
                selectinload(Team.members),
                selectinload(Team.department),
            )
            .order_by(Team.name)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().unique().all())

    async def list_users(
        self,
        *,
        team_id: uuid.UUID | None = None,
        department_id: uuid.UUID | None = None,
        is_active: bool | None = None,
        email: str | None = None,
        cursor: uuid.UUID | None = None,
        limit: int = 50,
    ) -> list[User]:
        stmt = select(User).order_by(User.created_at.desc())
        if team_id:
            stmt = stmt.where(User.team_id == team_id)
        if department_id:
            stmt = stmt.join(Team, User.team_id == Team.id).where(Team.dept_id == department_id)
        if is_active is not None:
            stmt = stmt.where(User.is_active == is_active)
        if email:
            # email 은 unique 컬럼 → exact 매칭(0/1건). DB 는 email 을 정규화 없이
            # Cognito 값 그대로 저장하므로 대소문자 무시(lower) 비교.
            stmt = stmt.where(func.lower(User.email) == email.lower())
        if cursor:
            stmt = stmt.where(User.id < cursor)
        stmt = stmt.limit(limit)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

