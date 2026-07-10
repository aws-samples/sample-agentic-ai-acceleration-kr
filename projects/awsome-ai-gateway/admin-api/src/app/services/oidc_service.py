# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""OIDC JWT → Virtual Key exchange + 사용자/팀 자동 프로비저닝.

전체 흐름 (POST /v1/auth/exchange):
    1. OIDCVerifier 로 JWT 서명/만료/audience/issuer 검증
    2. claim 추출 (sub, email, name, groups)
    3. (provider, sub) 로 사용자 조회 (없으면 신규 프로비저닝)
    4. groups → team 매핑 (자동 생성 시 budget=$0 INSERT)
    5. 기존 사용자의 team 변경 감지 시 ``UserTeamService.transfer_user`` 호출
       (budget/RL deactivate + VK cache invalidate + reverse-index swap 모두 자동)
    6. role 결정 (ADMIN_EMAILS / ADMIN_GROUPS)
    7. ``KeyService.issue_key`` 로 VK 발급
    8. ``CLIService._cache_for_gateway`` 로 budget/model Redis 캐시 hydrate

이 서비스는 cli_service.verify_sts_and_issue_key 와 **dual-mode 로 공존**합니다.
사용자는 ``users.provider`` 컬럼으로 분리됨 ('sts' vs 'oidc:keycloak' 등).
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser
from app.core.config import get_settings
from app.core.exceptions import ForbiddenError
from app.core.oidc_verifier import OIDCConfigError, OIDCVerifier, OIDCVerifyError
from app.models.auth import Department, Team, User, UserRole
from app.models.budget import BudgetConfig, BudgetPolicy, BudgetScope, PeriodType
from app.repositories.budget_repository import BudgetRepository
from app.repositories.user_repository import UserRepository
from app.schemas.cli import VirtualKeyIssueResponse
from app.services.cli_service import CLIService
from app.services.key_service import KeyService
from app.services.user_team_service import UserTeamService

logger = structlog.get_logger()


class OIDCAuthError(Exception):
    """JWT 검증 실패 (401)."""


class OIDCNotProvisionableError(Exception):
    """필수 claim 누락 또는 매칭 그룹 없음 등 (403)."""


class OIDCService:
    """JWT → VK exchange. cli_service 와 별개의 진입점."""

    def __init__(
        self,
        verifier: OIDCVerifier,
        key_service: KeyService,
        user_team_service: UserTeamService,
        http_client: httpx.AsyncClient,
        cognito_client=None,
    ) -> None:
        self._verifier = verifier
        self._key_service = key_service
        self._user_team_service = user_team_service
        self._http_client = http_client
        # optional: enrich missing email via Cognito AdminGetUser (best-effort).
        # Cognito access_token 에는 email claim 이 없어 ID token 만 갖던 fallback 을 보완.
        self._cognito_client = cognito_client

    async def _email_from_cognito(self, sub: str) -> str | None:
        """Cognito AdminGetUser 로 사용자 email 조회 (best-effort, never raise).

        이 풀들은 UsernameAttributes=[email] 이므로 내부 Username 이 sub(UUID) 와 동일.
        Cognito 미구성/조회 실패 시 None 반환 → 호출부가 <sub>@unknown 으로 fallback.
        """
        settings = get_settings()
        pool = settings.COGNITO_USER_POOL_ID
        if not self._cognito_client or not pool:
            return None
        try:
            resp = await asyncio.to_thread(
                self._cognito_client.admin_get_user,
                UserPoolId=pool,
                Username=sub,
            )
            for attr in resp.get("UserAttributes", []):
                if attr.get("Name") == "email":
                    return attr.get("Value")
            return None
        except Exception:
            logger.warning("oidc.email_from_cognito_failed", sub=sub, exc_info=True)
            return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def exchange_jwt_for_vk(
        self,
        session: AsyncSession,
        *,
        redis,
        token: str,
        device_name: str | None,
        sso_session_expires_at=None,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> VirtualKeyIssueResponse:
        """OIDC access JWT 를 VK 로 교환."""
        settings = get_settings()

        # 1. JWT 검증
        try:
            claims = await self._verifier.verify_async(token, self._http_client)
        except OIDCVerifyError as e:
            raise OIDCAuthError(str(e)) from e
        except OIDCConfigError as e:
            # IDP discovery / JWKS unreachable. 503 에 매핑되도록 별도 예외로 던짐.
            raise

        # 2. claim 추출
        sub = claims.get(settings.OIDC_USER_ID_CLAIM)
        email = claims.get(settings.OIDC_EMAIL_CLAIM, "")
        name = claims.get(settings.OIDC_NAME_CLAIM) or email or sub
        groups = claims.get(settings.OIDC_GROUPS_CLAIM, []) or []
        if not isinstance(groups, list):
            groups = [groups]

        if not sub:
            raise OIDCAuthError(f"missing claim: {settings.OIDC_USER_ID_CLAIM}")
        if not email:
            # email claim 없는 토큰(Cognito access_token 등) → preferred_username 시도.
            email = claims.get("preferred_username") or ""
        if not email:
            # 그래도 없으면 Cognito AdminGetUser 로 enrich (best-effort).
            # 실패 시 마지막 수단으로 <sub>@unknown.
            email = (await self._email_from_cognito(str(sub))) or f"{sub}@unknown"

        # 3. 게이팅 (옵션)
        if settings.OIDC_REQUIRED_GROUP and settings.OIDC_REQUIRED_GROUP not in groups:
            raise OIDCNotProvisionableError(
                f"required_group_missing: {settings.OIDC_REQUIRED_GROUP}"
            )

        # 4. 팀 결정 (필요 시 자동 생성)
        team_id = await self._resolve_team(session, groups)

        # 5. Role 결정
        role = self._derive_role(email, groups)

        # 6. User upsert (provider, sso_subject 기준)
        user, was_created, team_changed = await self._upsert_user(
            session=session,
            sso_subject=str(sub),
            email=str(email),
            display_name=str(name),
            team_id=team_id,
            role=role,
        )

        # 7. 기존 사용자의 팀 변경 → transfer_user (모든 cache/RL/budget hook)
        if not was_created and team_changed:
            actor = CurrentUser(
                user_id=user.id, email=user.email, role=user.role, team_id=team_id,
            )
            try:
                await self._user_team_service.transfer_user(
                    session,
                    user_id=user.id,
                    new_team_id=team_id,
                    actor=actor,
                    ip_address=ip_address,
                    request_id=request_id,
                )
                # transfer_user 가 user.team_id 업데이트
            except Exception:
                logger.exception(
                    "oidc.transfer_user_failed",
                    user_id=str(user.id),
                    new_team_id=str(team_id),
                )
                # 실패해도 인증은 진행 (다음 로그인 때 재시도)

        if not user.is_active:
            raise OIDCNotProvisionableError("user_deactivated")

        # 8. VK 발급 — OIDC 흐름은 spec 에 따라 짧은 TTL (default 24h) 명시.
        actor = CurrentUser(
            user_id=user.id, email=user.email, role=user.role, team_id=user.team_id,
        )
        vk_expires_at = datetime.now(tz=timezone.utc) + timedelta(
            hours=settings.OIDC_VK_TTL_HOURS
        )
        key_result = await self._key_service.issue_key(
            session,
            user_id=user.id,
            actor=actor,
            expires_at=vk_expires_at,
            sso_session_expires_at=sso_session_expires_at,
            ip_address=ip_address,
            request_id=request_id,
            user=user,  # 같은 session upsert 한 객체 재사용 → issue_key 내부 재조회 skip
        )

        # 9. Gateway-proxy 용 budget/model 캐시 hydrate.
        # auto_provision 신규 INSERT 케이스는 USER scope budget 이 없음 → None 반환되는 것이 정상.
        # 기존 유저는 여전히 조회 필요 (최적화 대상 아님).
        budget_repo = BudgetRepository(session)
        user_budget = await budget_repo.get_first_active_config(BudgetScope.USER, user.id)
        if redis is not None:
            await CLIService._cache_for_gateway(redis, user, user_budget)

        return VirtualKeyIssueResponse(
            virtual_key=key_result.virtual_key or "",
            expires_at=key_result.expires_at,
            gateway_endpoint="",  # CLI 가 ANTHROPIC_BASE_URL 에서 가져옴
            otel_endpoint="",
            user_id=str(user.id),
            team_id=str(user.team_id) if user.team_id else None,
            max_budget_usd=user_budget.max_budget_usd if user_budget else None,
            used_usd=None,
            tpm_limit=None,
            rpm_limit=None,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_group(group_name: str) -> tuple[str | None, str] | None:
        """Cognito 그룹명 → (department_name | None, team_name) 또는 None.

        - underscore 1개 ("Claude_team"): (None, "team") — Default Department 팀
        - underscore 2개 ("Claude_dept_team"): ("dept", "team")
        - 그 외 (prefix 불일치, underscore 0/3+, 빈 세그먼트): None → reject

        ``ClaudeAdmin`` 같은 admin 부트스트랩 그룹도 prefix 없이 None 반환
        (팀 매핑 로직 skip, _derive_role 이 ADMIN_GROUPS 별도 체크).
        """
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

    async def _resolve_team(
        self, session: AsyncSession, groups: list[str]
    ) -> uuid.UUID:
        """그룹 리스트 → team_id. 매칭 안 되면 DEFAULT_TEAM_ID 또는 거부."""
        settings = get_settings()
        repo = UserRepository(session)

        for group_name in groups:
            parsed = self._parse_group(group_name)
            if parsed is None:
                continue
            dept_name, team_name = parsed
            team = await self._get_or_create_team(
                repo, session, dept_name=dept_name, team_name=team_name,
            )
            return team.id

        if settings.OIDC_REJECT_UNMATCHED_GROUPS:
            raise OIDCNotProvisionableError("no_matching_team_group")

        return uuid.UUID(settings.DEFAULT_TEAM_ID)

    async def _get_or_create_team(
        self,
        repo: UserRepository,
        session: AsyncSession,
        *,
        dept_name: str | None,
        team_name: str,
    ) -> Team:
        """(dept_name, team_name) 으로 팀 조회/생성.

        - dept_name is None  → Default Department 하위에서 이름으로 팀 조회/생성
        - dept_name 주어짐    → 그 부서 조회/생성 후 그 아래에서 이름으로 팀 조회/생성

        동일 이름 팀이 다른 부서에 있어도 서로 독립적으로 취급 (dept_id + name 유니크 스코프).
        """
        settings = get_settings()

        # 1) 대상 부서 결정 (조회 또는 생성)
        dept = await self._get_or_create_department(repo, dept_name=dept_name)

        # 2) 부서 하위에서 팀 검색 — (dept_id, name) 단건 SELECT.
        # 10k boot storm 부하테스트 v4 에서 list_all_teams (전 팀 + 멤버 selectinload)
        # 를 매 exchange 마다 호출하던 경로가 DB connection 장시간 점유의 주원인이었음.
        team = await repo.get_team_by_dept_and_name(dept.id, team_name)
        if team is not None:
            return team

        # 3) 자동 생성
        team = Team(id=uuid.uuid4(), dept_id=dept.id, name=team_name)
        await repo.create_team(team)

        # Budget=$0 (HARD_BLOCK) — admin 이 예산 관리에서 풀어줄 때까지 deny.
        # team_allowed_models 행은 추가 안 함 → auth_service 가 None=전체 허용으로 처리.
        budget_repo = BudgetRepository(session)
        await budget_repo.upsert_config(
            BudgetConfig(
                id=uuid.uuid4(),
                scope=BudgetScope.TEAM,
                scope_id=team.id,
                max_budget_usd=Decimal("0"),
                period_type=PeriodType.MONTHLY,
                policy=BudgetPolicy.HARD_BLOCK,
                allocated_by=uuid.UUID(settings.SYSTEM_USER_ID),
                effective_from=date.today(),
                is_active=True,
            )
        )

        logger.info(
            "oidc.team_auto_created",
            team_id=str(team.id),
            team_name=team_name,
            dept_id=str(dept.id),
            dept_name=dept.name,
        )
        return team

    async def _get_or_create_department(
        self,
        repo: UserRepository,
        *,
        dept_name: str | None,
    ) -> Department:
        """dept_name is None → Default Department 반환.
        dept_name 주어짐 → 동일 org 에서 이름 매칭, 없으면 자동 생성.
        """
        settings = get_settings()

        if dept_name is None:
            default_dept_id = uuid.UUID(settings.DEFAULT_DEPT_ID)
            dept = await repo.get_department(default_dept_id)
            if dept is None:
                raise OIDCConfigError(
                    f"Default department not found (DEFAULT_DEPT_ID={default_dept_id}). "
                    "Check db/init seed."
                )
            return dept

        # 기명 부서: default org (또는 첫 org) 내에서 이름 매칭 — 단건 SELECT.
        # list_all_orgs 는 org × dept × team × members 를 전부 로드하므로 hot path
        # 에서 회피. 단일 org 운영 가정 (기존 로직과 동일).
        target_org = await repo.get_default_org()
        if target_org is None:
            raise OIDCConfigError(
                "No organization found. Check db/init seed (default org required)."
            )
        dept_existing = await repo.get_department_by_name(target_org.id, dept_name)
        if dept_existing is not None:
            return dept_existing

        # 자동 생성 — 첫 번째 org 아래에 (단일 org 운영 가정)
        dept = Department(id=uuid.uuid4(), org_id=target_org.id, name=dept_name)
        await repo.create_department(dept)
        logger.info(
            "oidc.department_auto_created",
            dept_id=str(dept.id),
            dept_name=dept_name,
            org_id=str(target_org.id),
        )
        return dept

    @staticmethod
    def _derive_role(email: str, groups: list[str]) -> UserRole:
        """ADMIN_EMAILS / ADMIN_GROUPS 둘 중 하나라도 매칭되면 ADMIN."""
        settings = get_settings()
        admin_emails = {e.lower() for e in settings.ADMIN_EMAILS}
        if email and email.lower() in admin_emails:
            return UserRole.ADMIN

        admin_groups = set(settings.ADMIN_GROUPS)
        if any(g in admin_groups for g in groups):
            return UserRole.ADMIN

        return UserRole.DEVELOPER

    async def _upsert_user(
        self,
        *,
        session: AsyncSession,
        sso_subject: str,
        email: str,
        display_name: str,
        team_id: uuid.UUID,
        role: UserRole,
    ) -> tuple[User, bool, bool]:
        """
        Returns (user, was_created, team_changed).

        provider 컬럼 추가 마이그레이션 적용 후 ``(provider, sso_subject)`` 복합키로 식별.
        마이그레이션 전 단계에서는 ``sso_subject`` 단독으로 동작.
        """
        settings = get_settings()
        repo = UserRepository(session)

        # provider scoping 은 마이그레이션 이후 활성. 현재는 sso_subject 만.
        # OIDC 의 sub 가 STS 의 ARN 과 충돌할 가능성은 거의 0 (포맷 다름).
        # 1차: sso_subject 로 조회. Cognito 재생성/재link 로 sub 가 바뀌면 miss 가능.
        existing = await repo.get_by_sso_subject(sso_subject)

        # 2차 fallback: email 로 조회. 같은 사람이 새 sub 로 들어온 경우(이메일 동일)
        # 기존 row 재사용 + sso_subject 갱신 → email UNIQUE 충돌(500) 방지.
        if existing is None:
            by_email = await repo.get_by_email(email)
            if by_email is not None:
                logger.info(
                    "oidc.sso_subject_reconciled",
                    user_id=str(by_email.id),
                    email=email,
                    old_sso_subject=by_email.sso_subject,
                    new_sso_subject=sso_subject,
                )
                by_email.sso_subject = sso_subject
                existing = by_email

        if existing is None:
            user = User(
                id=uuid.uuid4(),
                email=email,
                display_name=display_name,
                role=role,
                sso_subject=sso_subject,
                team_id=team_id,
                is_active=True,
                # provider 컬럼은 마이그레이션 이후 활성. 일단 model 에 setattr 로 시도.
            )
            # provider 속성이 모델에 추가되면 자동 반영. 아직 없으면 silently skip.
            if hasattr(User, "provider"):
                user.provider = settings.OIDC_PROVIDER_NAME
            user = await repo.create_user(user)
            logger.info(
                "oidc.user_auto_provisioned",
                user_id=str(user.id),
                email=email,
                role=role.value,
                team_id=str(team_id),
                provider=settings.OIDC_PROVIDER_NAME,
            )
            return user, True, False

        team_changed = existing.team_id != team_id
        # 기존 사용자: email/role/display_name 만 동기화. team 은 transfer_user 로 별도 처리.
        # email 가드: <sub>@unknown synthetic 이 admin Cognito-sync 가 채운 실제 email 을
        # 덮어쓰지 않도록. (Cognito access_token 엔 email claim 이 없어 매 로그인마다
        # synthetic 으로 회귀하던 버그 봉합.) 기존 email 도 synthetic 이면 갱신 허용.
        incoming_is_synthetic = email.endswith("@unknown")
        existing_is_synthetic = (existing.email or "").endswith("@unknown")
        if existing.email != email and (not incoming_is_synthetic or existing_is_synthetic):
            existing.email = email
        existing.display_name = display_name
        # role 변경은 admin 권한 변동이므로 명시 로그
        if existing.role != role:
            logger.info(
                "oidc.user_role_changed",
                user_id=str(existing.id),
                old_role=existing.role.value,
                new_role=role.value,
            )
            existing.role = role
        return existing, False, team_changed
