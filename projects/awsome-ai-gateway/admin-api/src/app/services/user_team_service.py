# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_logger
from app.core.auth import CurrentUser
from app.core.cache_invalidation import CacheInvalidationManager
from app.core.exceptions import NotFoundError, ValidationError
from app.models.auth import Department, Team, User, UserRole
from app.models.budget import BudgetScope
from app.models.model import RateLimitScope
from app.repositories.budget_repository import BudgetRepository
from app.repositories.model_repository import RateLimitConfigRepository
from app.repositories.user_repository import UserRepository
from app.schemas.users import (
    DepartmentResponse,
    OrgNodeMeta,
    OrgTreeNode,
    TeamListItem,
    TeamResponse,
    UserResponse,
    UserSearchItem,
)
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

        # 거부는 전부 쓰기 전에 — 거부된 요청은 리더 지정도 누구의 역할도 바꾸지 않는다.
        team = await repo.get_team(team_id)
        if team is None:
            raise NotFoundError("Team", str(team_id))
        user = await repo.get_user(user_id)
        if user is None:
            raise NotFoundError("User", str(user_id))
        # ⚠️ 인가는 role == TEAM_LEADER 와 그 사용자의 team_id 를 "그 팀의 리더" 로 읽는다. 예전엔 아무 사용자나
        #    받아서, 다른 팀 사람을 지정하면 이 팀이 아니라 그 사람 **자기 팀**의 리더가 됐다.
        if user.team_id != team_id:
            raise ValidationError(f"User {user_id} is not a member of team {team_id}")
        if not user.is_active:
            raise ValidationError(f"User {user_id} is deactivated")

        # 리더를 바꾸면 예전 리더는 지금 DEVELOPER 가 된다. 다음 CLI 로그인·동기화를 기다리면 콘솔만 쓰는
        # 사람은 끝까지 리더로 남는다. ADMIN 이거나 이미 다른 팀으로 옮긴 사람은 건드리지 않는다.
        previous_id = team.leader_user_id
        before: dict = {"leader_user_id": str(previous_id) if previous_id else None}
        after: dict = {"leader_user_id": str(user_id)}
        if previous_id is not None and previous_id != user_id:
            previous = await repo.get_user(previous_id)
            if previous is not None and previous.role == UserRole.TEAM_LEADER and previous.team_id == team_id:
                await repo.update_user_role(previous_id, UserRole.DEVELOPER)
                before["previous_leader_role"] = UserRole.TEAM_LEADER.value
                after["previous_leader_role"] = UserRole.DEVELOPER.value

        await repo.set_leader(team_id, user_id)
        # ADMIN 은 그대로 둔다 — 리더 지정이 관리자를 TEAM_LEADER 로 낮추면, 저장된 역할만으로 관리자인
        # 사람은 되돌릴 길이 없다. 지정은 남으므로 IdP 에서 관리자가 아니게 되면 다음 로그인에 TEAM_LEADER 다.
        if user.role != UserRole.ADMIN:
            await repo.update_user_role(user_id, UserRole.TEAM_LEADER)

        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            action="SET_TEAM_LEADER",
            resource_type="Team",
            resource_id=str(team_id),
            changes={"before": before, "after": after},
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

    # 검색 결과 상한. 드롭다운에 보여줄 수 있는 양이고, 타이핑마다 호출되는 경로라
    # 의도적으로 작게 유지한다. 초과분은 truncated=True 로 UI 에 알린다.
    SEARCH_LIMIT = 20
    # 최소 검색어 길이. 1자 검색은 사실상 전량 매칭이라 무의미하고 비싸다.
    SEARCH_MIN_LEN = 2

    async def search_users(
        self,
        session: AsyncSession,
        *,
        term: str,
        limit: int | None = None,
    ) -> tuple[list[UserSearchItem], bool]:
        """이메일/이름 부분 일치 사용자 검색. ``(items, truncated)`` 반환.

        검색어가 ``SEARCH_MIN_LEN`` 미만이면 **DB 를 치지 않고** 빈 결과를 준다.

        비활성 사용자는 제외한다 — 조직 트리도 비활성을 빼고 만들기 때문에
        (get_org_tree 의 ``active_members``), 검색에서 나온 사람을 선택했더니 트리에
        없다는 상태가 생기면 UI 가 조상 경로를 펼칠 대상을 찾지 못한다.
        """
        stripped = term.strip()
        if len(stripped) < self.SEARCH_MIN_LEN:
            return [], False

        effective = limit if limit is not None else self.SEARCH_LIMIT
        repo = UserRepository(session)
        # limit+1 로 조회해 잘림 여부를 판정한다 — 별도 COUNT 쿼리를 피한다.
        rows = await repo.search_users(term=stripped, is_active=True, limit=effective + 1)
        truncated = len(rows) > effective
        if truncated:
            rows = rows[:effective]
        return [
            UserSearchItem(
                id=str(user_id),
                email=email,
                display_name=display_name,
                role=role,
                team_id=str(team_id) if team_id else None,
                team_name=team_name,
            )
            for user_id, email, display_name, role, team_id, team_name in rows
        ], truncated

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
                                team_count=None,
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
                            # TEAM 은 팀이 아니라 사람을 담는다 — 팀 수는 의미가 없다.
                            team_count=None,
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
                        # 하위 팀들의 **활성 사용자** 합. 팀 수가 아니다(team_count 참조).
                        member_count=sum(len(n.children) for n in team_nodes),
                        team_count=len(team_nodes),
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
                # ⚠️ 예전엔 `sum(len(t.members) ...)` 였다 — ORM 의 **전체** 멤버라
                #    비활성 사용자까지 셌다. 반면 팀/부서는 active_members 만 센다.
                #    그래서 조직 합계가 부서 합계의 총합과 달라, 어느 쪽이 맞는지 화면에서
                #    판단할 수 없었다. 이미 만들어 둔 노드에서 세면 계층이 일관된다.
                member_count=sum(
                    len(team.children) for dept in dept_nodes for team in dept.children
                ),
                team_count=sum(len(dept.children) for dept in dept_nodes),
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
