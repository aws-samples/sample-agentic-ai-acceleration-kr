# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Cognito → DB 동기화 서비스.

Cognito User Pool 에서 전체 사용자/그룹 목록을 가져와
로컬 DB 의 Organization/Department/Team/User 구조를 동기화합니다.

동작:
1. ListGroups → 그룹 파싱 (Claude_<team>, Claude_<dept>_<team>)
2. 각 그룹에 대해 ListUsersInGroup → 멤버 목록
3. DB 에 부서/팀 자동 생성 (없으면)
4. 사용자 upsert (email, display_name, team 매핑)
5. Cognito 에 없는 사용자 비활성화 (선택적)
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import structlog

from app.core.config import get_settings
from app.models.auth import Department, Team, User, UserRole
from app.repositories.user_repository import UserRepository

logger = structlog.get_logger()


@dataclass
class SyncResult:
    groups_synced: int = 0
    users_created: int = 0
    users_updated: int = 0
    users_deactivated: int = 0
    teams_deleted: int = 0
    errors: list[str] = field(default_factory=list)
    # 단일 사용자 sync 대상 유저의 DB id(str). 후속 처리(개인별 allowed_clients/
    # allowed_models)용. Cognito·DB 모두 없는 no-op 이면 None.
    user_id: str | None = None


