# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.clients import validate_clients
from app.schemas.common import ApiFormatEnum, ProviderEnum

# 단가 상한 — DB 컬럼에서 유도한 값이지 임의로 고른 숫자가 아니다.
#   app/models/model.py:100~109  → Numeric(10, 6)
#   db/init/02_create_tables.sql:222~226 → NUMERIC(10,6)  (0003_rename_cache_5m.py:34~38 도 동일)
# NUMERIC(10,6) = 전체 10자리 중 소수 6자리 ⇒ 정수부는 4자리뿐이므로 최대값이 9999.999999 다.
# 상한이 없으면 pydantic 은 통과시키고 asyncpg 가 INSERT 시점에 NumericValueOutOfRange 를
# 던져 그냥 500 이 된다(입력 오류인데 서버 장애처럼 보이고, 어느 필드가 문제인지도 안 나온다).
# ⚠️ 기존 decimal_places=6 **만으로는** 정수부를 전혀 제한하지 못한다(소수 자리 수만 본다).
#    max_digits=10 을 더하는 방법도 있다 — pydantic 2.13.2 실측으로는 max_digits-decimal_places
#    를 정수부 상한(4자리)으로 환산해 같은 결과를 낸다(decimal_whole_digits 에러). 그래도 여기서는
#    le 를 쓴다: pyproject 가 pydantic>=2.0.0 만 요구하므로 그 파생 규칙에 기대지 않고
#    DB 최대값을 그대로 적는 편이 버전에 무관하고 에러 메시지도 사람이 읽을 수 있다.
MAX_PRICE_PER_1K = Decimal("9999.999999")


def _validate_long_context_consistency(obj):
    """long-context 티어 설정의 반쪽 상태를 막는다 (마이그레이션 0038).

    calculate_cost 는 threshold 만 있고 long 단가가 없으면 short 로 폴백한다(fail-safe).
    그 폴백은 크래시는 막지만, 운영자가 "272K 티어를 켰다" 고 믿는데 실제로는 short 로 청구되는
    **조용한 무과금 티어**를 만든다. 그래서 입력 계층에서 반쪽 설정을 거부한다:
      · threshold 설정 → long input·output 단가 필수(요청마다 항상 있는 두 축).
      · ★ threshold 설정 + 어떤 캐시 버킷의 **short 단가가 0 보다 크면**(= 그 모델이 실제로
        캐시 과금을 한다) → 그 버킷의 long 단가도 필수. 안 그러면 272K 초과 캐시 토큰이
        _rate() 의 short 폴백으로 1배(=short) 청구되어 캐시 슬라이스만 조용히 과소청구된다.
        short 캐시 단가가 0 인 캐시-없는 모델은 long 캐시가 여전히 선택이다 — 그 버킷은
        어차피 0 이라 폴백해도 무해하기 때문.
      · long 단가만 있고 threshold 없음 → 절대 안 쓰이는 죽은 값 → 거부.
    ModelCreateRequest·PricingRequest 두 곳에서 공유한다.
    """
    thr = obj.long_context_threshold_tokens
    long_prices = {
        "long_context_input_price_per_1k_tokens": obj.long_context_input_price_per_1k_tokens,
        "long_context_output_price_per_1k_tokens": obj.long_context_output_price_per_1k_tokens,
        "long_context_cache_creation_5m_price_per_1k_tokens": obj.long_context_cache_creation_5m_price_per_1k_tokens,
        "long_context_cache_creation_1h_price_per_1k_tokens": obj.long_context_cache_creation_1h_price_per_1k_tokens,
        "long_context_cache_read_price_per_1k_tokens": obj.long_context_cache_read_price_per_1k_tokens,
    }
    any_long = any(v is not None for v in long_prices.values())
    if thr is not None:
        missing = [
            k for k in (
                "long_context_input_price_per_1k_tokens",
                "long_context_output_price_per_1k_tokens",
            )
            if long_prices[k] is None
        ]
        # 캐시 버킷: short 단가가 0 보다 크면 long 단가도 요구한다(0 이면 폴백 무해 → 선택).
        for short_f, long_f in (
            ("cache_read_price_per_1k_tokens", "long_context_cache_read_price_per_1k_tokens"),
            ("cache_creation_5m_price_per_1k_tokens", "long_context_cache_creation_5m_price_per_1k_tokens"),
            ("cache_creation_1h_price_per_1k_tokens", "long_context_cache_creation_1h_price_per_1k_tokens"),
        ):
            short_rate = getattr(obj, short_f, None)
            if short_rate is not None and short_rate > 0 and long_prices[long_f] is None:
                missing.append(long_f)
        if missing:
            raise ValueError(
                "long_context_threshold_tokens 를 설정하면 long input·output 단가가 필수이고, "
                "short 단가가 0 보다 큰 캐시 버킷은 그 long 단가도 필수입니다 "
                f"(누락: {missing}). 없으면 272K 초과가 조용히 short 요율로 청구됩니다."
            )
    elif any_long:
        raise ValueError(
            "long-context 단가를 설정하려면 long_context_threshold_tokens 도 설정해야 합니다 "
            "(threshold 없이는 그 단가가 절대 적용되지 않습니다)."
        )
    return obj


# ── long-context 티어 단가 필드 (마이그레이션 0038) ──
# ModelCreateRequest·PricingRequest 가 공유하는 6개 필드. web_search 와 달리 기본값이 None
# 이다 — long 티어는 모델별이라 보편 기본값이 없고, None = "티어 없음"(단일 요율)이 안전한
# 기본이다. GPT-5.6 처럼 티어가 있는 모델에만 채운다. le=MAX_PRICE_PER_1K 는 short 필드와
# 같은 이유(NUMERIC(10,6) 오버플로 → 500).


# ── Requests ──


class ModelCreateRequest(BaseModel):
    # ⚠️ extra="forbid" — 오타 키 하나가 **201 Created 와 함께** 런타임에만 깨지는 행을
    #    만든다. 실측(적대적 검증 PROBE3): `endpoint_ur1` 로 보내면 endpoint_url=NULL 인
    #    BEDROCK_RUNTIME_OPENAI 행이 201 로 생성되는데, 그 어댑터는 endpoint_url 없이는
    #    서명 리전조차 유도할 수 없다. `displayName`(camelCase) → display_name NULL.
    #    0003 이전 이름 `cache_creation_price_per_1k_tokens` → 캐시 생성 단가 0 으로 등록.
    #    전부 등록 시점엔 성공으로 보이고 나중에 장애/오과금으로만 드러난다.
    model_config = ConfigDict(extra="forbid")

    alias: str = Field(max_length=128)
    provider: ProviderEnum
    provider_model_id: str = Field(max_length=512)
    endpoint_url: str | None = None
    api_format: ApiFormatEnum
    description: str | None = None
    display_name: str | None = Field(default=None, max_length=128)
    input_price_per_1k_tokens: Decimal = Field(ge=0, le=MAX_PRICE_PER_1K, decimal_places=6)
    output_price_per_1k_tokens: Decimal = Field(ge=0, le=MAX_PRICE_PER_1K, decimal_places=6)
    cache_creation_5m_price_per_1k_tokens: Decimal = Field(
        default=Decimal("0"), ge=0, le=MAX_PRICE_PER_1K, decimal_places=6
    )
    cache_creation_1h_price_per_1k_tokens: Decimal = Field(
        default=Decimal("0"), ge=0, le=MAX_PRICE_PER_1K, decimal_places=6
    )
    cache_read_price_per_1k_tokens: Decimal = Field(
        default=Decimal("0"), ge=0, le=MAX_PRICE_PER_1K, decimal_places=6
    )

    # long-context 티어 (0038) — 등록 시 바로 티어 있는 모델을 만들 수 있게 parity 로 둔다.
    long_context_threshold_tokens: int | None = Field(default=None, ge=1)
    long_context_input_price_per_1k_tokens: Decimal | None = Field(
        default=None, ge=0, le=MAX_PRICE_PER_1K, decimal_places=6
    )
    long_context_output_price_per_1k_tokens: Decimal | None = Field(
        default=None, ge=0, le=MAX_PRICE_PER_1K, decimal_places=6
    )
    long_context_cache_creation_5m_price_per_1k_tokens: Decimal | None = Field(
        default=None, ge=0, le=MAX_PRICE_PER_1K, decimal_places=6
    )
    long_context_cache_creation_1h_price_per_1k_tokens: Decimal | None = Field(
        default=None, ge=0, le=MAX_PRICE_PER_1K, decimal_places=6
    )
    long_context_cache_read_price_per_1k_tokens: Decimal | None = Field(
        default=None, ge=0, le=MAX_PRICE_PER_1K, decimal_places=6
    )

    #: 이 모델을 쓸 수 있는 앱 허용목록. **3-상태**(models/model.py 주석 참조):
    #:   생략/``null``  제한 없음
    #:   ``[]``         명시적으로 빈 허용목록 = 어떤 앱도 허용되지 않음
    #:   목록           그 앱들만 허용
    allowed_clients: list[str] | None = None

    @field_validator("allowed_clients")
    @classmethod
    def _validate_clients(cls, v: list[str] | None) -> list[str] | None:
        # ⚠️ None 을 그대로 통과시켜야 한다 — [] 로 정규화하면 "제한 없음" 이
        #    "전면 거부" 로 바뀐다(정확히 반대 방향의 사고).
        return validate_clients(v)

    @model_validator(mode="after")
    def _validate_long_context(self):
        return _validate_long_context_consistency(self)


