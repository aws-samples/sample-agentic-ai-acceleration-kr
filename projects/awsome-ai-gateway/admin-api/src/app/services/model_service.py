# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_logger
from app.core.auth import CurrentUser
from app.core.cache_invalidation import CacheInvalidationManager
from app.core.exceptions import ConflictError, NotFoundError
from app.models.model import ApiFormat, ModelAlias, ModelPricing, ModelStatus, Provider
from app.repositories.model_repository import ModelRepository
from app.schemas.models import (
    ModelCreateRequest,
    ModelPricingResponse,
    ModelResponse,
    ModelUpdateRequest,
    PricingRequest,
    StatusPatchRequest,
)

logger = structlog.get_logger()


class ModelService:
    def __init__(self, cache_mgr: CacheInvalidationManager) -> None:
        self._cache_mgr = cache_mgr

    async def list_models(self, session: AsyncSession) -> list[ModelResponse]:
        repo = ModelRepository(session)
        models = await repo.list_all()
        result: list[ModelResponse] = []
        for m in models:
            pricing = await repo.get_current_pricing(m.alias)
            result.append(self._to_response(m, pricing))
        return result

    async def create_model(
        self,
        session: AsyncSession,
        *,
        data: ModelCreateRequest,
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> ModelResponse:
        repo = ModelRepository(session)

        # BR-MOD-01: Case-insensitive alias uniqueness
        if await repo.alias_exists_ci(data.alias):
            raise ConflictError(f"Model alias already exists: {data.alias}")

        model = ModelAlias(
            alias=data.alias,
            provider=Provider(data.provider.value),
            provider_model_id=data.provider_model_id,
            endpoint_url=data.endpoint_url,
            api_format=ApiFormat(data.api_format.value),
            status=ModelStatus.ACTIVE,
            description=data.description,
            display_name=data.display_name,
            created_by=actor.user_id,
        )
        await repo.create_model(model)

        # Initial pricing
        pricing = ModelPricing(
            id=uuid.uuid4(),
            model_alias=data.alias,
            input_price_per_1k_tokens=data.input_price_per_1k_tokens,
            output_price_per_1k_tokens=data.output_price_per_1k_tokens,
            cache_creation_5m_price_per_1k_tokens=data.cache_creation_5m_price_per_1k_tokens,
            cache_creation_1h_price_per_1k_tokens=data.cache_creation_1h_price_per_1k_tokens,
            cache_read_price_per_1k_tokens=data.cache_read_price_per_1k_tokens,
            effective_from=datetime.now(timezone.utc),
            created_by=actor.user_id,
        )
        await repo.create_pricing(pricing)

        # BR-MOD-04 / P0-④: invalidate-only (do NOT pre-seed model:{alias}).
        # Pre-seeding a flat, TTL-less cache entry here was the cache-poison
        # pattern: DEL both keys and let the gateway populate model:{alias} with
        # the correct nested shape + TTL on first cache-miss (router_service
        # self-heal). Keeps admin writes and gateway reads on one cache contract.
        await self._cache_mgr.invalidate(
            [f"model:{model.alias}", "model:list"], session=session
        )

        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            action="CREATE_MODEL",
            resource_type="ModelAlias",
            resource_id=model.alias,
            changes={"after": {"alias": model.alias, "provider": model.provider.value}},
            ip_address=ip_address,
            request_id=request_id,
        )

        return self._to_response(model, pricing)

    async def update_model(
        self,
        session: AsyncSession,
        *,
        alias: str,
        data: ModelUpdateRequest,
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> ModelResponse:
        repo = ModelRepository(session)
        update_kwargs = {k: v for k, v in data.model_dump().items() if v is not None}
        model = await repo.update_model(alias, **update_kwargs)
        if model is None:
            raise NotFoundError("ModelAlias", alias)

        pricing = await repo.get_current_pricing(alias)

        # BR-MOD-04: Cache invalidation
        await self._cache_mgr.invalidate([f"model:{alias}", "model:list"], session=session)

        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            action="UPDATE_MODEL",
            resource_type="ModelAlias",
            resource_id=alias,
            changes={"after": update_kwargs},
            ip_address=ip_address,
            request_id=request_id,
        )

        return self._to_response(model, pricing)

    async def set_pricing(
        self,
        session: AsyncSession,
        *,
        alias: str,
        data: PricingRequest,
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> ModelResponse:
        repo = ModelRepository(session)
        model = await repo.get_by_alias(alias)
        if model is None:
            raise NotFoundError("ModelAlias", alias)

        # BR-MOD-02: Close current pricing, preserve history
        await repo.close_current_pricing(alias, data.effective_from)

        pricing = ModelPricing(
            id=uuid.uuid4(),
            model_alias=alias,
            input_price_per_1k_tokens=data.input_price_per_1k_tokens,
            output_price_per_1k_tokens=data.output_price_per_1k_tokens,
            cache_creation_5m_price_per_1k_tokens=data.cache_creation_5m_price_per_1k_tokens,
            cache_creation_1h_price_per_1k_tokens=data.cache_creation_1h_price_per_1k_tokens,
            cache_read_price_per_1k_tokens=data.cache_read_price_per_1k_tokens,
            effective_from=data.effective_from,
            created_by=actor.user_id,
        )
        await repo.create_pricing(pricing)

        await self._cache_mgr.invalidate([f"model:{alias}"], session=session)

        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            action="SET_PRICING",
            resource_type="ModelPricing",
            resource_id=alias,
            changes={"after": {
                "input_price": str(data.input_price_per_1k_tokens),
                "output_price": str(data.output_price_per_1k_tokens),
                "cache_creation_price": str(data.cache_creation_5m_price_per_1k_tokens),
                "cache_creation_1h_price": str(data.cache_creation_1h_price_per_1k_tokens),
                "cache_read_price": str(data.cache_read_price_per_1k_tokens),
            }},
            ip_address=ip_address,
            request_id=request_id,
        )

        return self._to_response(model, pricing)

    async def preview_price_sync(
        self,
        session: AsyncSession,
        *,
        pricing_sync_service,
        quantize: Decimal = Decimal("0.000001"),
    ):
        """AWS Price List 단가 vs DB 현재가 diff 미리보기(쓰기 없음, deepdive 가격동기화).

        BEDROCK provider 모델만 대상. AWS 에서 못 찾으면 matched=False 로 표시(스킵 후보).
        """
        from app.schemas.models import (
            PriceSyncDiff,
            PriceSyncPreviewResponse,
        )

        repo = ModelRepository(session)
        models = await repo.list_all()
        fetched = await pricing_sync_service.fetch_bedrock_prices()

        diffs: list[PriceSyncDiff] = []
        matched = 0
        changed = 0
        for m in models:
            if m.provider != Provider.BEDROCK:
                continue  # OpenModel/vLLM 은 AWS 단가 없음
            cur = await repo.get_current_pricing(m.alias)
            cur_resp = self._to_response(m, cur).current_pricing
            np = fetched.prices.get(m.provider_model_id.lower())
            if np is None:
                diffs.append(PriceSyncDiff(
                    alias=m.alias,
                    provider_model_id=m.provider_model_id,
                    matched=False,
                    note="AWS Price List 에서 단가 미발견(모델ID 매칭 실패 또는 미게시)",
                    current=cur_resp,
                ))
                continue
            matched += 1
            p_in = np.input_per_1k.quantize(quantize)
            p_out = np.output_per_1k.quantize(quantize)
            p_5m = np.cache_5m_per_1k.quantize(quantize)
            p_1h = np.cache_1h_per_1k.quantize(quantize)
            p_rd = np.cache_read_per_1k.quantize(quantize)
            is_changed = cur is None or any([
                cur.input_price_per_1k_tokens != p_in,
                cur.output_price_per_1k_tokens != p_out,
                cur.cache_creation_5m_price_per_1k_tokens != p_5m,
                cur.cache_creation_1h_price_per_1k_tokens != p_1h,
                cur.cache_read_price_per_1k_tokens != p_rd,
            ])
            if is_changed:
                changed += 1
            note = "캐시 단가 일부 파생(AWS 미게시 → input 기반 추정)" if np.cache_derived else None
            diffs.append(PriceSyncDiff(
                alias=m.alias,
                provider_model_id=m.provider_model_id,
                matched=True,
                note=note,
                current=cur_resp,
                proposed_input_per_1k=p_in,
                proposed_output_per_1k=p_out,
                proposed_cache_5m_per_1k=p_5m,
                proposed_cache_1h_per_1k=p_1h,
                proposed_cache_read_per_1k=p_rd,
                changed=is_changed,
            ))

        return PriceSyncPreviewResponse(
            region=getattr(pricing_sync_service, "region", "us-east-1"),
            diffs=diffs,
            matched_count=matched,
            changed_count=changed,
        )

    async def apply_price_sync(
        self,
        session: AsyncSession,
        *,
        pricing_sync_service,
        aliases: list[str],
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
        quantize: Decimal = Decimal("0.000001"),
    ):
        """승인된 alias 목록만 AWS 단가로 적용 — 기존 set_pricing 재사용(시계열·감사·캐시).

        자동 전체적용 금지: 호출자가 preview 후 명시 선택한 aliases 만.
        """
        from app.schemas.models import PriceSyncApplyResponse, PricingRequest

        repo = ModelRepository(session)
        fetched = await pricing_sync_service.fetch_bedrock_prices()
        now = datetime.now(timezone.utc)

        applied: list[str] = []
        skipped: list[str] = []
        errors: list[str] = list(fetched.errors)

        for alias in aliases:
            model = await repo.get_by_alias(alias)
            if model is None:
                errors.append(f"{alias}: 모델 없음")
                continue
            if model.provider != Provider.BEDROCK:
                skipped.append(alias)
                continue
            np = fetched.prices.get(model.provider_model_id.lower())
            if np is None:
                skipped.append(alias)  # AWS 단가 미발견 → 적용 안 함
                continue
            req = PricingRequest(
                input_price_per_1k_tokens=np.input_per_1k.quantize(quantize),
                output_price_per_1k_tokens=np.output_per_1k.quantize(quantize),
                cache_creation_5m_price_per_1k_tokens=np.cache_5m_per_1k.quantize(quantize),
                cache_creation_1h_price_per_1k_tokens=np.cache_1h_per_1k.quantize(quantize),
                cache_read_price_per_1k_tokens=np.cache_read_per_1k.quantize(quantize),
                effective_from=now,
            )
            # 기존 set_pricing 재사용 → close_current_pricing + 새 행 + 캐시무효화 + SET_PRICING 감사
            await self.set_pricing(
                session, alias=alias, data=req, actor=actor,
                ip_address=ip_address, request_id=request_id,
            )
            applied.append(alias)

        return PriceSyncApplyResponse(applied=applied, skipped=skipped, errors=errors)

    async def patch_status(
        self,
        session: AsyncSession,
        *,
        alias: str,
        data: StatusPatchRequest,
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> ModelResponse:
        repo = ModelRepository(session)
        new_status = ModelStatus.ACTIVE if data.active else ModelStatus.INACTIVE
        model = await repo.patch_status(alias, new_status)
        if model is None:
            raise NotFoundError("ModelAlias", alias)

        pricing = await repo.get_current_pricing(alias)

        # BR-MOD-03/04: Immediate cache invalidation on INACTIVE
        await self._cache_mgr.invalidate([f"model:{alias}", "model:list"], session=session)

        await audit_logger.log(
            session,
            actor_user_id=actor.user_id,
            actor_role=actor.role.value,
            action="PATCH_MODEL_STATUS",
            resource_type="ModelAlias",
            resource_id=alias,
            changes={"after": {"status": new_status.value}},
            ip_address=ip_address,
            request_id=request_id,
        )

        return self._to_response(model, pricing)

    @staticmethod
    def _to_response(model: ModelAlias, pricing: ModelPricing | None) -> ModelResponse:
        pricing_resp = None
        if pricing:
            pricing_resp = ModelPricingResponse(
                input_price_per_1k_tokens=pricing.input_price_per_1k_tokens,
                output_price_per_1k_tokens=pricing.output_price_per_1k_tokens,
                cache_creation_5m_price_per_1k_tokens=pricing.cache_creation_5m_price_per_1k_tokens,
                cache_creation_1h_price_per_1k_tokens=pricing.cache_creation_1h_price_per_1k_tokens,
                cache_read_price_per_1k_tokens=pricing.cache_read_price_per_1k_tokens,
                effective_from=pricing.effective_from,
                effective_until=pricing.effective_until,
            )
        return ModelResponse(
            alias=model.alias,
            provider=model.provider,
            provider_model_id=model.provider_model_id,
            endpoint_url=model.endpoint_url,
            api_format=model.api_format,
            status=model.status.value,
            description=model.description,
            display_name=model.display_name,
            current_pricing=pricing_resp,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )
