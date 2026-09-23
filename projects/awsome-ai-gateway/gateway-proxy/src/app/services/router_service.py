# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import json
from decimal import Decimal
from typing import Optional

import structlog
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.model import ModelAlias, ModelPricing
from app.observability.provider_metrics import record_cache_hit
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

# The OpenAI **Responses** wire is served by two different Bedrock planes, and a client on
# /v1/responses may legitimately use either — Mantle (bearer, openai.gpt-5.x) or the
# standard runtime plane (SigV4, us./global. CRIS ids). Both are acceptable for the same
# routing profile, so the model row's own provider — not the profile — decides which
# adapter runs. Anything OUTSIDE this tuple (a Bedrock-native or vLLM alias) must still be
# unreachable from /v1/responses, which is why this is an allow-list and not a wildcard.
OPENAI_RESPONSES_PROVIDERS: tuple[ProviderType, ...] = (
    ProviderType.BEDROCK_MANTLE_OPENAI,
    ProviderType.BEDROCK_RUNTIME_OPENAI,
)

# Same idea for the /v1/chat/completions wire: in-house vLLM plus the Bedrock runtime
# plane, which serves the identical Chat dialect. Mantle is deliberately absent — the
# shipped Mantle adapter only ever posts to /v1/responses.
OPENAI_CHAT_PROVIDERS: tuple[ProviderType, ...] = (
    ProviderType.OPENMODEL,
    ProviderType.BEDROCK_RUNTIME_OPENAI,
)


class ModelInactiveError(LookupError):
    """An alias exists and matches the expected provider, but its status is INACTIVE.

    A LookupError subclass so every existing ``except LookupError`` keeps behaving
    exactly as before (callers that only care "could not resolve" need no change).
    The distinction matters where a caller FALLS BACK on a failed lookup: setting
    status=INACTIVE is an operator kill switch, so it must deny the request rather
    than silently reroute it to some other model (see routers/openai_compat.py).
    """


class ClientModelScopeError(PermissionError):
    """모델 × 앱 축의 거부(:func:`check_client_model_scope`).

    ``PermissionError`` 의 하위 클래스이므로 기존 ``except PermissionError`` 는 그대로
    잡는다 — 이 타입을 도입해도 어느 호출 지점도 동작이 바뀌지 않는다.

    왜 구별하나: 계정 축 거부와 앱 축 거부는 사용자가 해야 할 일이 다르다. 한 문구로
    합치면 Codex 에서만 막힌 사용자가 계정 권한을 요청하러 가고, 운영자는 이미 권한이
    있다고 답한다 — 양쪽 다 맞는 말인데 아무도 원인을 못 찾는다.
    """


def check_client_scope(allowed_clients: list[str] | None, client: str | None) -> None:
    """Raise PermissionError if the identified client is not allowed for this user.

    allowed_clients None/[] = 전체 허용(both). 값이 있으면 화이트리스트 — client 가
    그 안에 없으면 거부. client 'other'/None 은 화이트리스트가 있으면 항상 거부.
    """
    if not allowed_clients:
        return
    if client not in allowed_clients:
        raise PermissionError(f"Client '{client}' not allowed for this key")


def check_client_model_scope(model_config, client: str | None) -> None:
    """앱(client)이 이 **모델**을 쓸 수 없으면 PermissionError.

    ``model_aliases.allowed_clients`` 의 canonical 의미(전 계층 동일, migration 0035):

      * ``None`` (SQL NULL) = **제한 없음** — 지금 앱과 나중에 추가될 앱 전부.
      * ``[]`` (SQL ``{}``) = **명시적으로 빈 허용목록** — 어떤 앱도 쓸 수 없다.
      * non-empty = 허용목록. 목록 밖 client 는 거부되며 ``other``/``None`` 도 거부.

    ⚠️ 바로 위 :func:`check_client_scope` 와 **의미가 다르다.** 그쪽은 사용자 × 앱 축이고
       ``None``/``[]`` 를 둘 다 전체 허용으로 본다(그 필드는 "이 키가 쓸 수 있는 앱" 이라
       빈 값이 "제한 없음" 을 뜻하도록 설계됐다). 이쪽은 모델 × 앱 축이고 ``[]`` 가
       **전면 거부**다 — 그래서 두 함수를 합치면 안 된다.

       ``[]`` 를 fail-closed 로 두는 이유: ``[]`` 는 운영자가 콘솔에서 마지막 앱의 체크를
       해제했을 때 만들어지는 값이다. 화면은 "허용된 앱 없음" 으로 보여주는데 게이트가
       전부 통과시키면, 운영자가 방금 내린 제한이 아무 효과가 없다. 접근제어 필드는 빈
       경우에 닫혀야 하고, ``NULL`` vs ``{}`` 가 "미설정" 과 "명시적으로 비움" 을
       구별하는 유일한 축이다 — 그래서 falsiness 가 아니라 ``is None`` 으로 판정한다.

    :func:`check_key_scope`(allowed_models 게이트)와 AND 로 걸린다.
    """
    allowed = getattr(model_config, "allowed_clients", None)
    if allowed is None:
        return
    if client not in allowed:
        raise ClientModelScopeError(
            f"Model '{getattr(model_config, 'alias', '?')}' not allowed for client '{client}'"
        )


