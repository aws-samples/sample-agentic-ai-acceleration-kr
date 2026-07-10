# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import hashlib
import json
from typing import Protocol

import jwt
import structlog
from sqlalchemy import select, text as _sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import JwtPublicKey, User
from app.models.model import TeamAllowedModel, UserAllowedModel
from app.schemas.domain import AuthContext, AuthType, Role

logger = structlog.get_logger(__name__)

VK_CACHE_TTL = 300  # 5분
JWT_KEY_CACHE_TTL = 3600  # 1시간


def _extract_bearer_token(authorization: str) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise ValueError("Missing or invalid Authorization header")
    return authorization[7:]


def _is_jwt_token(token: str) -> bool:
    """JWT 형태 판별: '.' 구분 3-part."""
    parts = token.split(".")
    return len(parts) == 3


class AuthStrategy(Protocol):
    async def authenticate(
        self, authorization: str, redis, db: AsyncSession | None
    ) -> AuthContext: ...


class VKAuthStrategy:
    """Virtual Key 인증 (Bedrock 경로 /model/*)."""

    async def authenticate(
        self, authorization: str, redis, db: AsyncSession | None
    ) -> AuthContext:
        token = _extract_bearer_token(authorization)
        key_hash = hashlib.sha256(token.encode()).hexdigest()

        # 1) AuthContext 캐시 조회
        if redis is not None:
            cached = await redis.get(f"key:cache:vk:{key_hash}")
            if cached:
                data = json.loads(cached)
                # user.is_active 재확인 — 캐시 TTL(300s) 안에 계정 비활성화된 경우 즉시 차단
                if db is not None:
                    from sqlalchemy import select as sa_select
                    result = await db.execute(
                        sa_select(User.is_active).where(User.id == data["user_id"])
                    )
                    is_active = result.scalar_one_or_none()
                    if is_active is False:
                        await redis.delete(f"key:cache:vk:{key_hash}", f"key:vk:{key_hash}")
                        raise PermissionError("User account is deactivated")
                return AuthContext(**data)

        # 2) VK→user_id 매핑 조회 (Admin API가 발급 시 저장)
        user_id = None
        if redis is not None:
            raw = await redis.get(f"key:vk:{key_hash}")
            if raw:
                user_id = raw if isinstance(raw, str) else raw.decode()

        if user_id is None:
            raise PermissionError("Invalid or inactive virtual key")

        # 3) 사용자 정보 조회
        if db is None:
            raise PermissionError("DB unavailable, cache miss")

        user_result = await db.execute(select(User).where(User.id == user_id))
        user = user_result.scalar_one_or_none()
        if user is None or not user.is_active:
            raise PermissionError("User not found or inactive")

        # allowed_models 스냅샷 — 우선순위 user > team > none (260626_comm_customer 항목2).
        #   user_allowed_models 행 존재 → 그 화이트리스트만 (팀 무시).
        #   user 행 0개 → team_allowed_models 로 폴백. 둘 다 없음 → None(전체 허용).
        # ★ fail-closed: 국가핵심기술 제한이므로, user override 조회가 DB 오류로 실패하면
        #   제한을 우회시키지 않고 인증을 막는다(allowed_clients 의 fail-open 과 다름).
        try:
            uam_result = await db.execute(
                select(UserAllowedModel.model_alias).where(
                    UserAllowedModel.user_id == user.id
                )
            )
            user_aliases = list(uam_result.scalars().all())
        except Exception:
            logger.warning(
                "user_allowed_models_lookup_failed_fail_closed",
                user_id=str(user.id),
                exc_info=True,
            )
            raise PermissionError(
                "model access policy unavailable (fail-closed)"
            )

        allowed_models: list[str] | None
        if user_aliases:
            allowed_models = user_aliases
        elif user.team_id:
            tam_result = await db.execute(
                select(TeamAllowedModel.model_alias).where(
                    TeamAllowedModel.team_id == user.team_id
                )
            )
            team_aliases = list(tam_result.scalars().all())
            allowed_models = team_aliases if team_aliases else None
        else:
            allowed_models = None

        # 사용자별 allowed_clients (행 0개 = None = both 허용).
        # text 는 모듈 상단에서 import — except 가 ImportError 까지 삼켜 enforcement 를
        # 조용히 끄지 않도록(실제 DB I/O 실패만 fail-open) 범위를 좁힌다.
        allowed_clients: list[str] | None = None
        try:
            rows = (await db.execute(
                _sql_text("SELECT client FROM auth.user_allowed_clients WHERE user_id = :uid"),
                {"uid": str(user.id)},
            )).scalars().all()
            allowed_clients = list(rows) if rows else None
        except Exception:
            allowed_clients = None  # DB 조회 실패 시 막지 않음(allow-all) — fail-open(soft gating)

        auth_context = AuthContext(
            user_id=str(user.id),
            team_id=str(user.team_id) if user.team_id else "",
            dept_id="",
            roles=[Role.USER],
            auth_type=AuthType.VIRTUAL_KEY,
            key_id=None,
            allowed_models=allowed_models,
            allowed_clients=allowed_clients,
            sso_subject=user.sso_subject,
        )

        # 4) AuthContext 캐시 저장
        if redis is not None:
            await redis.setex(
                f"key:cache:vk:{key_hash}",
                VK_CACHE_TTL,
                auth_context.model_dump_json(),
            )

        return auth_context


class JWTAuthStrategy:
    """JWT 인증 (OpenAI 경로 /v1/*)."""

    async def authenticate(
        self, authorization: str, redis, db: AsyncSession | None
    ) -> AuthContext:
        token = _extract_bearer_token(authorization)

        # kid 추출 (헤더 디코딩, 검증 없이)
        try:
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
        except Exception as e:
            raise PermissionError(f"Invalid JWT header: {e}")

        if not kid:
            raise PermissionError("JWT missing kid claim")

        # public key 조회
        public_key_pem: str | None = None

        if redis is not None:
            cached_key = await redis.get(f"key:cache:jwt:{kid}")
            if cached_key:
                public_key_pem = cached_key.decode()

        if public_key_pem is None:
            if db is None:
                raise PermissionError("DB unavailable, JWT key cache miss")

            result = await db.execute(
                select(JwtPublicKey)
                .where(JwtPublicKey.kid == kid)
                .where(JwtPublicKey.status == "active")
            )
            jwt_key = result.scalar_one_or_none()
            if jwt_key is None:
                raise PermissionError(f"Unknown JWT kid: {kid}")

            public_key_pem = jwt_key.public_key_pem
            algorithm = jwt_key.algorithm

            if redis is not None:
                await redis.setex(f"key:cache:jwt:{kid}", JWT_KEY_CACHE_TTL, public_key_pem)
        else:
            algorithm = "RS256"

        # JWT 서명 검증 및 클레임 추출
        try:
            claims = jwt.decode(
                token,
                public_key_pem,
                algorithms=[algorithm],
                options={"require": ["user_id", "team_id", "dept_id", "roles", "exp"]},
            )
        except jwt.ExpiredSignatureError:
            raise PermissionError("JWT expired")
        except jwt.InvalidTokenError as e:
            raise PermissionError(f"JWT invalid: {e}")

        return AuthContext(
            user_id=claims["user_id"],
            team_id=claims["team_id"],
            dept_id=claims["dept_id"],
            roles=[Role(r) for r in claims.get("roles", [])],
            auth_type=AuthType.JWT,
            key_id=None,
            allowed_models=None,  # JWT는 Key Scope 없음
            # allowed_clients 미설정(None=both) — JWT 경로는 client 정책을 강제하지 않음.
            # 의도적: Claude Code/Cowork 추론 트래픽은 VK(api-key-helper/inferenceGatewayApiKey)
            # 로 인증하므로 VK 경로의 allowed_clients 로 커버됨. JWT 는 admin/console 경로.
            # JWT 사용자까지 강제하려면 claims["user_id"] 로 동일 DB 조회를 추가하면 됨(향후).
        )


class DualAuthStrategy:
    """/v1/usage/me 전용 — VK 또는 JWT 자동 판별."""

    def __init__(self) -> None:
        self._vk = VKAuthStrategy()
        self._jwt = JWTAuthStrategy()

    async def authenticate(
        self, authorization: str, redis, db: AsyncSession | None
    ) -> AuthContext:
        token = _extract_bearer_token(authorization)
        if _is_jwt_token(token):
            return await self._jwt.authenticate(authorization, redis, db)
        return await self._vk.authenticate(authorization, redis, db)


# 전역 전략 인스턴스
_VK_STRATEGY = VKAuthStrategy()
_JWT_STRATEGY = JWTAuthStrategy()
_DUAL_STRATEGY = DualAuthStrategy()


def resolve_auth_strategy(path: str) -> AuthStrategy | None:
    """경로 기반 인증 전략 반환.

    NOTE (FR-1.4 A안, 2026-04-17): /v1/chat/completions, /v1/completions는
    본래 JWT 전용(FR-2.1b)이나 Week 3까지 사내 JWT 인프라가 준비되지 않아
    VK로 mock-vllm E2E 검증 가능하도록 임시로 DUAL에 편입. Week 3 JWT 붙을 때
    JWT 전용 또는 JWT 우선 DUAL로 전환할 것. 자세한 인계는
    `requirements-document/milestone.md` Week 3 섹션 참조.
    """
    if path in (
        "/v1/messages",
        "/v1/messages/count_tokens",
        "/v1/usage/me",
        "/v1/models",
        "/v1/chat/completions",
        "/v1/completions",
        "/v1/responses",  # Codex (OpenAI Responses API) — same VK/DUAL inference path
    ):
        return _DUAL_STRATEGY
    # /v1/models/{id} 같은 단일 모델 상세 엔드포인트 — Claude Code 가 세션 시작 시 호출.
    # 정확 매칭에 없으면 아래 `/v1/` fallthrough 로 JWT 로 라우팅돼 VK 인증 실패.
    if path.startswith("/v1/models/"):
        return _DUAL_STRATEGY
    if path.startswith("/model/"):
        return _VK_STRATEGY
    if path.startswith("/v1/"):
        return _JWT_STRATEGY
    return None
