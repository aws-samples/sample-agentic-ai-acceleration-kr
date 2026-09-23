# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import structlog
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_logger
from app.core.auth import CurrentUser
from app.core.cache_invalidation import CacheInvalidationManager
from app.core.exceptions import ConflictError, NotFoundError
from app.models.model import ApiFormat, ModelAlias, ModelPricing, ModelStatus, Provider
from app.repositories.model_repository import ModelRepository
from app.schemas.models import (
    AwsPriceChange,
    AwsPricePreviewItem,
    AwsPricePreviewResponse,
    AwsPriceSyncResponse,
    ModelCreateRequest,
    ModelPricingResponse,
    ModelResponse,
    ModelUpdateRequest,
    PricingRequest,
    StatusPatchRequest,
)
from app.services.aws_price_list import (
    LONG_CONTEXT_THRESHOLD_TOKENS,
    AwsModelPrice,
    fetch_bedrock_prices,
    long_tier_enabled,
    normalize_pmid,
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
            # None = 제한 없음(하위호환 기본값), [] = 허용 앱 없음, 목록 = 그 앱만.
            allowed_clients=data.allowed_clients,
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
            # long-context 티어 (0038) — 등록 시 바로 티어를 켤 수 있게 data 에서 그대로 받는다
            # (신규 행이라 승계 대상 없음; 티어 없는 모델은 스키마 기본값 None).
            long_context_threshold_tokens=data.long_context_threshold_tokens,
            long_context_input_price_per_1k_tokens=data.long_context_input_price_per_1k_tokens,
            long_context_output_price_per_1k_tokens=data.long_context_output_price_per_1k_tokens,
            long_context_cache_creation_5m_price_per_1k_tokens=data.long_context_cache_creation_5m_price_per_1k_tokens,
            long_context_cache_creation_1h_price_per_1k_tokens=data.long_context_cache_creation_1h_price_per_1k_tokens,
            long_context_cache_read_price_per_1k_tokens=data.long_context_cache_read_price_per_1k_tokens,
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

        # ── allowed_clients 의 "명시적 null = 제한 해제" ──
        #
        # 위 필터(`if v is not None`)는 이 저장소의 관례다: null 은 "생략" 과 같이
        # 취급해 값을 유지한다. 그런데 allowed_clients 는 그 관례에서 **한 칸 더**
        # 필요하다. canonical 의미가 `None`=제한 없음 / `[]`=허용 앱 없음 이므로,
        # null 을 필터로 버리면 **한 번 목록이 박힌 모델을 "제한 없음" 으로 되돌릴
        # API 가 사라진다.** 그러면 콘솔은 그 목적으로 `[]` 를 보낼 수밖에 없고,
        # `[]` 는 전면 거부이므로 "제한 해제" 버튼이 그 모델을 통째로 막는다.
        #
        # pydantic 의 `model_fields_set` 이 "키를 안 보냄" 과 "null 을 보냄" 을
        # 구별해 주므로, **명시적 null 만** 해제로 취급한다.
        #
        # ⚠️ 다른 nullable 필드(description / display_name / endpoint_url)는 오늘의
        #    "null = 무시" 동작을 그대로 둔다. 그 셋까지 바꾸면 null 을 "변경 안 함"
        #    으로 보내던 기존 호출자의 동작이 조용히 바뀐다 — 별건으로 다뤄야 한다.
        if "allowed_clients" in data.model_fields_set and data.allowed_clients is None:
            model.allowed_clients = None
            await session.flush()
            update_kwargs["allowed_clients"] = None

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

        # long-context 단가 승계 (0038): close_current_pricing 이 이전 활성 행을 닫기 **전에**
        # 그 행을 읽어 둔다. long 필드는 요청에서 생략되면 이전 행의 값을 이어받는다. 이 승계가
        # 없으면 운영자가 short 단가만 수정해도(또는 자동연동이 short 만 갱신해도) 새 행의 long
        # 컬럼이 NULL 로 리셋되어 272K 티어링이 조용히 꺼진다 — 삭제된 옛 sync 의 실제 회귀였다.
        prior = await repo.get_current_pricing(alias)

        def _keep(field_name):
            # ★ 생략(omit) 과 명시적 null 을 구별한다.
            #   · 필드가 요청에 **명시**됐으면(model_fields_set 에 있음) — 값이 None 이어도 —
            #     운영자/sync 의 의도이므로 그대로 쓴다. 이로써 "티어 제거"(long 필드를 null 로
            #     명시)가 실제로 반영된다.
            #   · 필드를 **생략**했으면(단가만 고치는 PUT) 이전 활성 행의 값을 승계한다 —
            #     short 단가만 수정해도 272K 티어링이 꺼지지 않게.
            if field_name in data.model_fields_set:
                return getattr(data, field_name)
            return getattr(prior, field_name) if prior else None

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
            # long-context 티어 (0038) — 생략 시 이전 행 승계, 명시 시 그 값(=제거 가능).
            long_context_threshold_tokens=_keep("long_context_threshold_tokens"),
            long_context_input_price_per_1k_tokens=_keep("long_context_input_price_per_1k_tokens"),
            long_context_output_price_per_1k_tokens=_keep("long_context_output_price_per_1k_tokens"),
            long_context_cache_creation_5m_price_per_1k_tokens=_keep("long_context_cache_creation_5m_price_per_1k_tokens"),
            long_context_cache_creation_1h_price_per_1k_tokens=_keep("long_context_cache_creation_1h_price_per_1k_tokens"),
            long_context_cache_read_price_per_1k_tokens=_keep("long_context_cache_read_price_per_1k_tokens"),
            effective_from=data.effective_from,
            created_by=actor.user_id,
        )
        await repo.create_pricing(pricing)

        # model:list 도 무효화한다 — list 엔드포인트가 _orm_to_schema 로 pricing 을 함께 실어
        # 보내므로, 여기서 안 지우면 요율 변경 뒤 MODEL_LIST_CACHE_TTL 동안 옛 단가를 응답한다.
        await self._cache_mgr.invalidate([f"model:{alias}", "model:list"], session=session)

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

    # ── AWS Price List 자동연동 (fetch ≠ apply) ──────────────────────────────────────
    # preview 는 DB 를 건드리지 않고 필드별 drift 만 계산한다. sync 는 운영자가 승인한 alias 만
    # 기존 set_pricing 경로로 반영한다 — 그래야 감사·캐시무효화·long-context 승계가 그대로
    # 적용되고 "가격 = 과금·차단" 을 사람 게이트 뒤에 둔다. fetch 는 aws_price_list 가 regionCode
    # 로 필터해 정확한 SKU 만 파싱하므로(옛 PriceSyncService 의 무필터+substring+last-write-wins
    # 회귀 없음), 여기서는 대조·반영만 한다.
    #
    # AWS 가 SKU 로 노출하지 않는 필드 — 대조/반영에서 제외하고 현재 값을 보존한다.
    #   short cache_creation_1h: AWS GPT 는 30m 캐시만 있다(우리 5m 컬럼에 대응). 게다가 이
    #     모델들의 1h 컬럼은 청구 경로에서 도달 불가(0032 주석)라 보존이 안전하다.

    @staticmethod
    def _dec(v) -> str | None:
        return None if v is None else str(v)

    def _diff_against_aws(
        self, model: ModelAlias, pricing: ModelPricing | None, aws: dict[str, AwsModelPrice]
    ) -> AwsPricePreviewItem:
        entry = aws.get(normalize_pmid(model.provider_model_id))
        if entry is None or "input" not in entry.short:
            return AwsPricePreviewItem(
                alias=model.alias,
                provider_model_id=model.provider_model_id,
                matched=False,
                note="AWS Price List 에 이 provider_model_id 의 standard 단가가 없다 — 자동연동 대상 아님.",
            )

        cur = pricing
        # preview 와 sync 는 동일 술어를 써야 한다 — 안 그러면 preview 가 켠다고 보여준 티어를
        # sync 가 조용히 건너뛴다.
        long_thr = LONG_CONTEXT_THRESHOLD_TOKENS if long_tier_enabled(entry) else None
        pairs: list[tuple[str, object, object]] = [
            ("input_price_per_1k_tokens", getattr(cur, "input_price_per_1k_tokens", None), entry.short.get("input")),
            ("output_price_per_1k_tokens", getattr(cur, "output_price_per_1k_tokens", None), entry.short.get("output")),
            ("cache_creation_5m_price_per_1k_tokens", getattr(cur, "cache_creation_5m_price_per_1k_tokens", None), entry.short.get("cache_write")),
            ("cache_read_price_per_1k_tokens", getattr(cur, "cache_read_price_per_1k_tokens", None), entry.short.get("cache_read")),
            ("long_context_threshold_tokens", getattr(cur, "long_context_threshold_tokens", None), long_thr),
            ("long_context_input_price_per_1k_tokens", getattr(cur, "long_context_input_price_per_1k_tokens", None), entry.long.get("input")),
            ("long_context_output_price_per_1k_tokens", getattr(cur, "long_context_output_price_per_1k_tokens", None), entry.long.get("output")),
            ("long_context_cache_creation_5m_price_per_1k_tokens", getattr(cur, "long_context_cache_creation_5m_price_per_1k_tokens", None), entry.long.get("cache_write")),
            ("long_context_cache_read_price_per_1k_tokens", getattr(cur, "long_context_cache_read_price_per_1k_tokens", None), entry.long.get("cache_read")),
        ]
        changes: list[AwsPriceChange] = []
        for fieldname, current_val, aws_val in pairs:
            # threshold 는 int, 나머지는 Decimal. 값 동등성으로 비교(스케일 무시).
            same = current_val is not None and aws_val is not None and (
                current_val == aws_val
                if fieldname == "long_context_threshold_tokens"
                else Decimal(current_val) == Decimal(aws_val)
            )
            if not same:
                changes.append(
                    AwsPriceChange(field=fieldname, current=self._dec(current_val), aws=self._dec(aws_val))
                )

        note = ""
        # CRIS(runtime) alias(global./us./in. 접두사)를 mantle SKU 단가에 맞춘 경우 경고한다.
        # Price List 가 이 계정에서 mantle SKU 만 노출하므로 매칭은 되지만, runtime track 의
        # 실제 단가(Geo/Global CRIS)는 mantle 과 다를 수 있어 운영자 확인이 필요하다.
        is_cris_alias = model.provider_model_id.startswith(("global.", "us.", "in."))
        if is_cris_alias and entry.endpoint == "mantle":
            note = f"AWS 소스는 {entry.endpoint} SKU 이고 이 alias 는 CRIS(runtime) track 이다 — 반영 전 확인 권장."
        return AwsPricePreviewItem(
            alias=model.alias,
            provider_model_id=model.provider_model_id,
            matched=True,
            aws_region_code=entry.region_code,
            aws_endpoint=entry.endpoint,
            changes=changes,
            note=note,
        )

    async def preview_aws_pricing(
        self, session: AsyncSession, *, region_code: str = "us-east-1"
    ) -> AwsPricePreviewResponse:
        """읽기 전용 drift 조회 — 각 활성 모델의 현재 단가 vs AWS Price List standard 단가."""
        repo = ModelRepository(session)
        # boto3 는 블로킹 — 이벤트 루프를 막지 않도록 스레드로 뺀다.
        aws = await asyncio.to_thread(fetch_bedrock_prices, region_code)
        models = await repo.list_all()
        items = []
        for model in models:
            pricing = await repo.get_current_pricing(model.alias)
            items.append(self._diff_against_aws(model, pricing, aws))
        return AwsPricePreviewResponse(region_code=region_code, items=items)

    async def sync_aws_pricing(
        self,
        session: AsyncSession,
        *,
        aliases: list[str],
        region_code: str,
        actor: CurrentUser,
        ip_address: str = "0.0.0.0",
        request_id: str = "",
    ) -> AwsPriceSyncResponse:
        """운영자가 승인한 alias 만 AWS 단가로 반영. 각 alias 는 기존 set_pricing 경로로 나가므로
        감사·캐시무효화·long 승계가 그대로 적용된다. AWS 가 안 주는 항(short cache_1h)은 현재
        행에서 보존해 full-replace clobber 를 막는다.
        """
        repo = ModelRepository(session)
        aws = await asyncio.to_thread(fetch_bedrock_prices, region_code)
        synced: list[str] = []
        skipped: list[str] = []

        for alias in aliases:
            model = await repo.get_by_alias(alias)
            if model is None:
                skipped.append(alias)
                continue
            entry = aws.get(normalize_pmid(model.provider_model_id))
            if entry is None or "input" not in entry.short or "output" not in entry.short:
                skipped.append(alias)
                continue

            current = await repo.get_current_pricing(alias)
            # 보존 항: AWS 에 SKU 가 없는 short 1h 캐시는 현재 값을 이어 쓴다(없으면 안전한 0).
            keep_1h = (
                current.cache_creation_1h_price_per_1k_tokens
                if current is not None
                else Decimal("0")
            )
            # preview(_diff_against_aws)와 **같은 술어**. 부분 AWS 데이터로 threshold 만 켜면
            # 반쪽-설정 검증이 422 를 내고, 설령 통과해도 그 축이 short 로 조용히 청구된다.
            has_long = long_tier_enabled(entry)
            try:
                data = PricingRequest(
                    input_price_per_1k_tokens=entry.short["input"],
                    output_price_per_1k_tokens=entry.short["output"],
                    cache_creation_5m_price_per_1k_tokens=entry.short.get("cache_write", Decimal("0")),
                    cache_creation_1h_price_per_1k_tokens=keep_1h,
                    cache_read_price_per_1k_tokens=entry.short.get("cache_read", Decimal("0")),
                    long_context_threshold_tokens=LONG_CONTEXT_THRESHOLD_TOKENS if has_long else None,
                    long_context_input_price_per_1k_tokens=entry.long.get("input") if has_long else None,
                    long_context_output_price_per_1k_tokens=entry.long.get("output") if has_long else None,
                    long_context_cache_creation_5m_price_per_1k_tokens=entry.long.get("cache_write") if has_long else None,
                    # AWS 는 캐시 쓰기 단가를 하나(30m)만 노출한다 → long 5m·1h 에 같은 값을 넣는다
                    # (0032/0038 seed 관례와 동일). 별도 AWS 소스가 없으므로 clobber 가 아니라 정본.
                    long_context_cache_creation_1h_price_per_1k_tokens=entry.long.get("cache_write") if has_long else None,
                    long_context_cache_read_price_per_1k_tokens=entry.long.get("cache_read") if has_long else None,
                    effective_from=datetime.now(timezone.utc),
                )
                await self.set_pricing(
                    session, alias=alias, data=data, actor=actor,
                    ip_address=ip_address, request_id=request_id,
                )
            except PydanticValidationError:
                # AWS 가 long input·output 은 주지만 캐시 long 을 안 주는 부분 데이터에서, 그 모델의
                # short 캐시 단가가 0 보다 크면 반쪽-설정 검증이 막는다. 한 alias 때문에 전체 sync
                # POST 를 500 으로 죽이지 않고 그 alias 만 건너뛴다(관측 가능하게 skipped).
                logger.warning("aws_sync_alias_rejected", alias=alias)
                skipped.append(alias)
                continue
            synced.append(alias)

        return AwsPriceSyncResponse(synced=synced, skipped=skipped)

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
                long_context_threshold_tokens=pricing.long_context_threshold_tokens,
                long_context_input_price_per_1k_tokens=pricing.long_context_input_price_per_1k_tokens,
                long_context_output_price_per_1k_tokens=pricing.long_context_output_price_per_1k_tokens,
                long_context_cache_creation_5m_price_per_1k_tokens=pricing.long_context_cache_creation_5m_price_per_1k_tokens,
                long_context_cache_creation_1h_price_per_1k_tokens=pricing.long_context_cache_creation_1h_price_per_1k_tokens,
                long_context_cache_read_price_per_1k_tokens=pricing.long_context_cache_read_price_per_1k_tokens,
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
            # 3-상태를 그대로 노출한다 — [] 를 None 으로 뭉개면 운영자가 자기가 만든
            # 전면 거부를 화면에서 볼 수 없다.
            allowed_clients=model.allowed_clients,
            description=model.description,
            display_name=model.display_name,
            current_pricing=pricing_resp,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )
