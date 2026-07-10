# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import json
from decimal import Decimal
from typing import Optional

import structlog
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.model import ModelAlias, ModelPricing
from app.schemas.domain import (
    ApiFormat,
    AuthContext,
    ModelConfigSchema,
    ModelPricingSchema,
    ModelStatus,
    ProviderType,
)

logger = structlog.get_logger(__name__)

MODEL_CACHE_TTL = 300  # 5분
MODEL_LIST_CACHE_TTL = 300


def check_client_scope(allowed_clients: list[str] | None, client: str | None) -> None:
    """Raise PermissionError if the identified client is not allowed for this user.

    allowed_clients None/[] = 전체 허용(both). 값이 있으면 화이트리스트 — client 가
    그 안에 없으면 거부. client 'other'/None 은 화이트리스트가 있으면 항상 거부.
    """
    if not allowed_clients:
        return
    if client not in allowed_clients:
        raise PermissionError(f"Client '{client}' not allowed for this key")


def _orm_to_schema(alias_row: ModelAlias, pricing_row: Optional[ModelPricing]) -> ModelConfigSchema:
    if pricing_row:
        p = ModelPricingSchema(
            input_per_1k=pricing_row.input_price_per_1k_tokens,
            output_per_1k=pricing_row.output_price_per_1k_tokens,
            cache_write_per_1k=pricing_row.cache_creation_5m_price_per_1k_tokens,
            cache_write_1h_per_1k=pricing_row.cache_creation_1h_price_per_1k_tokens,
            cache_read_per_1k=pricing_row.cache_read_price_per_1k_tokens,
        )
    else:
        p = ModelPricingSchema(input_per_1k=Decimal("0"), output_per_1k=Decimal("0"))

    return ModelConfigSchema(
        provider_model_id=alias_row.provider_model_id,
        alias=alias_row.alias,
        provider=ProviderType(alias_row.provider),
        api_format=ApiFormat(alias_row.api_format),
        endpoint=alias_row.endpoint_url or "",
        pricing=p,
        status=ModelStatus(alias_row.status),
        created_at=alias_row.created_at,
        description=alias_row.description,
    )


def _parse_cached_model(cached: str, model_ref: str) -> Optional[ModelConfigSchema]:
    """Parse a cached model:{alias} payload, returning None on any failure.

    P0-④ defense-in-depth: a malformed/legacy cache entry (e.g. an old flat
    shape with no nested `pricing`) must NOT raise ValidationError up the hot
    path (which surfaced as a permanent 500). Returning None makes the caller
    treat it as a cache miss and rebuild from the DB with the correct shape+TTL.
    """
    try:
        return ModelConfigSchema(**json.loads(cached))
    except Exception:
        logger.warning("model_cache_parse_failed_treating_as_miss", model_ref=model_ref)
        return None


async def _fetch_latest_pricing(db: AsyncSession, alias: str) -> Optional[ModelPricing]:
    """가장 최근 유효 pricing 1건 (effective_from <= now, effective_until is null or > now)."""
    result = await db.execute(
        select(ModelPricing)
        .where(
            and_(
                ModelPricing.model_alias == alias,
                ModelPricing.effective_from <= func.now(),
                or_(
                    ModelPricing.effective_until.is_(None),
                    ModelPricing.effective_until > func.now(),
                ),
            )
        )
        .order_by(ModelPricing.effective_from.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


class RouterService:
    """모델 alias 조회 및 Key Scope 검사."""

    async def resolve_bedrock_model(
        self,
        redis,
        db: Optional[AsyncSession],
        model_ref: str,
    ) -> ModelConfigSchema:
        """Bedrock 경로: alias OR provider_model_id 둘 다 수용. 미등록은 LookupError.

        Raises:
            LookupError: 미등록, INACTIVE, 또는 provider mismatch.
        """
        cache_key = f"model:{model_ref}"

        if redis is not None:
            cached = await redis.get(cache_key)
            if cached:
                schema = _parse_cached_model(cached, model_ref)
                if schema is not None:
                    if schema.status == ModelStatus.INACTIVE:
                        raise LookupError(f"Model '{schema.alias or model_ref}' is inactive")
                    if schema.provider != ProviderType.BEDROCK:
                        raise LookupError(f"Model alias '{model_ref}' not found")
                    return schema
                # parse failed → fall through to DB rebuild (self-heal poison entry)

        if db is None:
            raise LookupError(f"Model alias '{model_ref}' not found (DB unavailable)")

        # alias 정확 매칭 우선, 없으면 provider_model_id fallback (동일 provider_model_id가
        # 여러 alias에 매핑될 수 있으므로 first() 사용).
        result = await db.execute(
            select(ModelAlias).where(
                and_(
                    ModelAlias.provider == "BEDROCK",
                    ModelAlias.alias == model_ref,
                )
            )
        )
        alias_row = result.scalar_one_or_none()

        if alias_row is None:
            result = await db.execute(
                select(ModelAlias).where(
                    and_(
                        ModelAlias.provider == "BEDROCK",
                        ModelAlias.provider_model_id == model_ref,
                    )
                ).limit(1)
            )
            alias_row = result.scalar_one_or_none()
        if alias_row is None:
            raise LookupError(f"Model alias '{model_ref}' not found")

        if alias_row.status != "ACTIVE":
            raise LookupError(f"Model '{alias_row.alias}' is inactive")

        pricing_row = await _fetch_latest_pricing(db, alias_row.alias)
        schema = _orm_to_schema(alias_row, pricing_row)

        if redis is not None:
            payload = schema.model_dump_json()
            await redis.setex(f"model:{schema.alias}", MODEL_CACHE_TTL, payload)
            await redis.setex(f"model:{schema.provider_model_id}", MODEL_CACHE_TTL, payload)

        return schema

    async def resolve_mantle_model(
        self,
        redis,
        db: Optional[AsyncSession],
        model_ref: str,
        expected_provider: ProviderType = ProviderType.BEDROCK_MANTLE,
    ) -> ModelConfigSchema:
        """Bedrock Mantle 경로: alias OR provider_model_id 둘 다 수용. 미등록은 LookupError.

        expected_provider 로 Mantle 계열 provider 를 구분한다:
          - BEDROCK_MANTLE         → Cowork (Anthropic Messages, Tokyo Opus)
          - BEDROCK_MANTLE_OPENAI  → Codex  (OpenAI Responses, Ohio GPT-5.5)
        provider 가 다르면 LookupError (다른 계열 alias 로 라우팅되는 것 방지).

        Raises:
            LookupError: 미등록, INACTIVE, 또는 provider mismatch.
        """
        provider_value = expected_provider.value
        if redis is not None:
            cached = await redis.get(f"model:{model_ref}")
            if cached:
                schema = _parse_cached_model(cached, model_ref)
                if schema is not None:
                    if schema.status == ModelStatus.INACTIVE:
                        raise LookupError(f"Model '{schema.alias or model_ref}' is inactive")
                    if schema.provider != expected_provider:
                        raise LookupError(f"Model alias '{model_ref}' not found")
                    return schema
                # parse failed → fall through to DB rebuild (self-heal poison entry)

        if db is None:
            raise LookupError(f"Model alias '{model_ref}' not found (DB unavailable)")

        # alias 정확 매칭 우선, 없으면 provider_model_id fallback (동일 provider_model_id가
        # 여러 alias에 매핑될 수 있으므로 first() 사용).
        result = await db.execute(
            select(ModelAlias).where(
                and_(
                    ModelAlias.provider == provider_value,
                    ModelAlias.alias == model_ref,
                )
            )
        )
        alias_row = result.scalar_one_or_none()

        if alias_row is None:
            result = await db.execute(
                select(ModelAlias).where(
                    and_(
                        ModelAlias.provider == provider_value,
                        ModelAlias.provider_model_id == model_ref,
                    )
                ).limit(1)
            )
            alias_row = result.scalar_one_or_none()
        if alias_row is None:
            raise LookupError(f"Model alias '{model_ref}' not found")

        if alias_row.status != "ACTIVE":
            raise LookupError(f"Model '{alias_row.alias}' is inactive")

        pricing_row = await _fetch_latest_pricing(db, alias_row.alias)
        schema = _orm_to_schema(alias_row, pricing_row)

        if redis is not None:
            payload = schema.model_dump_json()
            await redis.setex(f"model:{schema.alias}", MODEL_CACHE_TTL, payload)
            await redis.setex(f"model:{schema.provider_model_id}", MODEL_CACHE_TTL, payload)

        return schema

    async def resolve_codex_model(
        self,
        redis,
        db: Optional[AsyncSession],
        model_ref: str,
    ) -> ModelConfigSchema:
        """Codex (Bedrock Mantle OpenAI Responses) 모델 해석. resolve_mantle_model 의
        BEDROCK_MANTLE_OPENAI 특화 래퍼 — provider mismatch 시 LookupError."""
        return await self.resolve_mantle_model(
            redis, db, model_ref, expected_provider=ProviderType.BEDROCK_MANTLE_OPENAI
        )

    async def alias_provider(self, redis, db, alias: str):
        """Return the ProviderType of an alias, or None if unknown.

        Does NOT raise on lookup failure — this method is used only as a routing
        hint; the caller decides whether to proceed or fall back to Bedrock.
        Checks Redis model cache first (avoids a DB round-trip on hot paths),
        then falls back to a direct DB query.
        """
        cache_key = f"model:{alias}"
        if redis is not None:
            cached = await redis.get(cache_key)
            if cached:
                try:
                    return ProviderType(json.loads(cached).get("provider"))
                except Exception:
                    pass
        if db is None:
            return None
        from sqlalchemy import select as sa_select
        row = (
            await db.execute(
                sa_select(ModelAlias.provider).where(ModelAlias.alias == alias)
            )
        ).scalar_one_or_none()
        return ProviderType(row) if row else None

    async def resolve_openai_model(
        self,
        redis,
        db: Optional[AsyncSession],
        alias: str,
    ) -> ModelConfigSchema:
        """OPENMODEL alias 조회. 이번 단위에선 minimal 동작만 (다음 단위 vLLM에서 완성).

        Raises:
            LookupError: 미등록 또는 INACTIVE.
        """
        cache_key = f"model:{alias}"

        if redis is not None:
            cached = await redis.get(cache_key)
            if cached:
                schema = _parse_cached_model(cached, alias)
                if schema is not None:
                    if schema.status == ModelStatus.INACTIVE:
                        raise LookupError(f"Model '{alias}' is inactive")
                    if schema.provider != ProviderType.OPENMODEL:
                        raise LookupError(f"Model '{alias}' not found")
                    return schema
                # parse failed → fall through to DB rebuild (self-heal poison entry)

        if db is None:
            raise LookupError(f"Model '{alias}' not found (DB unavailable)")

        result = await db.execute(
            select(ModelAlias).where(
                and_(
                    ModelAlias.provider == "OPENMODEL",
                    ModelAlias.alias == alias,
                )
            )
        )
        alias_row = result.scalar_one_or_none()
        if alias_row is None:
            raise LookupError(f"Model '{alias}' not found")
        if alias_row.status != "ACTIVE":
            raise LookupError(f"Model '{alias}' is inactive")

        pricing_row = await _fetch_latest_pricing(db, alias_row.alias)
        schema = _orm_to_schema(alias_row, pricing_row)

        if redis is not None:
            await redis.setex(cache_key, MODEL_CACHE_TTL, schema.model_dump_json())
        return schema

    def check_key_scope(
        self,
        auth_context: AuthContext,
        model_ref: str | ModelConfigSchema,
    ) -> None:
        """Key Scope 검사: allowed_models(팀 화이트리스트, alias 저장) 기준.

        `TeamAllowedModel`에는 alias만 저장. 호출부가 provider_model_id를
        넘겨도 alias로 매칭되어야 함. 둘 다 수용하기 위해:
        - `ModelConfigSchema` 전달: 내부 alias와 비교
        - `str` 전달: alias OR provider_model_id 로 해석하여 allowed_models의 alias 집합과
          모델 카탈로그로 한 번 더 resolve 없이 단순 멤버십 체크 (호출부가 alias를 안다면 alias로,
          모를 수 있으면 ModelConfigSchema 경로를 쓸 것).
        """
        if not auth_context.allowed_models:
            return

        if isinstance(model_ref, ModelConfigSchema):
            candidates = {model_ref.alias or "", model_ref.provider_model_id}
        else:
            candidates = {model_ref}

        allowed = set(auth_context.allowed_models)
        if allowed.isdisjoint(candidates):
            raise PermissionError(
                f"Model '{model_ref if isinstance(model_ref, str) else model_ref.alias}' "
                "not allowed for this key"
            )

    async def list_active_models(
        self, redis, db: Optional[AsyncSession]
    ) -> list[ModelConfigSchema]:
        """GET /v1/models: 활성 모델 목록 (Redis 캐시 5분)."""
        cache_key = "model:list"

        if redis is not None:
            cached = await redis.get(cache_key)
            if cached:
                return [ModelConfigSchema(**m) for m in json.loads(cached)]

        if db is None:
            return []

        result = await db.execute(select(ModelAlias).where(ModelAlias.status == "ACTIVE"))
        rows = result.scalars().all()
        schemas: list[ModelConfigSchema] = []
        for row in rows:
            pricing_row = await _fetch_latest_pricing(db, row.alias)
            schemas.append(_orm_to_schema(row, pricing_row))

        if redis is not None:
            await redis.setex(
                cache_key,
                MODEL_LIST_CACHE_TTL,
                json.dumps([s.model_dump(mode="json") for s in schemas]),
            )
        return schemas
