# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import os
import re
import uuid
from datetime import date
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser
from app.core.config import get_settings
from app.core.environment import is_development_environment
from app.core.exceptions import ForbiddenError, STSVerificationError
from app.models.auth import User, UserRole
from app.models.budget import BudgetConfig, BudgetPolicy, BudgetScope, PeriodType
from app.repositories.budget_repository import BudgetRepository
from app.repositories.user_repository import UserRepository
from app.schemas.cli import SetupRequest, SetupResponse, ToolConfig, VirtualKeyIssueRequest, VirtualKeyIssueResponse
from app.services.key_service import KeyService

logger = structlog.get_logger()

# IAM Arn patterns:
#   SSO:      arn:aws:sts::{account}:assumed-role/{role_name}/{session_name}
#   IAM user: arn:aws:iam::{account}:user/{user_name}
ARN_ASSUMED_ROLE = re.compile(r"arn:aws:sts::(\d+):assumed-role/([^/]+)/(.+)")
ARN_IAM_USER = re.compile(r"arn:aws:iam::(\d+):user/(.+)")
# IRSA 가 파드에 넣는 역할 ARN — 그 계정 ID 가 "admin-api 가 떠 있는 계정" 이다.
_ROLE_ARN_ACCOUNT = re.compile(r"^arn:aws[a-z-]*:iam::(\d{12}):role/")


def own_account_id() -> str | None:
    """admin-api 가 떠 있는 AWS 계정. IRSA 가 없으면(로컬 실행 등) None."""
    match = _ROLE_ARN_ACCOUNT.match(os.environ.get("AWS_ROLE_ARN", ""))
    return match.group(1) if match else None


def allowed_account_ids(settings) -> frozenset[str] | None:
    """CLI 로 virtual key 를 받을 수 있는 AWS 계정들. ``None`` 은 "대조하지 않는다".

    1. ``ALLOWED_AWS_ACCOUNT_IDS`` 가 있으면 그 목록.
    2. 없으면 admin-api 자신의 계정 하나(``AWS_ROLE_ARN``).
    3. 둘 다 없으면 — 개발 환경에서는 대조하지 않는다(IRSA 없는 로컬 실행에는 "자기 계정" 이
       없다). 그 밖의 환경에서는 **빈 집합**이다: 어느 계정을 믿을지 모르면 아무도 믿지 않는다.
    """
    configured = frozenset(settings.ALLOWED_AWS_ACCOUNT_IDS)
    if configured:
        return configured
    own = own_account_id()
    if own:
        return frozenset({own})
    if is_development_environment(settings.APP_ENV):
        return None
    return frozenset()


