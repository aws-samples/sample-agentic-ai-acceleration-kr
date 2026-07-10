# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_logger
from app.core.auth import CurrentUser
from app.core.cache_invalidation import CacheInvalidationManager
from app.core.exceptions import NotFoundError
from app.models.auth import Department, Team, User, UserRole
from app.models.budget import BudgetScope
from app.models.model import RateLimitScope
from app.repositories.budget_repository import BudgetRepository
from app.repositories.model_repository import RateLimitConfigRepository
from app.repositories.user_repository import UserRepository
from app.schemas.users import DepartmentResponse, OrgNodeMeta, OrgTreeNode, TeamListItem, TeamResponse, UserResponse
from app.services.key_service import KeyService

logger = structlog.get_logger()


class UserTeamService:
    def __init__(self, cache_mgr: CacheInvalidationManager, key_service: KeyService) -> None:
        self._cache_mgr = cache_mgr
        self._key_service = key_service

    async def create_department(
        self,
        session: AsyncSession,
        *,
        name: str,
        org_id: uuid.UUID | None = None,
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> DepartmentResponse:
        repo = UserRepository(session)

        # Default to first org if not specified
        if org_id is None:
            org = await repo.get_default_org()
            if org is None:
                raise NotFoundError("Organization", "default")
            org_id = org.id

        dept = Department(
            id=uuid.uuid4(),
            org_id=org_id,
            name=name,
        )
        await repo.create_department(dept)

        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            action="CREATE_DEPARTMENT",
            resource_type="Department",
            resource_id=str(dept.id),
            changes={"after": {"name": name}},
            ip_address=ip_address,
            request_id=request_id,
        )

        return DepartmentResponse(
            id=str(dept.id),
            name=dept.name,
            org_id=str(dept.org_id),
            created_at=dept.created_at,
        )

    async def create_team(
        self,
        session: AsyncSession,
        *,
        name: str,
        department_id: uuid.UUID,
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> TeamResponse:
        repo = UserRepository(session)
        dept = await repo.get_department(department_id)
        if dept is None:
            raise NotFoundError("Department", str(department_id))

        team = Team(
            id=uuid.uuid4(),
            dept_id=department_id,
            name=name,
        )
        await repo.create_team(team)

        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            action="CREATE_TEAM",
            resource_type="Team",
            resource_id=str(team.id),
            changes={"after": {"name": name, "department_id": str(department_id)}},
            ip_address=ip_address,
            request_id=request_id,
        )

        return TeamResponse(
            id=str(team.id),
            name=team.name,
            department_id=str(team.dept_id),
            leader_user_id=str(team.leader_user_id) if team.leader_user_id else None,
            created_at=team.created_at,
        )

    async def set_team_leader(
        self,
        session: AsyncSession,
        *,
        team_id: uuid.UUID,
        user_id: uuid.UUID,
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> TeamResponse:
        repo = UserRepository(session)

        team = await repo.set_leader(team_id, user_id)
        if team is None:
            raise NotFoundError("Team", str(team_id))

        # Update user role to TEAM_LEADER
        await repo.update_user_role(user_id, UserRole.TEAM_LEADER)

        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            action="SET_TEAM_LEADER",
            resource_type="Team",
            resource_id=str(team_id),
            changes={"after": {"leader_user_id": str(user_id)}},
            ip_address=ip_address,
            request_id=request_id,
        )

        return TeamResponse(
            id=str(team.id),
            name=team.name,
            department_id=str(team.dept_id),
            leader_user_id=str(team.leader_user_id) if team.leader_user_id else None,
            created_at=team.created_at,
        )

    async def transfer_user(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        new_team_id: uuid.UUID,
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> UserResponse:
        repo = UserRepository(session)

        user = await repo.get_user(user_id)
        if user is None:
            raise NotFoundError("User", str(user_id))
        old_team_id = user.team_id

        # BR-BUD-04: Deactivate existing user budget configs
        budget_repo = BudgetRepository(session)
        await budget_repo.deactivate_configs(BudgetScope.USER, user_id)

        # BR-RL: Deactivate existing USER scope rate-limit configs
        rl_repo = RateLimitConfigRepository(session)
        await rl_repo.deactivate_configs(RateLimitScope.USER, user_id)

        # Capture VK hashes before team update (still valid pre-transfer)
        vk_hashes: list[str] = await self._key_service.list_active_vk_hashes_for_user(session, user_id)

        # Transfer team
        user = await repo.update_user_team(user_id, new_team_id)

        # Cache invalidation: user context + budget config cache + all VK caches
        # RL config 캐시는 gateway-proxy 가 다른 namespace (rl:config:USER:<uid>:<model>) 로 관리.
        # 현재 admin-api 에서 직접 invalidate 하는 표준 패턴이 없어 5분 TTL 자연 만료에 의존.
        # 별도 후속 작업으로 wildcard invalidate 또는 통합 namespace 정리 필요.
        cache_keys: list[str] = [
            f"user_context:{user_id}",
            f"budget:config:user:{{{user_id}}}",
            *[f"key:cache:vk:{h}" for h in vk_hashes],
        ]
        await self._cache_mgr.invalidate(cache_keys, session=session)

        # Reverse-index swap: move VK hashes from old team set to new team set
        if old_team_id is not None and vk_hashes:
            try:
                await self._cache_mgr.swap_reverse_index_membership(
                    old_key=f"team:vk_hashes:{old_team_id}",
                    new_key=f"team:vk_hashes:{new_team_id}",
                    members=vk_hashes,
                    session=session,
                )
            except Exception:
                logger.exception(
                    "transfer_user.reverse_index_swap_failed",
                    user_id=str(user_id),
                    old_team_id=str(old_team_id),
                    new_team_id=str(new_team_id),
                )

        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            action="TRANSFER_USER",
            resource_type="User",
            resource_id=str(user_id),
            changes={
                "before": {"team_id": str(old_team_id) if old_team_id else None},
                "after": {"team_id": str(new_team_id)},
            },
            ip_address=ip_address,
            request_id=request_id,
        )

        return self._to_user_response(user)

    async def list_users(
        self,
        session: AsyncSession,
        *,
        team_id: uuid.UUID | None = None,
        department_id: uuid.UUID | None = None,
        is_active: bool | None = None,
        email: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> tuple[list[UserResponse], bool]:
        repo = UserRepository(session)
        cursor_uuid = uuid.UUID(cursor) if cursor else None
        users = await repo.list_users(
            team_id=team_id,
            department_id=department_id,
            is_active=is_active,
            email=email,
            cursor=cursor_uuid,
            limit=limit + 1,
        )
        has_more = len(users) > limit
        if has_more:
            users = users[:limit]
        return [self._to_user_response(u) for u in users], has_more

    async def list_teams(self, session: AsyncSession) -> list[TeamListItem]:
        repo = UserRepository(session)
        teams = await repo.list_all_teams()
        return [
            TeamListItem(
                id=str(t.id),
                name=t.name,
                department_id=str(t.dept_id),
                department_name=t.department.name if t.department else None,
                leader_user_id=str(t.leader_user_id) if t.leader_user_id else None,
                member_count=len([m for m in t.members if m.is_active]),
            )
            for t in teams
        ]

    async def get_org_tree(self, session: AsyncSession) -> OrgTreeNode | None:
        repo = UserRepository(session)
        orgs = await repo.list_all_orgs()
        if not orgs:
            return None
        org = orgs[0]

        dept_nodes: list[OrgTreeNode] = []
        for dept in org.departments:
            team_nodes: list[OrgTreeNode] = []
            for team in dept.teams:
                leader = next(
                    (m for m in team.members if team.leader_user_id and m.id == team.leader_user_id),
                    None,
                )
                active_members = [m for m in team.members if m.is_active]
                if not active_members:
                    continue
                member_nodes: list[OrgTreeNode] = []
                for member in active_members:
                    member_nodes.append(
                        OrgTreeNode(
                            id=str(member.id),
                            name=member.display_name,
                            type="USER",
                            children=[],
                            meta=OrgNodeMeta(
                                member_count=None,
                                leader_name=None,
                                email=member.email,
                                role=member.role.value,
                                team_name=team.name,
                            ),
                        )
                    )
                team_nodes.append(
                    OrgTreeNode(
                        id=str(team.id),
                        name=team.name,
                        type="TEAM",
                        children=member_nodes,
                        meta=OrgNodeMeta(
                            member_count=len(active_members),
                            leader_name=leader.display_name if leader else None,
                            email=leader.email if leader else None,
                            role=None,
                            team_name=None,
                        ),
                    )
                )
            if not team_nodes:
                continue
            dept_nodes.append(
                OrgTreeNode(
                    id=str(dept.id),
                    name=dept.name,
                    type="DEPARTMENT",
                    children=team_nodes,
                    meta=OrgNodeMeta(
                        member_count=sum(len(n.children) for n in team_nodes),
                        leader_name=None,
                        email=None,
                        role=None,
                        team_name=None,
                    ),
                )
            )

        return OrgTreeNode(
            id=str(org.id),
            name=org.name,
            type="ORGANIZATION",
            children=dept_nodes,
            meta=OrgNodeMeta(
                member_count=sum(len(t.members) for d in org.departments for t in d.teams),
                leader_name=None,
                email=None,
                role=None,
                team_name=None,
            ),
        )

    @staticmethod
    def _to_user_response(user: User) -> UserResponse:
        return UserResponse(
            id=str(user.id),
            email=user.email,
            display_name=user.display_name,
            role=user.role,
            team_id=str(user.team_id) if user.team_id else None,
            team_name=user.team.name if user.team else None,
            is_active=user.is_active,
            created_at=user.created_at,
        )
