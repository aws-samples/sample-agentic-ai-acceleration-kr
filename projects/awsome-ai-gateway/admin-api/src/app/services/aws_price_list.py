# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""AWS Price List API 에서 Bedrock 단가를 가져와 우리 model_pricings 와 대조할 형태로 변환.

이 모듈은 **읽기 전용 fetch + 파싱**만 한다. DB 는 건드리지 않는다 — 가격은 곧 과금·차단이라
자동 apply 는 금지이고, 반영은 운영자가 콘솔에서 승인한 뒤 기존 set_pricing 경로로만 나간다
(services/model_service.py::sync_aws_pricing). 이 분리(fetch ≠ apply)가 이 파일의 존재 이유다.

usagetype 파싱 (`aws pricing get-products --service-code AmazonBedrock`):
    {REGION}-openai.gpt-5.6-{sol|terra|luna}-{endpoint}-{tokentype}[-long-ctx]-{tier}
  · unit 은 "1M tokens" → per-1k = USD / 1000.
  · long-ctx 토큰이 붙으면 272K 초과 티어(마이그레이션 0038 의 long 컬럼에 대응).
  · tier 는 service_tier 속성으로도 온다. standard 만 쓴다(priority/flex/batch 는 우리 모델이
    쓰지 않으므로 대조 대상이 아니다).

★ 매칭 키: 우리 DB 의 provider_model_id 는 mantle(0025)이 `openai.gpt-5.6-*`,
  runtime(0032)이 `us.openai.gpt-5.6-*` 다. Price List SKU 의 pmid 는 bare
  (`openai.gpt-5.6-*`) 이므로 `global.`/`us.`/`in.` CRIS 접두사를 벗겨 정규화한 뒤 맞춘다.
  매칭이 안 되는 alias 는 **조용히 넘기지 않고** 호출자가 "AWS 소스 없음" 으로 보고한다.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

import boto3
import structlog

logger = structlog.get_logger()

# Price List API 엔드포인트는 us-east-1 / ap-south-1 에만 있다(전역 서비스). 대상 region 의
# 단가는 regionCode 필터로 고른다 — 엔드포인트 region 과는 별개다.
PRICE_LIST_API_REGION = "us-east-1"

# usagetype 파서. endpoint 는 [a-z]+ 로 열어 둔다(오늘은 mantle 만 관측되지만 runtime SKU 가
# 나오면 그대로 잡히게). tokentype/tier 는 알려진 값만 받는다.
_USAGETYPE_RE = re.compile(
    r"^[A-Z0-9]+-"
    r"(?P<pmid>openai\.gpt-[0-9.]+-[a-z]+)-"
    r"(?P<endpoint>[a-z]+)-"
    r"(?P<token>input-tokens|output-tokens|cache-read-tokens|cache-write-tokens-30m)"
    r"(?P<longctx>-long-ctx)?-"
    r"(?P<tier>standard|priority|flex|batch)$"
)

# usagetype tokentype → 우리 per-1k 필드명 (short/long 공통).
_TOKEN_FIELD = {
    "input-tokens": "input",
    "output-tokens": "output",
    "cache-read-tokens": "cache_read",
    "cache-write-tokens-30m": "cache_write",
}

# AWS 가 272K 를 long-ctx 경계로 쓴다(모델 카드). SKU 자체에는 임계값 숫자가 없으므로 상수로 둔다.
LONG_CONTEXT_THRESHOLD_TOKENS = 272_000


def normalize_pmid(provider_model_id: str) -> str:
    """CRIS 접두사(global./us./in.)를 벗겨 Price List 의 bare pmid 와 맞춘다."""
    return re.sub(r"^(global|us|in)\.", "", provider_model_id)


def long_tier_enabled(entry: "AwsModelPrice") -> bool:
    """AWS 가 이 모델의 long 티어를 **적용 가능한 형태로** 노출하는가.

    preview(_diff_against_aws)와 sync(sync_aws_pricing)가 **반드시 같은 술어**를 써야 한다:
    preview 가 `entry.long` truthy 만 보고 "272K 티어 켜짐" 을 보여주는데 sync 는 input·output
    둘 다 있어야 threshold 를 켜면, 부분 AWS long 데이터에서 preview 는 티어를 약속하고 sync 는
    조용히 건너뛴다. long 요율은 threshold + long input·output 이 성립할 때만 의미가 있으므로
    그 둘의 존재를 기준으로 삼는다.
    """
    return "input" in entry.long and "output" in entry.long


@dataclass
class AwsModelPrice:
    """한 (정규화된 pmid) 의 AWS standard-tier 단가 스냅샷. per-1k Decimal."""

    provider_model_id: str  # bare, 정규화됨
    region_code: str
    endpoint: str  # SKU 태그 (mantle 등) — 우리 alias 의 track 과 다를 수 있어 노출한다
    short: dict[str, Decimal] = field(default_factory=dict)  # input/output/cache_read/cache_write
    long: dict[str, Decimal] = field(default_factory=dict)


# 우리 단가 컬럼은 NUMERIC(10,6) 이고 PricingRequest 도 decimal_places=6 이다. per-1M 을
# 1000 으로 나누면 6자리를 넘는 값이 나올 수 있고(예: $0.00007725/1M → 0.00000007725),
# 그대로 PricingRequest 에 넣으면 422, DB 에 넣으면 절삭된다. 파서에서 6자리로 반올림해
# 저장 형식과 정확히 맞춘다(cost 계산과 같은 ROUND_HALF_UP).
_PER_1K_QUANT = Decimal("0.000001")


def _price_per_1k(unit: str, usd: str) -> Decimal:
    """priceDimension → per-1k Decimal(6dp).

    ★ 단위 문자열의 **토큰 수량을 파싱**한다 — substring 포함검사(`"1000" in u`)를 쓰지 않는다.
      substring 방식은 `"1000000 tokens"`(콤마·'M' 없는 bare per-1M)를 "1000 포함" 으로 오독해
      /1000 을 안 하고 1000배 과소청구를 만든다. 수량 qty 를 뽑아
      per_1k = usd * 1000 / qty 로 계산하면 어떤 표기(1M / 1K / 1000000 / 1,000,000)든 정확하다.
    """
    val = Decimal(usd)
    u = unit.lower().replace(",", "").strip()
    m = re.match(r"([0-9.]+)\s*([km]?)", u)
    if not m or not m.group(1):
        raise ValueError(f"unexpected Price List unit {unit!r} (usd={usd})")
    qty = Decimal(m.group(1))
    suffix = m.group(2)
    if suffix == "m":
        qty *= 1_000_000
    elif suffix == "k":
        qty *= 1000
    if qty <= 0:
        raise ValueError(f"non-positive token quantity in unit {unit!r}")
    per_1k = val * 1000 / qty
    return per_1k.quantize(_PER_1K_QUANT, rounding=ROUND_HALF_UP)


def _iter_products(client, region_code: str):
    """AmazonBedrock 제품을 regionCode 로 필터해 전 페이지 순회."""
    paginator = client.get_paginator("get_products")
    for page in paginator.paginate(
        ServiceCode="AmazonBedrock",
        Filters=[{"Type": "TERM_MATCH", "Field": "regionCode", "Value": region_code}],
    ):
        for raw in page.get("PriceList", []):
            yield json.loads(raw)


def fetch_bedrock_prices(region_code: str = "us-east-1") -> dict[str, AwsModelPrice]:
    """Price List 에서 OpenAI GPT standard-tier 단가를 정규화 pmid → AwsModelPrice 로 반환.

    boto3 pricing 클라이언트는 항상 PRICE_LIST_API_REGION 으로 만든다(그 리전에만 엔드포인트가
    있다). 대조할 실제 단가 리전은 region_code 인자로 고른다.
    """
    client = boto3.client("pricing", region_name=PRICE_LIST_API_REGION)
    out: dict[str, AwsModelPrice] = {}
    skipped = 0
    for product in _iter_products(client, region_code):
        # ★ 제품별 격리: SKU 하나가 미지의 단위/필드 형태여도 전체 preview/sync 를 500 으로
        #   죽이지 않는다. 우리가 매칭하는 것은 openai gpt standard SKU 뿐이고, 그 외/깨진
        #   항목은 건너뛰고 계속한다. 매칭 대상이 파싱 실패하면 그 항목만 빠지고(로그로 관측),
        #   나머지 단가는 정상 반영된다.
        try:
            attrs = product.get("product", {}).get("attributes", {})
            ut = attrs.get("usagetype", "")
            m = _USAGETYPE_RE.match(ut)
            if not m or m.group("tier") != "standard":
                continue
            pmid = m.group("pmid")
            field_name = _TOKEN_FIELD[m.group("token")]
            is_long = bool(m.group("longctx"))

            # priceDimension 에서 unit + USD 추출 (OnDemand 단일 term).
            price = None
            for term in product.get("terms", {}).get("OnDemand", {}).values():
                for pd in term.get("priceDimensions", {}).values():
                    price = _price_per_1k(pd["unit"], pd["pricePerUnit"]["USD"])
            if price is None:
                continue

            entry = out.get(pmid)
            if entry is None:
                entry = AwsModelPrice(
                    provider_model_id=pmid,
                    region_code=attrs.get("regionCode", region_code),
                    endpoint=m.group("endpoint"),
                )
                out[pmid] = entry
            (entry.long if is_long else entry.short)[field_name] = price
        except (KeyError, ValueError, TypeError) as exc:
            skipped += 1
            logger.warning(
                "aws_price_list_sku_skipped",
                usagetype=product.get("product", {}).get("attributes", {}).get("usagetype"),
                error=str(exc),
            )
            continue
    if skipped:
        logger.info("aws_price_list_partial", skipped=skipped, parsed=len(out))
    return out