def _orm_to_schema(alias_row: ModelAlias, pricing_row: Optional[ModelPricing]) -> ModelConfigSchema:
    if pricing_row:
        p = ModelPricingSchema(
            input_per_1k=pricing_row.input_price_per_1k_tokens,
            output_per_1k=pricing_row.output_price_per_1k_tokens,
            cache_write_per_1k=pricing_row.cache_creation_5m_price_per_1k_tokens,
            cache_write_1h_per_1k=pricing_row.cache_creation_1h_price_per_1k_tokens,
            cache_read_per_1k=pricing_row.cache_read_price_per_1k_tokens,
            # long-context 티어(0038). short 컬럼과 같은 방식으로 직접 읽는다(전부 nullable;
            # NULL threshold = 티어 없음). DB 컬럼명 → 스키마 필드명 매핑.
            long_context_threshold_tokens=pricing_row.long_context_threshold_tokens,
            long_input_per_1k=pricing_row.long_context_input_price_per_1k_tokens,
            long_output_per_1k=pricing_row.long_context_output_price_per_1k_tokens,
            long_cache_write_per_1k=pricing_row.long_context_cache_creation_5m_price_per_1k_tokens,
            long_cache_write_1h_per_1k=pricing_row.long_context_cache_creation_1h_price_per_1k_tokens,
            long_cache_read_per_1k=pricing_row.long_context_cache_read_price_per_1k_tokens,
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
        # getattr 로 읽는다 — 0035 를 적용하지 않은 DB(구 배포와의 롤링 창)에서도
        # 부팅이 죽지 않아야 하고, 그때는 None(제한 없음) = 오늘 동작이다.
        allowed_clients=getattr(alias_row, "allowed_clients", None),
    )


def _parse_cached_model_list(cached: str) -> Optional[list[ModelConfigSchema]]:
    """Parse a cached ``model:list`` payload, returning None on any failure.

    Same defense as :func:`_parse_cached_model`, which the single-model path has had
    since P0-④ — this path did not, and the asymmetry was the bug. ``model:list`` is a
    single provider-agnostic key holding EVERY active model, so one unparseable entry
    poisons the whole endpoint, and it stays poisoned for the full 300 s TTL because the
    exception fires before any rebuild can run.

    The realistic trigger is a rolling deploy, not corruption: a NEW pod writes the key
    with a provider label (``BEDROCK_RUNTIME_OPENAI``) that an OLD pod's ``ProviderType``
    does not know, and every old pod then 500s on ``GET /v1/models`` — an outage caused
    by deploying, in the direction nobody tests. All-or-nothing is deliberate: silently
    serving a filtered catalogue would make a model look deregistered, so we rebuild
    from the DB instead (returning None = treat as a miss).
    """
    try:
        return [ModelConfigSchema(**m) for m in json.loads(cached)]
    except Exception:
        logger.warning("model_list_cache_parse_failed_treating_as_miss", cache_key="model:list")
        return None


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
    # ⚠️ **클래스** 속성이다. 라우터 모듈 3곳이 각자 `RouterService()` 를 만들기 때문에
    #    인스턴스 주입으로는 한 곳만 계측된다. main.py 의 lifespan 에서
    #    `RouterService.metrics = gateway_metrics` 로 한 번 주입하면 세 인스턴스가 모두
    #    본다. 미설정(테스트 등)이면 None → record_cache_hit 이 no-op 이다.
    #
    #    파라미터로 넘기지 않는 이유: 진입점 호출부가 23곳이고, 그 전부에 지표 인자를
    #    끼우는 것은 계측이 얻는 값보다 회귀 위험이 크다.
    metrics = None

    """모델 alias 조회 및 Key Scope 검사."""

    async def _resolve_by_providers(
        self,
        redis,
        db: Optional[AsyncSession],
        model_ref: str,
        providers: tuple[ProviderType, ...],
    ) -> ModelConfigSchema:
        """alias OR provider_model_id 로 모델 1건 해석 (provider 화이트리스트 적용).

        ``resolve_bedrock_model`` / ``resolve_mantle_model`` / ``resolve_codex_model`` 의
        공통 본문. 원래는 provider 1개만 비교하는 코드가 두 벌 복사돼 있었는데, OpenAI
        Responses 와 Chat 이 각각 **두 개의 provider** (Mantle/runtime, vLLM/runtime) 로
        서비스되면서 단일 비교로는 표현이 불가능해졌다. providers 가 1-튜플이면 동작은
        기존과 완전히 동일하다(= 회귀 없음).

        Redis 캐시는 provider 무관 단일 네임스페이스(``model:{ref}``)를 공유하므로,
        캐시 히트 후에도 provider 화이트리스트를 **다시** 검사해야 한다. 그러지 않으면
        예컨대 Bedrock-native alias 가 캐시에 있을 때 /v1/responses 로 도달한다.

        Raises:
            ModelInactiveError: alias 는 있으나 status=INACTIVE (운영자 kill switch).
            LookupError: 미등록 또는 provider 화이트리스트 불일치.
        """
        provider_values = [p.value for p in providers]

        if redis is not None:
            cached = await redis.get(f"model:{model_ref}")
            if cached:
                schema = _parse_cached_model(cached, model_ref)
                if schema is not None:
                    record_cache_hit(RouterService.metrics, kind="model")
                    if schema.status == ModelStatus.INACTIVE:
                        raise ModelInactiveError(f"Model '{schema.alias or model_ref}' is inactive")
                    if schema.provider not in providers:
                        raise LookupError(f"Model alias '{model_ref}' not found")
                    return schema
                # parse failed → fall through to DB rebuild (self-heal poison entry)

        if db is None:
            raise LookupError(f"Model alias '{model_ref}' not found (DB unavailable)")

        # alias 정확 매칭 우선, 없으면 provider_model_id fallback (동일 provider_model_id가
        # 여러 alias에 매핑될 수 있으므로 limit(1) 사용).
        result = await db.execute(
            select(ModelAlias).where(
                and_(
                    ModelAlias.provider.in_(provider_values),
                    ModelAlias.alias == model_ref,
                )
            ).limit(1)
        )
        alias_row = result.scalar_one_or_none()

        if alias_row is None:
            result = await db.execute(
                select(ModelAlias).where(
                    and_(
                        ModelAlias.provider.in_(provider_values),
                        ModelAlias.provider_model_id == model_ref,
                    )
                ).limit(1)
            )
            alias_row = result.scalar_one_or_none()
        if alias_row is None:
            raise LookupError(f"Model alias '{model_ref}' not found")

        if alias_row.status != "ACTIVE":
            raise ModelInactiveError(f"Model '{alias_row.alias}' is inactive")

        pricing_row = await _fetch_latest_pricing(db, alias_row.alias)
        schema = _orm_to_schema(alias_row, pricing_row)

        if redis is not None:
            payload = schema.model_dump_json()
            await redis.setex(f"model:{schema.alias}", MODEL_CACHE_TTL, payload)
            await redis.setex(f"model:{schema.provider_model_id}", MODEL_CACHE_TTL, payload)

        return schema

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
        return await self._resolve_by_providers(
            redis, db, model_ref, (ProviderType.BEDROCK,)
        )

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
        return await self._resolve_by_providers(redis, db, model_ref, (expected_provider,))

    async def resolve_codex_model(
        self,
        redis,
        db: Optional[AsyncSession],
        model_ref: str,
    ) -> ModelConfigSchema:
        """/v1/responses 용 모델 해석 — **두 Bedrock plane 을 모두 수용**한다.

        BEDROCK_MANTLE_OPENAI (bearer, openai.gpt-5.x) 와 BEDROCK_RUNTIME_OPENAI
        (SigV4, us./global. CRIS) 는 동일한 Responses 방언을 쓰므로, 같은 클라이언트가
        모델 이름만 바꿔 두 plane 을 오갈 수 있다. 어느 plane 으로 나갈지는 **해석된
        모델 row 의 provider** 가 결정한다(routing profile 이 아니라). 그 외 provider
        (Bedrock native / vLLM) 는 이 경로에서 여전히 도달 불가.
        """
        return await self._resolve_by_providers(
            redis, db, model_ref, OPENAI_RESPONSES_PROVIDERS
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
        """/v1/chat/completions (+/v1/completions) 용 모델 해석.

        in-house vLLM(OPENMODEL) 과 Bedrock runtime plane(BEDROCK_RUNTIME_OPENAI) 을 모두
        수용한다 — 후자는 동일한 Chat Completions 방언을 SigV4 + CRIS 모델 id 로 서비스한다.
        어느 어댑터로 나갈지는 해석된 row 의 provider 가 결정한다(routers/openai_compat).
        Mantle 계열은 여기서 여전히 도달 불가(그 어댑터는 /v1/responses 전용).

        Raises:
            ModelInactiveError: alias 는 있으나 status=INACTIVE.
            LookupError: 미등록 또는 provider 불일치.
        """
        return await self._resolve_by_providers(redis, db, alias, OPENAI_CHAT_PROVIDERS)

    def check_client_model_scope(self, model_config, client: str | None) -> None:
        """모듈 레벨 :func:`check_client_model_scope` 로 위임.

        ``allowed_clients`` 가 ``None`` 이면 제한 없음, ``[]`` 면 어떤 앱도 허용되지 않음.
        """
        check_client_model_scope(model_config, client)

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
                schemas = _parse_cached_model_list(cached)
                if schemas is not None:
                    record_cache_hit(RouterService.metrics, kind="model_list")
                    return schemas
                # parse failed → drop the poisoned key so the rebuild below is not
                # racing a still-poisoned entry, then fall through to the DB.
                # delete (not just overwrite) because if the DB is also unavailable we
                # must serve an empty list rather than keep 500ing for 300 s.
                try:
                    await redis.delete(cache_key)
                except Exception:  # noqa: BLE001 — cache eviction must never fail a read
                    logger.warning("model_list_cache_delete_failed", cache_key=cache_key)

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