class CognitoSyncService:
    """Cognito User Pool 전체 동기화."""

    def __init__(self, cognito_client) -> None:
        self._cognito = cognito_client

    async def sync_all(self, session) -> SyncResult:
        """Cognito 에서 그룹/사용자를 가져와 DB 동기화.

        흐름:
        1. 모든 그룹 목록 fetch
        2. 각 그룹의 멤버 fetch → user_sub → group_names 매핑 구축
        3. 팀 매핑 가능한 그룹 파싱 (Claude_<team>, Claude_<dept>_<team>)
        4. 사용자별로 첫 번째 매칭 그룹의 팀에 배정 + role 결정
        """
        import asyncio

        settings = get_settings()
        result = SyncResult()

        user_pool_id = settings.COGNITO_USER_POOL_ID
        if not user_pool_id:
            result.errors.append("COGNITO_USER_POOL_ID not configured")
            return result

        repo = UserRepository(session)

        # 1. Cognito 그룹 목록 가져오기
        try:
            groups = await asyncio.to_thread(
                self._list_all_groups, user_pool_id
            )
        except Exception as e:
            result.errors.append(f"Failed to list groups: {e}")
            return result

        # 2. 그룹별 멤버 수집 → user_sub 별 소속 그룹 + 프로필 정보 축적
        #    user_sub → { "email", "name", "enabled", "groups": [group_name, ...] }
        user_map: dict[str, dict] = {}
        # group_name → Team (팀 매핑 가능한 그룹만)
        team_map: dict[str, Team] = {}

        for group in groups:
            group_name = group["GroupName"]

            # 팀 매핑 가능한 그룹이면 부서/팀 확보
            parsed = self._parse_group(group_name)
            if parsed is not None:
                dept_name, team_name = parsed
                result.groups_synced += 1
                try:
                    team = await self._ensure_team(repo, session, dept_name, team_name)
                    team_map[group_name] = team
                except Exception as e:
                    result.errors.append(f"Failed to ensure team {team_name}: {e}")

            # 그룹 멤버 가져오기 (admin 그룹 포함 — role 결정에 필요)
            try:
                members = await asyncio.to_thread(
                    self._list_users_in_group, user_pool_id, group_name
                )
            except Exception as e:
                result.errors.append(f"Failed to list members of {group_name}: {e}")
                continue

            for member in members:
                sub = self._get_attr(member, "sub") or member.get("Username")
                if not sub:
                    continue
                if sub not in user_map:
                    user_map[sub] = {
                        "email": self._get_attr(member, "email") or "",
                        "name": self._get_attr(member, "name") or self._get_attr(member, "email") or member.get("Username", ""),
                        "enabled": member.get("Enabled", True),
                        "groups": [],
                    }
                user_map[sub]["groups"].append(group_name)

        # 2b. 전체 유저 목록으로 user_map 보완 (그룹 삭제 시 누락 방지)
        try:
            all_cognito_users = await asyncio.to_thread(
                self._list_all_users, user_pool_id
            )
            for cu in all_cognito_users:
                # sub은 Attributes에 있거나 Username 자체가 sub인 경우도 있음
                sub = self._get_attr(cu, "sub") or cu.get("Username")
                if not sub:
                    continue
                if sub not in user_map:
                    user_map[sub] = {
                        "email": self._get_attr(cu, "email") or "",
                        "name": self._get_attr(cu, "name") or self._get_attr(cu, "email") or cu.get("Username", ""),
                        "enabled": cu.get("Enabled", True),
                        "groups": [],
                    }
        except Exception as e:
            result.errors.append(f"Failed to list all users: {e}")

        # 3. 사용자별 DB upsert
        seen_sso_subjects: set[str] = set()

        for sub, info in user_map.items():
            email = info["email"]
            name = info["name"] or email
            enabled = info["enabled"]
            user_groups: list[str] = info["groups"]

            # 팀 결정: 사용자가 속한 그룹 중 첫 번째 팀 매핑 가능한 그룹
            team_id: uuid.UUID | None = None
            for g in user_groups:
                if g in team_map:
                    team_id = team_map[g].id
                    break

            if team_id is None:
                # 팀 매핑 불가 (admin-only 그룹 등) → DEFAULT_TEAM_ID
                team_id = uuid.UUID(settings.DEFAULT_TEAM_ID)

            seen_sso_subjects.add(sub)

            # Role 결정 (사용자의 실제 그룹 기반)
            role = self._derive_role(email, user_groups)

            # DB upsert
            try:
                # 1차: sso_subject 로 조회.
                existing = await repo.get_by_sso_subject(sub)

                # 2차 fallback: email 로 재조회. Cognito 유저 삭제·재생성 시 username
                # (email)은 같아도 새 sub(UUID)가 발급되어 1차 조회가 miss 한다. 이때
                # 같은 email 의 기존 row 를 재사용 + sso_subject 를 새 sub 로 갱신하면
                # 새 sub INSERT → email UNIQUE 충돌(IntegrityError 누적, row 미갱신)을
                # 원천 차단한다. oidc_service._upsert_user 와 동일 규약.
                if existing is None:
                    by_email = await repo.get_by_email(email)
                    if by_email is not None:
                        logger.info(
                            "cognito_sync.sso_subject_reconciled",
                            user_id=str(by_email.id),
                            email=email,
                            old_sso_subject=by_email.sso_subject,
                            new_sso_subject=sub,
                        )
                        by_email.sso_subject = sub
                        existing = by_email
                        result.users_updated += 1

                if existing is None:
                    user = User(
                        id=uuid.uuid4(),
                        email=email,
                        display_name=name,
                        role=role,
                        sso_subject=sub,
                        team_id=team_id,
                        is_active=enabled,
                        provider=settings.OIDC_PROVIDER_NAME,
                    )
                    await repo.create_user(user)
                    result.users_created += 1
                else:
                    changed = False
                    if existing.email != email:
                        existing.email = email
                        changed = True
                    if existing.display_name != name:
                        existing.display_name = name
                        changed = True
                    if existing.team_id != team_id:
                        existing.team_id = team_id
                        changed = True
                    if existing.role != role:
                        existing.role = role
                        changed = True
                    if existing.is_active != enabled:
                        existing.is_active = enabled
                        changed = True
                    if changed:
                        result.users_updated += 1
            except Exception as e:
                result.errors.append(f"Failed to sync user {email}: {e}")

        # 4. Cognito 에 없는 OIDC 사용자 비활성화
        if settings.COGNITO_SYNC_DEACTIVATE_MISSING:
            try:
                all_users = await repo.list_users(limit=10000)
                for user in all_users:
                    if (
                        user.provider == settings.OIDC_PROVIDER_NAME
                        and user.sso_subject not in seen_sso_subjects
                        and user.is_active
                    ):
                        user.is_active = False
                        result.users_deactivated += 1
            except Exception as e:
                result.errors.append(f"Failed to deactivate missing users: {e}")

        # 5. Cognito 에 없는 팀 정리
        # team_map에 있는 팀 = Cognito 그룹에서 매핑된 팀
        # DB의 팀 중 team_map에 없고 Default Team이 아닌 팀 → 멤버를 Default Team으로 이동
        # 팀 자체는 삭제하지 않음 (usage_logs FK 제약)
        default_team_id = uuid.UUID(settings.DEFAULT_TEAM_ID)
        synced_team_ids = {t.id for t in team_map.values()}
        try:
            all_teams = await repo.list_all_teams()
            for team in all_teams:
                if team.id == default_team_id:
                    continue
                if team.id in synced_team_ids:
                    continue
                # 팀 멤버를 Default Team으로 이동
                moved = [m for m in (team.members or []) if m.is_active]
                for member in moved:
                    member.team_id = default_team_id
                if moved:
                    result.teams_deleted += 1
        except Exception as e:
            result.errors.append(f"Failed to clean stale teams: {e}")

        await session.commit()

        logger.info(
            "cognito_sync.completed",
            groups_synced=result.groups_synced,
            users_created=result.users_created,
            users_updated=result.users_updated,
            users_deactivated=result.users_deactivated,
            teams_deleted=result.teams_deleted,
            error_count=len(result.errors),
        )
        return result

    # ── Per-entity sync (260626_comm_customer 항목1-a) ──
    # 사내 관리서비스가 Cognito 신규생성 직후 단일 단위로 호출. 전체 크롤(sync_all)
    # 대신 해당 user/group 만 Cognito 에서 조회 후 upsert. ★ sync_all 의 전역
    # reconciliation(deactivate-missing, stale-team) 은 절대 수행하지 않는다 —
    # 단일 엔티티는 전체 그림이 없어 그 단계를 돌리면 무관한 유저를 대량 비활성화한다.

    async def _upsert_one_user(
        self, repo: UserRepository, *, sub: str, email: str, name: str,
        enabled: bool, team_id: uuid.UUID, role: UserRole, result: SyncResult,
    ) -> User:
        """단일 사용자 upsert. sync_all 의 163-217 블록과 동일 규약(get_by_sso_subject
        → get_by_email fallback(재생성 시 새 sub 재조정) → 없으면 create).

        upsert 된(생성/갱신) User 를 반환한다 — sync_user 가 응답 user_id 확보용."""
        settings = get_settings()
        existing = await repo.get_by_sso_subject(sub)
        if existing is None and email:
            by_email = await repo.get_by_email(email)
            if by_email is not None:
                logger.info(
                    "cognito_sync.sso_subject_reconciled",
                    user_id=str(by_email.id), email=email,
                    old_sso_subject=by_email.sso_subject, new_sso_subject=sub,
                )
                by_email.sso_subject = sub
                existing = by_email
                result.users_updated += 1
        if existing is None:
            new_user = User(
                id=uuid.uuid4(), email=email, display_name=name, role=role,
                sso_subject=sub, team_id=team_id, is_active=enabled,
                provider=settings.OIDC_PROVIDER_NAME,
            )
            await repo.create_user(new_user)
            result.users_created += 1
            return new_user
        else:
            changed = False
            if email and existing.email != email:
                existing.email = email; changed = True
            if existing.display_name != name:
                existing.display_name = name; changed = True
            if existing.team_id != team_id:
                existing.team_id = team_id; changed = True
            if existing.role != role:
                existing.role = role; changed = True
            if existing.is_active != enabled:
                existing.is_active = enabled; changed = True
            if changed:
                result.users_updated += 1
            return existing

    async def _resolve_team_id_from_groups(
        self, repo: UserRepository, session, user_groups: list[str], result: SyncResult,
    ) -> uuid.UUID:
        """그룹 목록 → 첫 매핑 가능한 팀 id (없으면 DEFAULT_TEAM_ID)."""
        settings = get_settings()
        for g in user_groups:
            parsed = self._parse_group(g)
            if parsed is None:
                continue
            dept_name, team_name = parsed
            try:
                team = await self._ensure_team(repo, session, dept_name, team_name)
                return team.id
            except Exception as e:
                result.errors.append(f"Failed to ensure team {team_name}: {e}")
        return uuid.UUID(settings.DEFAULT_TEAM_ID)

    async def sync_user(self, session, username: str) -> SyncResult:
        """단일 사용자만 동기화. DB 에 없어도 신규 생성(고객 명시 케이스).

        username = Cognito Username (이 풀에선 email-attribute 이므로 sub 와 별개일 수
        있으나 admin_get_user(Username) 로 조회되어 sub/attributes 를 정확히 가져온다).
        role/team 은 그룹 멤버십에서 나오므로 admin_list_groups_for_user 도 함께 호출.
        """
        import asyncio
        settings = get_settings()
        result = SyncResult()
        user_pool_id = settings.COGNITO_USER_POOL_ID
        if not user_pool_id:
            result.errors.append("COGNITO_USER_POOL_ID not configured")
            return result

        repo = UserRepository(session)
        try:
            cu = await asyncio.to_thread(self._admin_get_user, user_pool_id, username)
        except Exception as e:
            result.errors.append(f"Failed to get user {username}: {e}")
            return result
        if cu is None:
            # Cognito 에 없음 = AI도구 미사용 전환으로 관리서비스가 삭제한 케이스.
            # 에러가 아니라 DB soft-delete(is_active=False). Cognito 삭제 후엔 sub 를
            # 알 수 없으므로 email(=username) 로 조회. email 충돌 사고 방지를 위해
            # OIDC provider 유저에 한해 비활성화(sync_all 비활성화 원칙과 동일).
            await self._deactivate_missing_user(repo, session, username, result)
            return result

        sub = self._get_attr_admin(cu, "sub") or cu.get("Username") or username
        email = self._get_attr_admin(cu, "email") or ""
        name = self._get_attr_admin(cu, "name") or email or cu.get("Username", "")
        enabled = cu.get("Enabled", True)

        try:
            user_groups = await asyncio.to_thread(
                self._list_groups_for_user, user_pool_id, username
            )
        except Exception as e:
            result.errors.append(f"Failed to list groups for {username}: {e}")
            user_groups = []

        team_id = await self._resolve_team_id_from_groups(
            repo, session, user_groups, result
        )
        role = self._derive_role(email, user_groups)

        try:
            user = await self._upsert_one_user(
                repo, sub=sub, email=email, name=name, enabled=enabled,
                team_id=team_id, role=role, result=result,
            )
            result.user_id = str(user.id)
        except Exception as e:
            result.errors.append(f"Failed to upsert user {email or username}: {e}")

        await session.commit()
        logger.info(
            "cognito_sync.user_synced", username=username,
            created=result.users_created, updated=result.users_updated,
            error_count=len(result.errors),
        )
        return result

    async def _deactivate_missing_user(
        self, repo: UserRepository, session, username: str, result: SyncResult,
    ) -> None:
        """Cognito 에 없는 사용자를 DB 에서 email(=username) 로 찾아 soft-delete.

        - DB 에도 없으면 no-op(user_id 는 None 유지).
        - OIDC provider 유저만 비활성화(비-OIDC 계정 오작동/이메일 충돌 방지).
          찾은 경우 후속 처리를 위해 user_id 는 항상 반환한다.
        - 이미 비활성이면 users_deactivated 를 올리지 않는다(멱등).
        """
        settings = get_settings()
        try:
            user = await repo.get_by_email(username)
        except Exception as e:
            result.errors.append(f"Failed to look up user {username}: {e}")
            return
        if user is None:
            logger.info("cognito_sync.deactivate_noop", username=username)
            return

        result.user_id = str(user.id)
        if user.provider == settings.OIDC_PROVIDER_NAME and user.is_active:
            user.is_active = False
            result.users_deactivated += 1
            await session.commit()
            logger.info(
                "cognito_sync.user_deactivated",
                username=username, user_id=result.user_id,
            )
        else:
            logger.info(
                "cognito_sync.deactivate_skipped",
                username=username, user_id=result.user_id,
                provider=user.provider, is_active=user.is_active,
            )

    async def sync_group(self, session, group_name: str) -> SyncResult:
        """단일 그룹만 동기화: 팀 확보 + 그 그룹 멤버 upsert. 전역 정리 없음."""
        import asyncio
        settings = get_settings()
        result = SyncResult()
        user_pool_id = settings.COGNITO_USER_POOL_ID
        if not user_pool_id:
            result.errors.append("COGNITO_USER_POOL_ID not configured")
            return result

        repo = UserRepository(session)
        parsed = self._parse_group(group_name)
        if parsed is None:
            result.errors.append(f"group not team-mappable (prefix mismatch): {group_name}")
            return result

        dept_name, team_name = parsed
        try:
            team = await self._ensure_team(repo, session, dept_name, team_name)
            result.groups_synced += 1
        except Exception as e:
            result.errors.append(f"Failed to ensure team {team_name}: {e}")
            return result

        try:
            members = await asyncio.to_thread(
                self._list_users_in_group, user_pool_id, group_name
            )
        except Exception as e:
            result.errors.append(f"Failed to list members of {group_name}: {e}")
            return result

        for m in members:
            sub = self._get_attr(m, "sub") or m.get("Username")
            if not sub:
                continue
            email = self._get_attr(m, "email") or ""
            name = self._get_attr(m, "name") or email or m.get("Username", "")
            enabled = m.get("Enabled", True)
            # 그룹 멤버의 role 은 본인의 전체 그룹 기준이 정확하나, 단일 그룹 sync 에서는
            # 그 그룹 기준으로 팀 배정. role 은 이 그룹만으로 보수적 판정(개별 user sync 가
            # 전체 그룹 기준 role 을 정밀 보정). 여기선 team 배정이 주목적.
            try:
                await self._upsert_one_user(
                    repo, sub=sub, email=email, name=name, enabled=enabled,
                    team_id=team.id, role=self._derive_role(email, [group_name]),
                    result=result,
                )
            except Exception as e:
                result.errors.append(f"Failed to upsert member {email or sub}: {e}")

        await session.commit()
        logger.info(
            "cognito_sync.group_synced", group_name=group_name,
            members=len(members), created=result.users_created,
            updated=result.users_updated, error_count=len(result.errors),
        )
        return result

    # ── Cognito API helpers (sync, run in thread) ──

    def _list_all_groups(self, user_pool_id: str) -> list[dict]:
        """Paginate through all groups."""
        groups = []
        params = {"UserPoolId": user_pool_id, "Limit": 60}
        while True:
            resp = self._cognito.list_groups(**params)
            groups.extend(resp.get("Groups", []))
            token = resp.get("NextToken")
            if not token:
                break
            params["NextToken"] = token
        return groups

    def _admin_get_user(self, user_pool_id: str, username: str) -> dict | None:
        """admin_get_user — 단일 사용자 속성 조회. 없으면 None."""
        try:
            return self._cognito.admin_get_user(
                UserPoolId=user_pool_id, Username=username
            )
        except self._cognito.exceptions.UserNotFoundException:
            return None

    def _list_groups_for_user(self, user_pool_id: str, username: str) -> list[str]:
        """admin_list_groups_for_user — 사용자 소속 그룹명 목록(페이지네이션)."""
        names: list[str] = []
        params = {"UserPoolId": user_pool_id, "Username": username, "Limit": 60}
        while True:
            resp = self._cognito.admin_list_groups_for_user(**params)
            names.extend(g["GroupName"] for g in resp.get("Groups", []))
            token = resp.get("NextToken")
            if not token:
                break
            params["NextToken"] = token
        return names

    @staticmethod
    def _get_attr_admin(user: dict, attr_name: str) -> str | None:
        """admin_get_user 응답은 UserAttributes 키(ListUsers 는 Attributes)."""
        for attr in user.get("UserAttributes", []):
            if attr["Name"] == attr_name:
                return attr["Value"]
        return None

    def _list_users_in_group(self, user_pool_id: str, group_name: str) -> list[dict]:
        """Paginate through all users in a group."""
        users = []
        params = {"UserPoolId": user_pool_id, "GroupName": group_name, "Limit": 60}
        while True:
            resp = self._cognito.list_users_in_group(**params)
            users.extend(resp.get("Users", []))
            token = resp.get("NextToken")
            if not token:
                break
            params["NextToken"] = token
        return users

    def _list_all_users(self, user_pool_id: str) -> list[dict]:
        """Paginate through all users in the pool."""
        users = []
        params = {"UserPoolId": user_pool_id, "Limit": 60}
        while True:
            resp = self._cognito.list_users(**params)
            users.extend(resp.get("Users", []))
            token = resp.get("PaginationToken")
            if not token:
                break
            params["PaginationToken"] = token
        return users

    @staticmethod
    def _get_attr(user: dict, attr_name: str) -> str | None:
        """Extract attribute from Cognito user attributes list."""
        for attr in user.get("Attributes", []):
            if attr["Name"] == attr_name:
                return attr["Value"]
        return None

    @staticmethod
    def _parse_group(group_name: str) -> tuple[str | None, str] | None:
        """Cognito 그룹명 → (department_name | None, team_name) 또는 None."""
        settings = get_settings()
        prefix = settings.OIDC_GROUP_PREFIX
        if not group_name.startswith(prefix):
            return None
        tail = group_name[len(prefix):]
        if not tail:
            return None
        parts = tail.split("_")
        if len(parts) == 1:
            team = parts[0]
            return (None, team) if team else None
        if len(parts) == 2:
            dept, team = parts
            return (dept, team) if (dept and team) else None
        return None

    async def _ensure_team(
        self, repo: UserRepository, session, dept_name: str | None, team_name: str
    ) -> Team:
        """부서/팀 조회 또는 생성."""
        settings = get_settings()

        # 부서 결정
        if dept_name is None:
            dept = await repo.get_department(uuid.UUID(settings.DEFAULT_DEPT_ID))
        else:
            orgs = await repo.list_all_orgs()
            dept = None
            for org in orgs:
                for d in org.departments:
                    if d.name == dept_name:
                        dept = d
                        break
                if dept:
                    break
            if dept is None and orgs:
                dept = Department(id=uuid.uuid4(), org_id=orgs[0].id, name=dept_name)
                await repo.create_department(dept)

        if dept is None:
            raise ValueError(f"Cannot resolve department for {dept_name}")

        # 팀 검색
        all_teams = await repo.list_all_teams()
        for t in all_teams:
            if t.dept_id == dept.id and t.name == team_name:
                return t

        # 자동 생성
        team = Team(id=uuid.uuid4(), dept_id=dept.id, name=team_name)
        await repo.create_team(team)
        return team

    @staticmethod
    def _derive_role(email: str, user_groups: list[str]) -> UserRole:
        """ADMIN_EMAILS / ADMIN_GROUPS 매칭 시 ADMIN.

        user_groups 는 해당 사용자가 속한 Cognito 그룹 이름 목록.
        """
        settings = get_settings()
        admin_emails = {e.lower() for e in settings.ADMIN_EMAILS}
        if email and email.lower() in admin_emails:
            return UserRole.ADMIN

        admin_groups = set(settings.ADMIN_GROUPS)
        if any(g in admin_groups for g in user_groups):
            return UserRole.ADMIN

        return UserRole.DEVELOPER