class CLIService:
    def __init__(self, key_service: KeyService) -> None:
        self._key_service = key_service

    async def verify_sts_and_issue_key(
        self,
        session: AsyncSession,
        *,
        redis=None,
        data: VirtualKeyIssueRequest,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> VirtualKeyIssueResponse:
        settings = get_settings()

        # ── SSRF Prevention: validate STS URL host ──
        parsed = urlparse(data.sts_request.url)
        host = parsed.hostname or ""
        allowed_hosts = {f"sts.{r}.amazonaws.com" for r in settings.ALLOWED_STS_REGIONS}
        allowed_hosts.add("sts.amazonaws.com")  # global endpoint
        if host not in allowed_hosts:
            raise STSVerificationError(f"STS URL host not allowed: {host}")

        # Validate Action=GetCallerIdentity only
        qs = parse_qs(parsed.query)
        action = qs.get("Action", [None])[0]
        if action != "GetCallerIdentity":
            raise STSVerificationError("Only GetCallerIdentity action is allowed")

        # ── Forward pre-signed request to AWS STS ──
        # Presigned URL (query-string auth): GET without headers
        # Header-signed request (SigV4): POST with Authorization header
        is_presigned = "X-Amz-Signature" in parsed.query
        import os
        ca_bundle = os.environ.get("SSL_CERT_FILE", True)
        async with httpx.AsyncClient(timeout=10.0, verify=ca_bundle) as client:
            try:
                if is_presigned:
                    resp = await client.get(data.sts_request.url)
                else:
                    resp = await client.post(
                        data.sts_request.url,
                        headers=data.sts_request.headers,
                    )
            except httpx.RequestError as e:
                raise STSVerificationError(f"STS request failed: {e}")

        if resp.status_code != 200:
            raise STSVerificationError(f"STS returned status {resp.status_code}")

        # Parse STS XML response to extract Arn
        arn = self._extract_arn_from_sts_response(resp.text)
        if not arn:
            raise STSVerificationError("Could not extract Arn from STS response")

        # ── IAM identity check ──
        match_role = ARN_ASSUMED_ROLE.match(arn)
        match_user = ARN_IAM_USER.match(arn)

        if match_role:
            account_id = match_role.group(1)
            role_name = match_role.group(2)
            session_name = match_role.group(3)
        elif match_user:
            account_id = match_user.group(1)
            role_name = None  # IAM users don't have a role name
            session_name = match_user.group(2)
        else:
            raise STSVerificationError(f"Unexpected Arn format: {arn}")

        # 계정 대조가 먼저다. 역할 이름은 어느 계정에서나 지을 수 있으므로, 이름만 보면 남의
        # 계정에서 같은 이름의 역할을 만든 사람이 그대로 통과한다. ARN 은 STS 가 서명을 검증하고
        # 돌려준 값이라 계정 ID 를 믿을 수 있다.
        allowed_accounts = allowed_account_ids(settings)
        if allowed_accounts is not None and account_id not in allowed_accounts:
            logger.warning("cli.aws_account_not_allowed", account_id=account_id, arn=arn)
            raise ForbiddenError(f"AWS account not allowed: {account_id}")

        # 역할 허용 목록이 있으면 **그 역할만** 통과한다. IAM 사용자는 역할이 없으므로 통과하지
        # 못한다 — 예전에는 role_name 이 None 이면 이 검사를 건너뛰어, 목록을 아무리 좁혀도
        # IAM 사용자는 그냥 들어왔다.
        if settings.ALLOWED_IAM_ROLES and role_name not in settings.ALLOWED_IAM_ROLES:
            raise ForbiddenError(f"IAM role not allowed: {role_name or 'IAM user (no role)'}")

        # ── Auto-provisioning ──
        user_repo = UserRepository(session)
        user = await user_repo.get_by_sso_subject(arn)

        if user is None:
            email = session_name if "@" in session_name else f"{session_name}@unknown"
            user = User(
                id=uuid.uuid4(),
                email=email,
                display_name=session_name,
                role=UserRole.DEVELOPER,
                sso_subject=arn,
                team_id=uuid.UUID(settings.DEFAULT_TEAM_ID) if settings.DEFAULT_TEAM_ID else None,
                is_active=True,
            )
            user = await user_repo.create_user(user)
            logger.info("cli.user_auto_provisioned", user_id=str(user.id), email=email, role="DEVELOPER")

            # Create default budget
            if settings.DEFAULT_USER_BUDGET_USD > 0 and settings.DEFAULT_TEAM_ID:
                budget_repo = BudgetRepository(session)
                budget = BudgetConfig(
                    id=uuid.uuid4(),
                    scope=BudgetScope.USER,
                    scope_id=user.id,
                    max_budget_usd=Decimal(str(settings.DEFAULT_USER_BUDGET_USD)),
                    period_type=PeriodType.MONTHLY,
                    policy=BudgetPolicy.HARD_BLOCK,
                    allocated_by=user.id,
                    effective_from=date.today(),
                    is_active=True,
                )
                await budget_repo.upsert_config(budget)

        if not user.is_active:
            raise ForbiddenError("User account is deactivated")

        # ── Issue VK ──
        actor = CurrentUser(
            user_id=user.id,
            email=user.email,
            role=user.role,
            team_id=user.team_id,
        )
        key_result = await self._key_service.issue_key(
            session,
            user_id=user.id,
            actor=actor,
            sso_session_expires_at=data.sso_session_expires_at,
            ip_address=ip_address,
            request_id=request_id,
            user=user,  # STS 경로도 같은 session 의 user 객체 재사용 (issue_key 내부 재조회 skip)
        )

        # Gather rate limit / budget info for response
        budget_repo = BudgetRepository(session)
        user_budget = await budget_repo.get_first_active_config(BudgetScope.USER, user.id)

        # ── Cache budget config and model configs in Redis for Gateway Proxy ──
        if redis is not None:
            await self._cache_for_gateway(redis, user, user_budget)

        return VirtualKeyIssueResponse(
            virtual_key=key_result.virtual_key or "",
            expires_at=key_result.expires_at,
            gateway_endpoint=f"http://gateway-proxy:8000",
            otel_endpoint="",
            user_id=str(user.id),
            team_id=str(user.team_id) if user.team_id else None,
            max_budget_usd=user_budget.max_budget_usd if user_budget else None,
            used_usd=None,
            tpm_limit=None,
            rpm_limit=None,
        )

    def get_setup_config(self, data: SetupRequest) -> SetupResponse:
        tool_configs: dict[str, ToolConfig] = {}
        for tool in data.detected_tools:
            if tool == "claude-code":
                tool_configs[tool] = ToolConfig(type="bedrock", use_api_key_helper=True)
            else:
                tool_configs[tool] = ToolConfig(type="openai", auth="jwt")

        return SetupResponse(
            bedrock_endpoint="http://gateway-proxy:8000",
            openai_endpoint="http://gateway-proxy:8000/v1",
            otel_endpoint="",
            tool_configs=tool_configs,
        )

    @staticmethod
    async def _cache_for_gateway(redis, user, user_budget) -> None:
        """Cache budget config and model configs in Redis for Gateway Proxy."""

        # Budget config — format must match what budget_check.lua expects.
        #
        # ⚠️ `redis.set` 을 직접 쓰면 안 된다. 이 페이로드에는 `app_clients`(앱별 하위
        #    한도 게이트)가 없고, `ex=` 도 없어서 **기존 TTL 까지 버린다**(Redis SET 은
        #    KEEPTTL 없이 TTL 을 폐기한다). 이 함수는 CLI 로그인과 OIDC 교환 **매번**
        #    실행되므로, 예전 코드는 로그인 한 번으로 그 사용자의 앱별 예산 하위 한도를
        #    영구히 꺼 버렸다 — 게이트웨이는 키가 **없을 때만** 재수화하기 때문에
        #    present-and-persistent 가 된 키는 자가치유되지 않는다.
        #    core/budget_cache.write_user_budget_config 는 Lua 로 병합하며 app_clients
        #    를 보존하고 TTL 을 다시 건다.
        if user_budget:
            from app.core.budget_cache import write_user_budget_config
            from app.services.budget_service import BUDGET_CONFIG_CACHE_TTL

            await write_user_budget_config(
                redis,
                user.id,
                {
                    "limit_usd": str(user_budget.max_budget_usd),
                    "policy": user_budget.policy.value.lower(),
                    "soft_limit_pct": 110,
                    "throttle_rpm_pct": 50,
                    "thresholds": [80, 90, 100],
                },
                BUDGET_CONFIG_CACHE_TTL,
            )

        # NOTE: Model config Redis cache 는 gateway-proxy 의 router_service 가 DB 에서
        # 동적으로 로드하여 관리 (ModelConfigSchema 포맷). admin-api 가 여기서 미리 넣던
        # 하드코딩 fallback 은 옛 스키마 (`model_id` / `api_format: "BEDROCK"` / `status: "active"`)
        # 를 쓰고 있어, gateway-proxy 가 read 시 ModelConfigSchema validation 실패 → 401.
        # 이미 정상 DB seed 가 있으므로 이 fallback populate 는 제거.

    @staticmethod
    def _extract_arn_from_sts_response(xml_text: str) -> str | None:
        # Use defusedxml instead of stdlib xml.etree.
        # EN: stdlib `xml.etree` is vulnerable to XML attacks (XXE, billion
        #     laughs, external entity expansion). The XML we parse here is
        #     the response body of an STS GetCallerIdentity call signed by
        #     the operator, but a man-in-the-middle or compromised STS
        #     endpoint could in principle inject malicious XML. defusedxml
        #     disables entity resolution and DTD loading by default and is
        #     the standard hardened drop-in replacement.
        # KO: stdlib `xml.etree` 는 XML 공격(XXE, billion laughs, 외부
        #     엔티티 확장)에 취약합니다. 여기서 파싱하는 XML 은 운영자가
        #     서명한 STS GetCallerIdentity 호출의 응답이지만, 중간자 공격
        #     또는 STS endpoint 가 변조될 가능성을 고려해 강화된
        #     drop-in replacement 인 defusedxml 을 사용합니다. defusedxml
        #     은 기본적으로 엔티티 resolution 과 DTD 로딩을 비활성화합니다.
        from defusedxml import ElementTree as ET
        from xml.etree.ElementTree import ParseError

        try:
            root = ET.fromstring(xml_text)
            ns = {"sts": "https://sts.amazonaws.com/doc/2011-06-15/"}
            arn_elem = root.find(".//sts:Arn", ns)
            if arn_elem is not None and arn_elem.text:
                return arn_elem.text
            # Try without namespace
            arn_elem = root.find(".//{*}Arn")
            if arn_elem is not None and arn_elem.text:
                return arn_elem.text
        except ParseError:
            pass
        return None
