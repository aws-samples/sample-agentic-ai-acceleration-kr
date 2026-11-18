# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
# ⚠️ FastAPI 기본 HTTPException 핸들러(fastapi/exception_handlers.py)가 쓰는 것과 같은
#    판정 함수를 그대로 재사용한다. 204/304 에 본문을 실으면 h11 이 프로토콜 위반으로
#    끊어버리기 때문에, 우리가 봉투를 씌울 때도 같은 예외를 지켜야 한다.
from fastapi.utils import is_body_allowed_for_status_code
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.cache_invalidation import CacheInvalidationManager
from app.core.config import get_settings
from app.core.db import AsyncSessionLocal, create_engine, install_commit_before_response
from app.core.encryption import AESEncryptionService
from app.core.exceptions import (
    AppError,
    BudgetExceededError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    STSVerificationError,
    ValidationError,
)
from app.core.redis_client import create_redis_client

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logger.info("app.starting", env=settings.APP_ENV)

    # dev 로그인이 dev 계열이 아닌 환경에서 켜져 있으면 여기서 멈춘다 — 어떤 리소스도 열기 전이다.
    # 롤링 업데이트에서는 새 파드가 Ready 가 되지 못해 기존 파드가 계속 서빙한다.
    from app.core.dev_login import refuse_dev_login_outside_dev

    refuse_dev_login_outside_dev()

    # ── Initialize resources ──

    # DB engine (module-level, but ensure it's created)
    engine = create_engine()

    # Audit logger — start in-process batch consumer (A: reliable batch queue)
    from app.core.audit import audit_logger
    from app.core.db import AsyncSessionLocal as _audit_session_factory
    await audit_logger.start(_audit_session_factory)
    logger.info("audit_logger.started", batch_size=100, flush_interval_s=0.5)

    # Redis
    redis = await create_redis_client()
    app.state.redis = redis

    # Encryption — fail-fast if key is missing/invalid
    try:
        encryption = AESEncryptionService(
            settings.VIRTUAL_KEY_ENCRYPTION_KEY.get_secret_value()
        )
    except ValueError as exc:
        logger.error(
            "app.startup_failed",
            reason="VIRTUAL_KEY_ENCRYPTION_KEY invalid or missing",
            detail=str(exc),
            hint="Set a 64-char hex value. Generate via: openssl rand -hex 32",
        )
        raise

    # Cache invalidation manager
    cache_mgr = CacheInvalidationManager(redis)
    app.state.cache_mgr = cache_mgr

    # JWT Verifier — load public keys from DB
    from app.core.auth import JWTVerifier

    jwt_verifier = JWTVerifier()
    async with AsyncSessionLocal() as session:
        from sqlalchemy import select
        from app.models.auth import AdminJWTConfig

        result = await session.execute(
            select(AdminJWTConfig).where(AdminJWTConfig.is_active.is_(True))
        )
        configs = result.scalars().all()
        jwt_verifier.load_configs([
            {
                "id": c.id,
                "public_key_pem": c.public_key_pem,
                "issuer": c.issuer,
                "audience": c.audience,
                "algorithm": c.algorithm,
            }
            for c in configs
        ])
    app.state.jwt_verifier = jwt_verifier

    # ── Build services ──
    from app.services.analytics_service import AnalyticsService
    from app.services.budget_service import BudgetService
    from app.services.cli_service import CLIService
    from app.services.key_service import KeyService
    from app.services.model_service import ModelService
    from app.services.rate_limit_service import RateLimitService
    from app.services.service_token_service import ServiceTokenService
    from app.services.team_allowed_model_service import TeamAllowedModelService
    from app.services.user_team_service import UserTeamService

    key_service = KeyService(encryption=encryption, cache_mgr=cache_mgr)
    app.state.key_service = key_service
    app.state.cli_service = CLIService(key_service=key_service)
    # CLI(STS) 키 발급이 어느 AWS 계정을 받는지 기동 로그에 남긴다. 값이 틀렸을 때 운영자가
    # 첫 403 을 받기 전에 알 수 있어야 한다.
    from app.services.cli_service import allowed_account_ids

    _cli_accounts = allowed_account_ids(settings)
    if _cli_accounts is None:
        logger.warning(
            "cli_auth.aws_accounts_unrestricted",
            reason="development environment without AWS_ROLE_ARN or ALLOWED_AWS_ACCOUNT_IDS",
        )
    elif not _cli_accounts:
        logger.error(
            "cli_auth.no_aws_account_allowed",
            reason="set ALLOWED_AWS_ACCOUNT_IDS; there is no AWS_ROLE_ARN to infer the account from",
        )
    else:
        logger.info(
            "cli_auth.allowed_aws_accounts",
            accounts=sorted(_cli_accounts),
            source="ALLOWED_AWS_ACCOUNT_IDS" if settings.ALLOWED_AWS_ACCOUNT_IDS else "AWS_ROLE_ARN",
        )
    budget_service = BudgetService(cache_mgr=cache_mgr)
    app.state.budget_service = budget_service
    app.state.model_service = ModelService(cache_mgr=cache_mgr)
    # 전역 런타임 설정(body logging 토글 등). DB 가 진실의 원천, Redis 는 읽기 캐시.
    from app.services.system_settings_service import SystemSettingsService

    app.state.system_settings_service = SystemSettingsService(cache_mgr=cache_mgr)
    app.state.rate_limit_service = RateLimitService(cache_mgr=cache_mgr)
    app.state.user_team_service = UserTeamService(cache_mgr=cache_mgr, key_service=key_service)
    app.state.team_allowed_model_service = TeamAllowedModelService(cache_mgr=cache_mgr)
    app.state.analytics_service = AnalyticsService()
    app.state.service_token_service = ServiceTokenService()

    # ── OIDC Service (optional — only if OIDC_ISSUER_URL configured) ──
    # OIDC_AUDIENCE 는 optional (Cognito access_token 은 aud claim 없음).
    if settings.OIDC_ISSUER_URL:
        import httpx as _httpx
        from app.core.oidc_verifier import OIDCVerifier
        from app.services.oidc_service import OIDCService

        oidc_http = _httpx.AsyncClient(timeout=10.0)
        oidc_verifier = OIDCVerifier(
            issuer_url=settings.OIDC_ISSUER_URL,
            audience=settings.OIDC_AUDIENCE,
            jwks_cache_ttl_seconds=settings.OIDC_JWKS_CACHE_TTL_SECONDS,
            discovery_url_override=settings.OIDC_DISCOVERY_URL_OVERRIDE or None,
        )
        app.state.oidc_http = oidc_http
        app.state.oidc_verifier = oidc_verifier
        # admin-ui SSO 세션 검증기 — 같은 issuer 의 JWKS 를 쓰되 발급 대상 client 가 다르다
        # (CLI client 가 아니라 admin-ui client). 그래서 인스턴스를 따로 둔다.
        app.state.console_oidc_verifier = None
        if settings.OIDC_ADMIN_UI_CLIENT_ID:
            app.state.console_oidc_verifier = OIDCVerifier(
                issuer_url=settings.OIDC_ISSUER_URL,
                audience="",
                jwks_cache_ttl_seconds=settings.OIDC_JWKS_CACHE_TTL_SECONDS,
                discovery_url_override=settings.OIDC_DISCOVERY_URL_OVERRIDE or None,
                required_client_id=settings.OIDC_ADMIN_UI_CLIENT_ID,
            )
            logger.info("oidc.console_sso_enabled", client_id=settings.OIDC_ADMIN_UI_CLIENT_ID)
        # Cognito access_token 에는 email claim 이 없어, email 누락 시 AdminGetUser 로
        # enrich 하기 위한 client (best-effort). 풀 미구성 시 None.
        oidc_cognito_client = None
        if settings.COGNITO_USER_POOL_ID:
            import boto3
            oidc_cognito_client = boto3.client(
                "cognito-idp", region_name=settings.COGNITO_REGION
            )
        app.state.oidc_service = OIDCService(
            verifier=oidc_verifier,
            key_service=key_service,
            user_team_service=app.state.user_team_service,
            http_client=oidc_http,
            cognito_client=oidc_cognito_client,
        )
        logger.info(
            "oidc.enabled",
            issuer=settings.OIDC_ISSUER_URL,
            audience=settings.OIDC_AUDIENCE,
            provider_name=settings.OIDC_PROVIDER_NAME,
        )
    else:
        app.state.oidc_service = None
        app.state.oidc_http = None
        app.state.oidc_verifier = None
        app.state.console_oidc_verifier = None
        logger.info("oidc.disabled", reason="OIDC_ISSUER_URL or OIDC_AUDIENCE empty")
        if settings.OIDC_ADMIN_UI_CLIENT_ID:
            # issuer 없이는 JWKS 를 찾을 수 없다. 조용히 넘기면 SSO 가 왜 401 인지 알 수 없다.
            logger.warning(
                "oidc.console_sso_ignored",
                reason="OIDC_ADMIN_UI_CLIENT_ID is set but OIDC_ISSUER_URL is empty",
            )

    # TEAM budget config cold-cache warmup
    # init SQL / alembic 백필로 DB에 삽입된 TEAM 예산이 Redis에 없어
    # gateway-proxy 가 team_budget_unset 429 를 반환하는 문제를 startup 시 봉합.
    try:
        async with AsyncSessionLocal() as session:
            warmup_count = await budget_service.warm_team_budget_cache(session)
            logger.info("admin_api.startup.team_budget_warmup_complete", count=warmup_count)
    except Exception as exc:
        logger.warning("admin_api.startup.team_budget_warmup_failed", error=str(exc))

    # P0-③ (MF4): detect pre-existing orphan per-app budgets (no parent USER total)
    # — these bypass the gateway hot path. Read-only WARN log for operator action;
    # the set_user_client_budget guard only prevents NEW orphans.
    try:
        async with AsyncSessionLocal() as session:
            orphan_count = await budget_service.detect_orphan_app_budgets(session)
            logger.info("admin_api.startup.orphan_app_budget_scan_complete", count=orphan_count)
    except Exception as exc:
        logger.warning("admin_api.startup.orphan_app_budget_scan_failed", error=str(exc))

    logger.info("app.started")

    yield

    # ── Cleanup ──
    if getattr(app.state, "oidc_http", None) is not None:
        await app.state.oidc_http.aclose()
    await redis.aclose()
    # Audit logger — graceful drain (max 10s)
    await audit_logger.shutdown(timeout=10.0)
    logger.info("audit_logger.stopped", queue_size=audit_logger.queue_size, dropped=audit_logger.dropped_count)
    await engine.dispose()
    logger.info("app.shutdown")



# HTTP 상태 → 프로젝트 에러 `type`. 위/아래 AppError 계열 핸들러들이 쓰는 어휘와 같은
# 값을 쓴다 — 같은 403 이 ForbiddenError 에서 왔든 raise HTTPException(403) 에서 왔든
# 클라이언트가 보는 type 은 "forbidden" 하나여야 한다.
_HTTP_STATUS_ERROR_TYPES = {
    400: "validation_error",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "validation_error",
    429: "rate_limited",
    # 502 는 **상류** 실패다. 없으면 fallback 이 "internal_error" 로 뭉개서, 상류(Bedrock/
    # AgentCore/S3) 장애를 우리 서버 결함처럼 보고하고 클라이언트는 재시도 가능 여부를
    # 구분할 근거를 잃는다. 실제 발생지: routers/chat_agent.py 의 query/S3/upstream 실패.
    502: "bad_gateway",
    503: "service_unavailable",
    504: "gateway_timeout",
}


#: 422 `fields[].input` 을 반사할 때 값을 가리는 키 이름 조각(소문자 부분일치).
#: 완전일치가 아니라 부분일치로 본다 — `x_api_key`, `X-Api-Key`, `client_secret`,
#: `refresh_token` 처럼 접두/접미가 붙는 형태가 실제로 들어온다.
_SENSITIVE_KEY_HINTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "credential",
    "authorization",
    "private_key",
    "session",
    "cookie",
    "signature",
)


def _redact_validation_inputs(fields: object) -> object:
    """422 `fields[].input` 에서 민감해 보이는 값을 가린다.

    ⚠️ 왜 필요해졌나: 요청 스키마에 ``extra="forbid"`` 를 넣으면 **선언되지 않은 임의의 키**가
    이 핸들러로 흘러들어온다(``extra_forbidden``). pydantic 의 ``errors()`` 는 그 키의
    **값**을 ``input`` 에 담고, 우리는 그걸 응답에 그대로 실었다. 실측: ``x_api_key`` 를
    보내면 422 본문에 ``"input": "sk-SECRET-abc123"`` 이 그대로 반사됐다.
    forbid 를 넣기 전에는 그 키가 조용히 버려져 반사될 일이 없었으니, 이 노출면은
    forbid 도입이 만든 것이다 — 같은 변경 안에서 닫는다.

    가리는 대상은 ``input`` 값뿐이다. ``loc``(어느 키가 문제인지)과 ``msg``(왜 거부됐는지)는
    남긴다 — 그게 없으면 정규화의 목적(사람이 원인을 아는 것) 자체가 사라진다.
    중첩된 dict/list 도 재귀로 훑는다(본문이 객체면 ``input`` 이 dict 일 수 있다).
    """

    def _is_sensitive(name: object) -> bool:
        if not isinstance(name, str):
            return False
        # ⚠️ 구분자를 `_` 로 정규화한다. 안 하면 헤더식 이름 `X-Api-Key` 가 `api_key` 힌트에
        #    걸리지 않는다(실측으로 새어 나갔다). camelCase `apiKey` 는 소문자화만으로
        #    `apikey` 힌트에 걸린다.
        lowered = name.lower().replace("-", "_").replace(" ", "_")
        return any(hint in lowered for hint in _SENSITIVE_KEY_HINTS)

    def _scrub_value(value: object) -> object:
        # 값 자체가 컨테이너면 내부 키 이름을 보고 개별 판단한다.
        if isinstance(value, dict):
            return {
                k: ("[REDACTED]" if _is_sensitive(k) else _scrub_value(v))
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [_scrub_value(v) for v in value]
        return value

    def _walk(node: object) -> object:
        if isinstance(node, list):
            return [_walk(n) for n in node]
        if not isinstance(node, dict):
            return node
        out = dict(node)
        if "input" in out:
            loc = out.get("loc") or ()
            leaf = loc[-1] if isinstance(loc, (list, tuple)) and loc else None
            if _is_sensitive(leaf):
                out["input"] = "[REDACTED]"
            else:
                out["input"] = _scrub_value(out["input"])
        return out

    return _walk(fields)


def _detail_to_message(detail: object) -> str:
    """str 이 아닌 detail(dict/list)을 사람이 읽는 한 줄로 접는다.

    ⚠️ 그냥 흘려보내면 admin-ui 토스트에 "[object Object]" 만 뜬다 — 422 배열에서 이미
    겪은 그 결함이다(admin-ui/src/lib/utils/errorDetail.ts 의 방어 로직 참고).
    """
    if isinstance(detail, str):
        return detail
    if isinstance(detail, dict):
        for key in ("message", "msg", "detail"):
            value = detail.get(key)
            if isinstance(value, str) and value:
                return value
    try:
        return json.dumps(jsonable_encoder(detail), ensure_ascii=False)
    except (TypeError, ValueError):
        return str(detail)


def _http_exception_envelope(exc: StarletteHTTPException) -> dict:
    """HTTPException 을 프로젝트 에러 봉투 `{"error": {...}}` 로 바꾼다."""
    detail = exc.detail

    # ⚠️ 이미 프로젝트 봉투인 detail 은 **다시 감싸지 않는다**. routers/auth_oidc.py 는
    #    detail 에 {"error": {...}} 를 통째로 넣는데, FastAPI 기본 핸들러가 그걸 또
    #    detail 로 감싸서 {"detail": {"error": {...}}} 이중 봉투가 나갔다(실측).
    #    여기서 통과시키면 그 이중 포장이 사라지고, oidc_disabled/idp_unavailable 처럼
    #    상태코드로는 표현 못 하는 의미 있는 type 도 그대로 살아남는다.
    if isinstance(detail, dict) and isinstance(detail.get("error"), dict):
        return jsonable_encoder(detail)

    status = exc.status_code
    err_type = _HTTP_STATUS_ERROR_TYPES.get(status)
    if err_type is None:
        # 매핑에 없는 상태코드도 봉투는 유지한다(형상이 갈라지면 안 된다).
        err_type = "internal_error" if status >= 500 else "http_error"
        code = f"HTTP_{status}"
    else:
        code = err_type.upper()

    error: dict = {
        "type": err_type,
        "code": code,
        "message": _detail_to_message(detail),
    }
    # str 이 아니었던 원본(dict/list)은 기계 소비자를 위해 보존한다 — 422 핸들러가
    # 원본 배열을 error.fields 로 남기는 것과 같은 이유.
    if not isinstance(detail, str):
        error["detail"] = jsonable_encoder(detail)
    return {"error": error}


def register_exception_handlers(app: FastAPI) -> None:
    """모든 전역 예외 핸들러를 등록한다.

    ⚠️ 이 함수를 통해서만 등록할 것. 예전엔 tests/integration/conftest.py 의
    _build_test_app() 이 핸들러를 **손으로 복제**해서, main.py 에만 추가된
    핸들러(422 정규화·최후의 그물)를 통합테스트가 전혀 검증하지 못했다.
    서비스 간 계약이 테스트 사이 틈으로 빠지는 것과 같은 부류의 결함이다."""
    @app.exception_handler(NotFoundError)
    async def not_found_handler(request: Request, exc: NotFoundError):
        return JSONResponse(
            status_code=404,
            content={"error": {"type": "not_found", "message": exc.message, "code": exc.code}},
        )

    @app.exception_handler(ConflictError)
    async def conflict_handler(request: Request, exc: ConflictError):
        return JSONResponse(
            status_code=409,
            content={"error": {"type": "conflict", "message": exc.message, "code": exc.code}},
        )

    @app.exception_handler(ForbiddenError)
    async def forbidden_handler(request: Request, exc: ForbiddenError):
        return JSONResponse(
            status_code=403,
            content={"error": {"type": "forbidden", "message": exc.message, "code": exc.code}},
        )

    @app.exception_handler(ValidationError)
    async def validation_handler(request: Request, exc: ValidationError):
        return JSONResponse(
            status_code=400,
            content={"error": {"type": "validation_error", "message": exc.message, "code": exc.code}},
        )

    @app.exception_handler(BudgetExceededError)
    async def budget_handler(request: Request, exc: BudgetExceededError):
        return JSONResponse(
            status_code=429,
            content={"error": {"type": "budget_exceeded", "message": exc.message, "code": exc.code}},
        )

    @app.exception_handler(STSVerificationError)
    async def sts_handler(request: Request, exc: STSVerificationError):
        return JSONResponse(
            status_code=401,
            content={"error": {"type": "sts_verification_error", "message": exc.message, "code": exc.code}},
        )

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError):
        return JSONResponse(
            status_code=500,
            content={"error": {"type": "internal_error", "message": exc.message, "code": exc.code}},
        )

    # DB 유니크 제약 위반 → 409. 마이그레이션 0034 가 넣은 부분 유니크 인덱스
    # (활성 단가 1개/사용자당 ACTIVE VK 1개/lower(alias) 유일)는 **동시 요청**에서
    # 정상적으로 발동할 수 있다 — 예컨대 두 관리자가 같은 모델 단가를 동시에 바꾸면
    # 한쪽이 진다. 그건 서버 결함이 아니라 경합이므로 409 로 알려주고 재시도하게 해야
    # 한다. 핸들러가 없으면 최후의 그물이 잡아 **500** 이 되고, admin-ui 는 "서버 오류"
    # 라고만 띄운다(실측: admin-api 전체에 IntegrityError 처리가 하나도 없었다).
    #
    # ⚠️ 무조건 409 로 바꾸면 안 된다. IntegrityError 는 FK 위반·NOT NULL 위반 등
    #    **코드 버그**로도 발생하고, 그걸 409 로 감추면 500 으로 드러나야 할 결함이
    #    "정상적인 경합" 처럼 보인다(로그·알람에서도 사라진다). 그래서 제약 이름을
    #    확인해 **알려진 유니크 제약만** 409 로 매핑하고, 나머지는 다시 던져
    #    최후의 그물이 500 + logger.exception 으로 처리하게 둔다.
    _CONFLICT_CONSTRAINTS = frozenset(
        {
            "idx_model_pricings_active_unique",
            "idx_virtual_keys_user_active_unique",
            "idx_model_aliases_lower_unique",
            "idx_productivity_events_idempotency",
        }
    )

    @app.exception_handler(IntegrityError)
    async def integrity_error_handler(request: Request, exc: IntegrityError):
        # asyncpg 의 UniqueViolationError 는 constraint_name 을 들고 있다. SQLAlchemy 가
        # 감싸므로 orig 를 본다. 드라이버가 이름을 안 주면 문자열에서 찾는다(psycopg 폴백).
        orig = getattr(exc, "orig", None)
        constraint = getattr(orig, "constraint_name", None)
        if not constraint:
            text = str(orig or exc)
            constraint = next((c for c in _CONFLICT_CONSTRAINTS if c in text), None)

        if constraint not in _CONFLICT_CONSTRAINTS:
            # 알 수 없는 무결성 위반 = 버그일 가능성. 감추지 않는다.
            raise exc

        logger.info(
            "integrity_conflict",
            path=request.url.path,
            method=request.method,
            constraint=constraint,
        )
        return JSONResponse(
            status_code=409,
            content={
                "error": {
                    "type": "conflict",
                    "code": "CONCURRENT_MODIFICATION",
                    # 제약 이름은 내부 구조라 본문에 넣지 않는다.
                    "message": "The resource was modified concurrently. Please retry.",
                }
            },
        )

    # 요청 검증 실패(422). FastAPI 기본 응답은 {"detail": [{loc, msg, type}, ...]} 인
    # **배열**인데, admin-ui 의 api-client 는 detail 을 문자열로 보고 그대로 메시지에
    # 넣는다 → 토스트에 "[object Object]" 만 떴다(사용자가 어느 필드가 틀렸는지 알 수
    # 없음). 다른 모든 에러와 같은 {"error": {...}} 봉투로 정규화하고, 사람이 읽을 수
    # 있는 'field: reason' 문자열을 만들어 준다. 원본 배열은 error.fields 로 보존.
    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(request: Request, exc: RequestValidationError):
        errors = exc.errors()
        parts = []
        for err in errors:
            # loc = ('query', 'group_by') 처럼 (출처, 필드…) — 출처는 버리고 경로만.
            loc = ".".join(str(p) for p in err.get("loc", ())[1:]) or "request"
            parts.append(f"{loc}: {err.get('msg', 'invalid value')}")
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "type": "validation_error",
                    "code": "REQUEST_VALIDATION_ERROR",
                    "message": "; ".join(parts) or "Request validation failed",
                    # jsonable_encoder 필수 — errors() 안의 ValueError/bytes 는
                    # json.dumps 로 직렬화되지 않아 핸들러 자체가 500 이 된다.
                    "fields": _redact_validation_inputs(jsonable_encoder(errors)),
                }
            },
        )

    # HTTPException 정규화. 코드베이스에는 `raise HTTPException(...)` 이 53곳(AST 집계)
    # 있고, 그 전부가 FastAPI 기본 핸들러를 타서 `{"detail": ...}` 로 나갔다. 위의
    # 핸들러들은 모두 `{"error": {...}}` 봉투를 내므로 **같은 API 표면이 어느 내부 경로가
    # 던졌는지에 따라 서로 호환되지 않는 두 형상**을 반환했다. admin-ui 의 api-client
    # (admin-ui/src/lib/api-client.ts:54) 는 error.code / error.message 를 먼저 읽어서
    # detail 형상에서는 error_code 가 'UNKNOWN_ERROR' 로 뭉개진다. 53곳을 손대는 대신
    # 여기서 한 번만 정규화한다.
    #
    # ⚠️ 등록 대상은 **starlette** 의 HTTPException 이다(fastapi.HTTPException 아님):
    #   * fastapi.HTTPException 은 starlette 것의 서브클래스라 둘 다 잡힌다.
    #   * 라우터가 직접 내는 404(경로 없음)/405(메서드 불일치)는 starlette 쪽 예외라,
    #     fastapi.HTTPException 에만 등록하면 FastAPI 기본 핸들러가 그대로 남아
    #     `{"detail": "Not Found"}` 가 계속 새어 나간다(실측).
    #   * RequestValidationError 는 HTTPException 계열이 **아니므로**(fastapi 의
    #     ValidationException 계열) 위 422 핸들러가 계속 담당한다. Starlette 의 조회는
    #     예외 클래스 MRO 기준이라 등록 **순서**와도 무관하다.
    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        # ⚠️ raise 쪽이 붙인 헤더를 잃으면 안 된다 — 401 의 WWW-Authenticate 가 사라지면
        #    인증 계약이 깨지고, Retry-After 류도 같이 날아간다.
        headers = dict(getattr(exc, "headers", None) or {})
        # 성공 경로(add_request_id 미들웨어)와 같은 상관관계 헤더를 보장한다. 미들웨어가
        # 어차피 덮어쓰지만, 핸들러만 등록하고 미들웨어는 없는 앱(tests/integration 의
        # 테스트 앱)에서도 계약이 성립해야 한다.
        request_id = getattr(request.state, "request_id", None) or request.headers.get(
            "x-request-id"
        )
        if request_id:
            headers.setdefault("x-request-id", request_id)

        if not is_body_allowed_for_status_code(exc.status_code):
            return Response(status_code=exc.status_code, headers=headers)

        return JSONResponse(
            status_code=exc.status_code,
            content=_http_exception_envelope(exc),
            headers=headers,
        )

    # ⚠️ 최후의 그물. 이게 없으면 처리 못 한 예외는 Starlette 기본 응답인
    #    `Internal Server Error`(**text/plain**)으로 나가서, JSON 을 기대하는 모든
    #    클라이언트가 파싱에 실패하고 error_code 도 request_id 도 잃는다.
    #    본문에는 예외 문자열을 넣지 않는다(내부 구조/자격증명 노출 방지) — 상관관계는
    #    x-request-id 로만.
    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        request_id = getattr(request.state, "request_id", None) or request.headers.get(
            "x-request-id"
        )
        logger.exception(
            "unhandled_exception",
            path=request.url.path,
            method=request.method,
            request_id=request_id,
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "type": "internal_error",
                    "code": "INTERNAL_ERROR",
                    "message": "Internal server error",
                    "request_id": request_id,
                }
            },
            # ⚠️ 이 응답에는 add_request_id 미들웨어가 헤더를 달아주지 못한다. Exception
            #    핸들러는 Starlette 의 ServerErrorMiddleware(미들웨어 스택 **바깥**)가
            #    호출하므로 call_next 가 정상 반환하지 않는다 → 500 만 x-request-id
            #    헤더가 빠졌다(실측: 403/404 는 있고 500 은 없음). 본문 request_id 만으로는
            #    본문을 파싱하지 않는 프록시/ALB 로그·클라이언트가 상관관계를 잃는다.
            headers={"x-request-id": request_id} if request_id else None,
        )



def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="LLM Gateway — Admin API",
        version="0.1.0",
        lifespan=lifespan,
    )

    # ── CORS (Admin UI) ──
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if settings.APP_ENV == "development" else [],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Request ID middleware ──
    @app.middleware("http")
    async def add_request_id(request: Request, call_next):
        request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
        # 예외 핸들러도 같은 id 를 쓸 수 있게 state 에 남긴다 — 헤더만 보면 클라이언트가
        # 안 보낸 경우(여기서 생성한 uuid)를 500 응답에 실어 줄 수 없다.
        request.state.request_id = request_id
        structlog.contextvars.bind_contextvars(request_id=request_id)
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        structlog.contextvars.unbind_contextvars("request_id")
        return response

    # ── Global exception handlers (register_exception_handlers 에 정의) ──
    register_exception_handlers(app)

    # ── Register routers ──
    from app.routers import analytics, apps, aws_accounts, budgets, cli, dashboard, internal, keys, me, models, monitoring, my, productivity, rate_limits, routing, service_tokens, settings as settings_router, users

    app.include_router(keys.router)
    app.include_router(budgets.router)
    app.include_router(models.router)
    app.include_router(rate_limits.router)
    app.include_router(routing.router)
    app.include_router(aws_accounts.router)  # /admin/aws-accounts — 계정 레지스트리 (0044)
    app.include_router(apps.router)
    app.include_router(settings_router.router)
    app.include_router(users.router)
    app.include_router(analytics.router)
    app.include_router(dashboard.router)
    app.include_router(my.router)
    app.include_router(me.router)  # /admin/me — admin-ui 가 화면 권한을 정할 때 묻는 신원
    app.include_router(monitoring.router)
    app.include_router(productivity.router)
    app.include_router(cli.router)
    app.include_router(internal.router)
    app.include_router(service_tokens.router)
    from app.routers import auth_oidc
    app.include_router(auth_oidc.router)

    from app.routers import chat_agent  # admin-chat-agent BI assistant (Phase 2)
    app.include_router(chat_agent.router)

    # Bedrock invocation log ↔ usage_logs 대조(runtime plane 본문 감사). 설정이 비어 있으면
    # 엔드포인트는 존재하되 503 으로 "미설정" 을 알린다 — 라우터를 조건부로 빼면 UI 가
    # 404 와 미설정을 구분할 수 없다.
    from app.routers import audit_reconcile
    app.include_router(audit_reconcile.router)

    # ── 커밋 시점 교정 (반드시 모든 include_router 뒤) ──
    # 의존성 종료 블록의 커밋은 응답이 나간 **뒤에** 실행되므로, 커밋이 실패해도
    # 클라이언트는 200 + 성공 본문을 받는다(실측). 모든 라우트를 CommittingRoute 로
    # 승격해 커밋을 응답 전송 전으로 옮긴다. 여기서 호출하지 않으면 그 회귀가 조용히
    # 돌아오므로 tests/regression 에서 라우트 클래스를 전수 검사한다.
    upgraded = install_commit_before_response(app)
    logger.info("app.commit_before_response_installed", routes=upgraded)

    return app


app = create_app()
