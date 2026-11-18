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
from app.core.oidc_identity import reconcile_role
from app.models.auth import Department, Team, User, UserRole
from app.repositories.user_repository import UserRepository

logger = structlog.get_logger()

BATCH_SIZE = 500  # 유저 upsert 배치 commit 단위. 초과 시 commit + expunge_all 로
                  # identity map 을 비워 sync_all 메모리 상한을 확보한다.


def _needs_update(
    snap: dict, *, email: str, name: str, team_id, role, enabled: bool
) -> bool:
    """prefetch gate 판정. _upsert_one_user 의 update 분기와 1:1 동일 조건이어야 한다.
    (email 이 truthy 이고 다르면) OR display_name/team_id/role/is_active 변경.
    role 은 _upsert_one_user 가 저장할 값(reconcile_role 을 거친 값)이어야 한다."""
    return (
        (bool(email) and snap["email"] != email)
        or snap["display_name"] != name
        or snap["team_id"] != team_id
        or snap["role"] != role
        or snap["is_active"] != enabled
    )


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


@dataclass
class _TeamCache:
    """sync_all 진입 시 1회 구축하는 팀/부서 인덱스. expunge 후에도 안전하도록
    ORM 객체가 아니라 PK(uuid)만 보유한다."""
    teams: dict[tuple[uuid.UUID, str], uuid.UUID] = field(default_factory=dict)
    #: 팀 id → teams.leader_user_id (리더가 지정된 팀만). 역할 판정에 쓴다 — 사용자마다 팀을 다시 읽지 않는다.
    leaders: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)
    depts_by_name: dict[str, uuid.UUID] = field(default_factory=dict)
    default_dept_id: uuid.UUID | None = None
    org_id: uuid.UUID | None = None


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

        # 0. 팀/부서 캐시 1회 구축 (멤버 미포함 경량 조회) — 그룹마다 전체 그래프를
        #    반복 로드하던 것을 대체(OOM 주원인 제거).
        cache = await self._build_team_cache(repo)

        # 1. Cognito 그룹 목록
        try:
            groups = await asyncio.to_thread(self._list_all_groups, user_pool_id)
        except Exception as e:
            result.errors.append(f"Failed to list groups: {e}")
            return result

        # 2. 그룹별 멤버 수집 → user_sub 별 프로필/그룹 축적
        user_map: dict[str, dict] = {}
        # group_name → team_id (팀 매핑 가능한 그룹만)
        group_team_id: dict[str, uuid.UUID] = {}

        for group in groups:
            group_name = group["GroupName"]
            parsed = self._parse_group(group_name)
            if parsed is not None:
                dept_name, team_name = parsed
                result.groups_synced += 1
                try:
                    tid = await self._ensure_team_id(
                        repo, session, cache, dept_name, team_name
                    )
                    group_team_id[group_name] = tid
                except Exception as e:
                    result.errors.append(f"Failed to ensure team {team_name}: {e}")

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

        # 2b. 전체 유저 목록으로 보완 (그룹 삭제 시 누락 방지)
        try:
            all_cognito_users = await asyncio.to_thread(
                self._list_all_users, user_pool_id
            )
            for cu in all_cognito_users:
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

        # 3. 사용자별 DB upsert (배치 commit + expunge 로 메모리 상한)
        # 유저 upsert 전 기존 유저 스냅샷 일괄 prefetch (per-user 조회 N+1 제거).
        existing_snaps = await repo.prefetch_users_by_subjects(set(user_map.keys()))

        seen_sso_subjects: set[str] = set()
        default_team_id = uuid.UUID(settings.DEFAULT_TEAM_ID)
        processed = 0

        for sub, info in user_map.items():
            email = info["email"]
            name = info["name"] or email
            enabled = info["enabled"]
            user_groups: list[str] = info["groups"]

            team_id: uuid.UUID | None = None
            for g in user_groups:
                if g in group_team_id:
                    team_id = group_team_id[g]
                    break
            if team_id is None:
                team_id = default_team_id

            # skip 여부와 무관하게 항상 seen 에 추가 — reconcile(4단계)이 skip 된
            # 유저를 잘못 비활성화하지 않도록 (불변식).
            seen_sso_subjects.add(sub)
            role = self._derive_role(email, user_groups)
            leader_id = cache.leaders.get(team_id)

            # gate: 확실히 안 바뀐 기존 유저는 upsert 를 건너뜀 (DB 왕복 0).
            # snap None(신규/재생성 새 sub)은 절대 skip 하지 않는다 — email reconcile
            # 규약(_upsert_one_user 내부)이 보존돼야 하므로 항상 upsert 경로로.
            # ⚠️ role 은 _upsert_one_user 가 저장할 값과 비교한다. derive_role 값(ADMIN/DEVELOPER)과
            #    비교하면 팀 리더는 언제나 '바뀜' 이라 동기화마다 upsert 를 다시 탄다.
            snap = existing_snaps.get(sub)
            if snap is not None and not _needs_update(
                snap, email=email, name=name, team_id=team_id,
                role=reconcile_role(role, leads_own_team=leader_id == snap["id"]), enabled=enabled,
            ):
                continue

            try:
                # SAVEPOINT 로 각 유저 upsert 를 격리. flush 실패(예: email UNIQUE
                # 충돌)가 발생해도 savepoint 만 롤백되고 외부 트랜잭션은 건강하게
                # 유지된다 → 이후 배치/잔여 commit + reconcile/stale-team 이 정상 수행.
                async with session.begin_nested():
                    await self._upsert_one_user(
                        repo, sub=sub, email=email, name=name, enabled=enabled,
                        team_id=team_id, role=role, team_leader_id=leader_id, result=result,
                    )
            except Exception as e:
                result.errors.append(f"Failed to sync user {email}: {e}")

            processed += 1
            if processed % BATCH_SIZE == 0:
                await session.commit()
                session.expunge_all()

        # 잔여분 commit
        await session.commit()

        # 4. Cognito 에 없는 OIDC 사용자 비활성화 — bulk UPDATE (ORM 전량 로드 제거).
        #    반드시 전체 upsert 완료 후. seen 에 없는 유저만 비활성화.
        if settings.COGNITO_SYNC_DEACTIVATE_MISSING:
            try:
                deactivated = await repo.deactivate_missing_oidc_users(
                    seen_sso_subjects, settings.OIDC_PROVIDER_NAME
                )
                result.users_deactivated += deactivated
                await session.commit()
            except Exception as e:
                result.errors.append(f"Failed to deactivate missing users: {e}")

        # 5. Cognito 에 없는 팀 정리 — 멤버 이동을 위해 members 포함 조회 유지.
        synced_team_ids = set(group_team_id.values())
        try:
            all_teams = await repo.list_all_teams()
            for team in all_teams:
                if team.id == default_team_id:
                    continue
                if team.id in synced_team_ids:
                    continue
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
        enabled: bool, team_id: uuid.UUID, role: UserRole,
        team_leader_id: uuid.UUID | None, result: SyncResult,
    ) -> User:
        """단일 사용자 upsert. sync_all 의 163-217 블록과 동일 규약(get_by_sso_subject
        → get_by_email fallback(재생성 시 새 sub 재조정) → 없으면 create).

        ``role`` 은 derive_role 값, ``team_leader_id`` 는 ``team_id`` 팀의 leader_user_id 다(없으면 None).
        upsert 된(생성/갱신) User 를 반환한다 — sync_user 가 응답 user_id 확보용."""
        settings = get_settings()
        # Track whether this call mutated an existing row. We flush ONCE at the
        # end and only then increment users_updated — so a deferred flush failure
        # (e.g. an email UNIQUE collision that the enclosing SAVEPOINT will roll
        # back) surfaces here and the counter is NOT bumped for a change that did
        # not persist.
        updated = False
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
                updated = True
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
            if email and existing.email != email:
                existing.email = email
                updated = True
            if existing.display_name != name:
                existing.display_name = name
                updated = True
            if existing.team_id != team_id:
                existing.team_id = team_id
                updated = True
            # IdP 는 팀 리더를 모른다 — 이 동기화가 정한 팀이 이 사람을 리더로 가리킬 때만 TEAM_LEADER 다.
            role = reconcile_role(role, leads_own_team=team_leader_id == existing.id)
            if existing.role != role:
                existing.role = role
                updated = True
            if existing.is_active != enabled:
                existing.is_active = enabled
                updated = True
            if updated:
                # Flush pending mutations so a failure (e.g. UNIQUE email
                # collision) raises HERE, before we count. If it raises, the
                # exception propagates to the caller's try/except (recorded in
                # result.errors) and users_updated stays untouched.
                await repo.flush()
                result.users_updated += 1
            return existing

    async def _resolve_team_id_from_groups(
        self, repo: UserRepository, session, user_groups: list[str], result: SyncResult,
        cache: _TeamCache | None = None,
    ) -> uuid.UUID:
        """그룹 목록 → 첫 매핑 가능한 팀 id (없으면 DEFAULT_TEAM_ID).

        ``cache`` 를 주면 팀/부서 조회를 캐시로 해결한다. 전체 동기화(:98)는 이미
        캐시를 만들어 쓰는데 per-entity 경로(sync_user/sync_group)만 캐시 없이
        ``_ensure_team`` 을 타고 있었다 — 그건 그룹 하나당 팀·부서를 DB 에서 다시 찾는
        경로라, 그룹이 많은 사용자에서 라운드트립이 그룹 수만큼 쌓인다.
        캐시 인프라는 이미 있었고 이 호출부만 연결이 빠져 있었다.

        ⚠️ ``cache=None`` 이면 예전 그대로 ``_ensure_team`` 을 쓴다. 기본값이라
        기존 호출부의 동작은 바뀌지 않는다(호출 호환).
        """
        settings = get_settings()
        for g in user_groups:
            parsed = self._parse_group(g)
            if parsed is None:
                continue
            dept_name, team_name = parsed
            try:
                if cache is not None:
                    # ORM 객체가 아니라 uuid 를 돌려주므로 session expunge 후에도 안전.
                    return await self._ensure_team_id(
                        repo, session, cache, dept_name, team_name
                    )
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

        # 팀/부서 캐시를 한 번 만들어 그룹 해석에 넘긴다. 없으면 그룹 하나당 팀·부서를
        # DB 에서 다시 찾는다(그룹이 많은 사용자에서 라운드트립이 그룹 수만큼 쌓인다).
        cache = await self._build_team_cache(repo)
        team_id = await self._resolve_team_id_from_groups(
            repo, session, user_groups, result, cache=cache
        )
        role = self._derive_role(email, user_groups)

        try:
            user = await self._upsert_one_user(
                repo, sub=sub, email=email, name=name, enabled=enabled,
                team_id=team_id, role=role, team_leader_id=cache.leaders.get(team_id), result=result,
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
            # ⚠️ ORM 객체(`team`)가 아니라 uuid 를 받는다. 아래 멤버 upsert 루프는 중간에
            #    flush/commit 을 하는데, ORM 객체를 들고 있으면 expunge 이후 속성 접근이
            #    DetachedInstanceError 로 터질 수 있다(_ensure_team_id 의 docstring 참조).
            cache = await self._build_team_cache(repo)
            team_id = await self._ensure_team_id(
                repo, session, cache, dept_name, team_name
            )
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
                    team_id=team_id, role=self._derive_role(email, [group_name]),
                    team_leader_id=cache.leaders.get(team_id), result=result,
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

    async def _build_team_cache(self, repo: UserRepository) -> _TeamCache:
        """멤버 미포함 경량 조회로 팀/부서 인덱스를 1회 구축.

        sync_all 이 그룹마다 list_all_orgs/list_all_teams(members selectinload)를
        반복 호출하던 것을 대체한다 — OOM 의 주원인."""
        settings = get_settings()
        cache = _TeamCache()
        for d in await repo.list_departments_lite():
            cache.depts_by_name[d.name] = d.id
        for t in await repo.list_teams_lite():
            cache.teams[(t.dept_id, t.name)] = t.id
            if t.leader_user_id is not None:
                cache.leaders[t.id] = t.leader_user_id
        # 신규 부서 생성 시 배치할 org id 를 1회 확보(멤버 그래프 미로드).
        cache.org_id = await repo.get_first_org_id()
        try:
            cache.default_dept_id = uuid.UUID(settings.DEFAULT_DEPT_ID)
        except (ValueError, TypeError):
            cache.default_dept_id = None
        return cache

    async def _ensure_team_id(
        self, repo: UserRepository, session, cache: _TeamCache,
        dept_name: str | None, team_name: str,
    ) -> uuid.UUID:
        """캐시 기반 팀 확보. 없으면 부서·팀을 생성하고 캐시에 즉시 반영한 뒤
        team_id(uuid)를 반환한다. ORM 객체를 반환하지 않아 expunge 후에도 안전."""
        settings = get_settings()

        # 부서 id 결정
        if dept_name is None:
            dept_id = cache.default_dept_id or uuid.UUID(settings.DEFAULT_DEPT_ID)
        else:
            dept_id = cache.depts_by_name.get(dept_name)
            if dept_id is None:
                if cache.org_id is None:
                    raise ValueError(f"Cannot resolve department for {dept_name}")
                new_dept = Department(
                    id=uuid.uuid4(), org_id=cache.org_id, name=dept_name
                )
                await repo.create_department(new_dept)
                dept_id = new_dept.id
                cache.depts_by_name[dept_name] = dept_id

        key = (dept_id, team_name)
        cached = cache.teams.get(key)
        if cached is not None:
            return cached

        team = Team(id=uuid.uuid4(), dept_id=dept_id, name=team_name)
        await repo.create_team(team)
        cache.teams[key] = team.id
        return team.id

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