class ModelUpdateRequest(BaseModel):
    # ⚠️ extra="forbid" 필수. pydantic 기본값(extra="ignore")이면 여기 선언되지 않은 키가
    #    **조용히 버려진다**. 특히 provider / api_format 은 이 스키마에 없고 update 경로
    #    (services/model_service.py:update_model → repositories/model_repository.py:update_model)
    #    에도 없어서 admin API 로 바꿀 방법이 아예 없는 불변 필드인데, admin-ui 편집 폼은
    #    provider 드롭다운을 그대로 보여준다. 예전엔 운영자가 provider 를 바꿔 저장하면
    #    200 + 성공 토스트가 뜨고 DB 는 새 provider_model_id/endpoint_url + **옛**
    #    provider/api_format 의 반쪽 상태로 남아, 그 alias 의 모든 게이트웨이 호출이
    #    런타임에만 실패했다(편집 시점 경고 0). 이제 422 로 즉시 거부한다.
    #    provider 를 정말 바꾸려면 모델을 새로 등록해야 한다(불변 유지가 의도된 설계).
    #
    #    ⚠️ 정정: 예전 주석은 "ModelCreateRequest/PricingRequest 의 extra 키는 무해한 오타"
    #       라며 그쪽엔 forbid 를 넣지 않았다. **틀렸다** — PricingRequest 는 캐시 단가
    #       default 가 0 이라 오타가 곧 0 원 청구이고 직전 단가 행은 이미 닫힌다.
    #       ModelCreateRequest 는 오타가 201 과 함께 런타임에만 깨지는 행을 만든다.
    #       세 스키마 모두 forbid 로 통일했다(각 클래스 주석에 실측 근거).
    model_config = ConfigDict(extra="forbid")

    provider_model_id: str | None = None
    endpoint_url: str | None = None
    description: str | None = None
    # max_length matches VARCHAR(128); without it an overlong update would 500 at the DB
    # instead of a clean 422 (mirrors ModelCreateRequest.display_name).
    # NOTE: update uses an is-not-None filter, so display_name can be SET/changed but not
    # cleared back to NULL via the API (repo-wide behavior for all nullable update fields).
    display_name: str | None = Field(default=None, max_length=128)
    #: 3-상태. ⚠️ 여기서 "생략 = 유지" 와 "명시적 null = 제한 해제" 를 구별해야 한다.
    #:    null 을 생략과 같이 다루면 한 번 목록이 박힌 모델을 "제한 없음" 으로 되돌릴 API
    #:    가 사라지고, 콘솔은 그 목적으로 ``[]`` 를 보내게 된다 — 그런데 ``[]`` 는 전면
    #:    거부이므로 "제한 해제" 버튼이 그 모델을 통째로 막는다.
    #:    구별은 서비스 계층에서 ``model_fields_set`` 으로 한다.
    allowed_clients: list[str] | None = None

    @field_validator("allowed_clients")
    @classmethod
    def _validate_update_clients(cls, v: list[str] | None) -> list[str] | None:
        return validate_clients(v)



class PricingRequest(BaseModel):
    # ⚠️ extra="forbid" — 여기서는 **돈이 걸린다.** 이 스키마의 캐시 단가 3개는 default 가
    #    Decimal("0") 이라, 키 이름이 틀리면 값이 버려지고 0 이 들어간다. 그리고 이 엔드포인트는
    #    쓰기 전에 직전 단가 행을 close 하므로(services/model_service.py close_current_pricing),
    #    **새 ACTIVE 단가 행이 캐시 생성 비용을 0 으로 청구**하게 되고 되돌아갈 행도 없다.
    #    실측(적대적 검증 PROBE2): 0003 이전 이름 `cache_creation_price_per_1k_tokens` 로
    #    보내면 200, 기록된 행은 5m=0 · 1h=0, 직전 행은 이미 닫힘.
    #    ⇒ "extra 키는 무해한 오타" 라는 이전 판단은 이 스키마에서 반증됐다.
    #
    # le=MAX_PRICE_PER_1K: ModelCreateRequest 와 같은 이유(NUMERIC(10,6) 오버플로 → 500).
    # ⚠️ 이 스키마는 사용자 입력 외에 model_service.sync_aws_pricing 도 만들어 쓴다.
    #    AWS Price List 값이 비정상이면 DB 쓰기 전에 여기서 걸린다(더 이른 실패가 낫다).
    #    sync 는 선언된 필드만 kwargs 로 넘기므로 forbid 의 영향을 받지 않는다.
    model_config = ConfigDict(extra="forbid")

    input_price_per_1k_tokens: Decimal = Field(ge=0, le=MAX_PRICE_PER_1K, decimal_places=6)
    output_price_per_1k_tokens: Decimal = Field(ge=0, le=MAX_PRICE_PER_1K, decimal_places=6)
    cache_creation_5m_price_per_1k_tokens: Decimal = Field(
        default=Decimal("0"), ge=0, le=MAX_PRICE_PER_1K, decimal_places=6
    )
    cache_creation_1h_price_per_1k_tokens: Decimal = Field(
        default=Decimal("0"), ge=0, le=MAX_PRICE_PER_1K, decimal_places=6
    )
    cache_read_price_per_1k_tokens: Decimal = Field(
        default=Decimal("0"), ge=0, le=MAX_PRICE_PER_1K, decimal_places=6
    )
    # long-context 티어 (0038). set_pricing 은 **생략(model_fields_set 에 없음)** 을 "이전 행
    # 승계", **명시적 None** 을 "티어 제거" 로 구별한다 — short 단가만 고치는 PUT 이 272K
    # 티어를 조용히 지우지 않게 한다(model_service.py set_pricing 의 _keep).
    long_context_threshold_tokens: int | None = Field(default=None, ge=1)
    long_context_input_price_per_1k_tokens: Decimal | None = Field(
        default=None, ge=0, le=MAX_PRICE_PER_1K, decimal_places=6
    )
    long_context_output_price_per_1k_tokens: Decimal | None = Field(
        default=None, ge=0, le=MAX_PRICE_PER_1K, decimal_places=6
    )
    long_context_cache_creation_5m_price_per_1k_tokens: Decimal | None = Field(
        default=None, ge=0, le=MAX_PRICE_PER_1K, decimal_places=6
    )
    long_context_cache_creation_1h_price_per_1k_tokens: Decimal | None = Field(
        default=None, ge=0, le=MAX_PRICE_PER_1K, decimal_places=6
    )
    long_context_cache_read_price_per_1k_tokens: Decimal | None = Field(
        default=None, ge=0, le=MAX_PRICE_PER_1K, decimal_places=6
    )
    effective_from: datetime

    @model_validator(mode="after")
    def _validate_long_context(self):
        return _validate_long_context_consistency(self)


class StatusPatchRequest(BaseModel):
    active: bool


# ── Responses ──


class ModelPricingResponse(BaseModel):
    input_price_per_1k_tokens: Decimal
    output_price_per_1k_tokens: Decimal
    cache_creation_5m_price_per_1k_tokens: Decimal = Decimal("0")
    cache_creation_1h_price_per_1k_tokens: Decimal = Decimal("0")
    cache_read_price_per_1k_tokens: Decimal = Decimal("0")
    # long-context 티어 (0038) — 티어 없는 모델은 전부 None. 화면/미리보기가 현재 티어를
    # 표시할 수 있도록 노출한다.
    long_context_threshold_tokens: int | None = None
    long_context_input_price_per_1k_tokens: Decimal | None = None
    long_context_output_price_per_1k_tokens: Decimal | None = None
    long_context_cache_creation_5m_price_per_1k_tokens: Decimal | None = None
    long_context_cache_creation_1h_price_per_1k_tokens: Decimal | None = None
    long_context_cache_read_price_per_1k_tokens: Decimal | None = None
    effective_from: datetime
    effective_until: datetime | None = None


class ModelResponse(BaseModel):
    alias: str
    provider: ProviderEnum
    provider_model_id: str
    endpoint_url: str | None = None
    api_format: ApiFormatEnum
    status: str
    description: str | None = None
    display_name: str | None = None
    #: ``None`` = 제한 없음, ``[]`` = 허용 앱 없음, 목록 = 그 앱만. 화면이 이 세 상태를
    #: 구별해 보여줘야 한다 — ``[]`` 를 "제한 없음" 으로 렌더하면 운영자가 자기가 만든
    #: 전면 거부를 보지 못한다.
    allowed_clients: list[str] | None = None
    current_pricing: ModelPricingResponse | None = None
    created_at: datetime
    updated_at: datetime


class ModelListResponse(BaseModel):
    items: list[ModelResponse]


# ── AWS Price List 자동연동 (fetch ≠ apply) ──
# 이전 설계(PriceSyncService + PriceSync* 스키마)는 지웠다. 그 fetch 층이 regionCode 필터
# 없이 모든 SKU 를 substring 분류 + last-write-wins 로 누적해 다른 리전/티어 SKU 가 정상
# 요율을 덮었고(비결정적), apply 는 set_pricing 을 short 5필드로만 호출해 0038 의 long 컬럼을
# 매 적용마다 NULL 로 지웠다. 아래 Aws* 계약은 리전을 명시받고, 필드별 changes 를 그대로
# 노출하며(캐시·long 포함), 반영은 long 승계가 붙은 set_pricing 을 그대로 탄다.


class AwsPriceSyncRequest(BaseModel):
    # 운영자가 preview 에서 고른 alias 부분집합만 반영한다. 빈 목록은 no-op.
    aliases: list[str]
    # 대조·반영에 쓸 단가 리전. Price List API 엔드포인트(us-east-1)와는 별개다.
    # ⚠️ pub alias 는 In-Region(Geo CRIS)으로 시드됐다 — 기본값(us-east-1)을 그대로 두면
    #    비-US alias 에 US 요율이 써진다. 운영자는 alias 의 실제 서빙 리전을 넘겨야 한다.
    region_code: str = "us-east-1"


class AwsPriceChange(BaseModel):
    """한 단가 필드의 현재 DB 값 vs AWS 값. 둘 다 문자열(Decimal 직렬화) 또는 None."""

    field: str
    current: str | None = None
    aws: str | None = None


class AwsPricePreviewItem(BaseModel):
    alias: str
    provider_model_id: str
    # matched=False = Price List 에 이 pmid 의 standard 단가가 없다 = 자동연동 대상 아님.
    matched: bool
    aws_region_code: str | None = None
    aws_endpoint: str | None = None
    changes: list[AwsPriceChange] = Field(default_factory=list)
    note: str = ""


class AwsPricePreviewResponse(BaseModel):
    region_code: str
    items: list[AwsPricePreviewItem]


class AwsPriceSyncResponse(BaseModel):
    synced: list[str]
    # 매칭 안 되거나 존재하지 않는 alias — 조용히 넘기지 않고 되돌려준다.
    skipped: list[str]


# ── Team Allowed Models ──


class AllowedModelsSetRequest(BaseModel):
    """Replace-all semantics: provided list becomes the new full whitelist.

    빈 리스트 = 전체 허용 (엔트리 전부 삭제).
    """

    model_aliases: list[str] = Field(default_factory=list)


class AllowedModelsResponse(BaseModel):
    team_id: str
    model_aliases: list[str]
