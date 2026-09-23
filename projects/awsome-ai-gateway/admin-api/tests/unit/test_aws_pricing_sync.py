# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""preview_aws_pricing / sync_aws_pricing — 자동연동 오케스트레이션 (fetch ≠ apply).

preview 는 DB 를 안 건드리고 drift 만 낸다. sync 는 승인된 alias 만 set_pricing 경로로 반영하며,
AWS 가 SKU 로 주지 않는 항(short cache_1h)을 현재 행에서 **보존**해 full-replace clobber 를 막는다.
매칭 안 되는 alias 는 조용히 넘기지 않고 skipped 로 되돌린다.

⚠️ pub 에는 web_search 단가 컬럼이 없다 — phase2 의 web_search 커플링은 이식하지 않았고,
   마지막 테스트가 그 부재를 고정한다(다시 새어들면 실패).
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from app.core.cache_invalidation import CacheInvalidationManager
from app.models.model import ApiFormat, ModelAlias, ModelPricing, ModelStatus, Provider
from app.schemas.models import PricingRequest
from app.services.aws_price_list import AwsModelPrice
from app.services.model_service import ModelService


def _model(alias: str, pmid: str) -> ModelAlias:
    m = MagicMock(spec=ModelAlias)
    m.alias = alias
    m.provider = Provider.BEDROCK
    m.provider_model_id = pmid
    m.endpoint_url = None
    m.api_format = ApiFormat.OPENAI_RESPONSES if "gpt" in pmid else ApiFormat.BEDROCK_NATIVE
    m.status = ModelStatus.ACTIVE
    m.description = None
    m.display_name = None
    m.created_at = datetime.now(timezone.utc)
    m.updated_at = datetime.now(timezone.utc)
    return m


def _pricing(*, inp="0.002", cache_1h="0.009") -> ModelPricing:
    p = MagicMock(spec=ModelPricing)
    p.input_price_per_1k_tokens = Decimal(inp)
    p.output_price_per_1k_tokens = Decimal("0.012")
    p.cache_creation_5m_price_per_1k_tokens = Decimal("0.0025")
    p.cache_creation_1h_price_per_1k_tokens = Decimal(cache_1h)
    p.cache_read_price_per_1k_tokens = Decimal("0.0002")
    p.long_context_threshold_tokens = None
    p.long_context_input_price_per_1k_tokens = None
    p.long_context_output_price_per_1k_tokens = None
    p.long_context_cache_creation_5m_price_per_1k_tokens = None
    p.long_context_cache_creation_1h_price_per_1k_tokens = None
    p.long_context_cache_read_price_per_1k_tokens = None
    p.effective_from = datetime.now(timezone.utc)
    p.effective_until = None
    return p


_AWS = {
    "openai.gpt-5.6-terra": AwsModelPrice(
        provider_model_id="openai.gpt-5.6-terra",
        region_code="us-gov-west-1",
        endpoint="mantle",
        short={"input": Decimal("0.00264"), "output": Decimal("0.01584"),
               "cache_read": Decimal("0.000264"), "cache_write": Decimal("0.0033")},
        long={"input": Decimal("0.00528"), "output": Decimal("0.02376"),
              "cache_read": Decimal("0.000528"), "cache_write": Decimal("0.0066")},
    )
}


def _svc() -> ModelService:
    return ModelService(cache_mgr=MagicMock(spec=CacheInvalidationManager))


async def test_preview_flags_matched_drift_and_unmatched(mock_session):
    svc = _svc()
    terra = _model("gpt-5.6-terra", "us.openai.gpt-5.6-terra")
    claude = _model("claude-sonnet", "anthropic.claude-3-5-sonnet-20241022-v2:0")

    with patch("app.services.model_service.fetch_bedrock_prices", return_value=_AWS), \
         patch("app.services.model_service.ModelRepository") as MockRepo:
        repo = MockRepo.return_value
        repo.list_all = AsyncMock(return_value=[terra, claude])
        repo.get_current_pricing = AsyncMock(side_effect=[_pricing(inp="0.002"), _pricing()])
        resp = await svc.preview_aws_pricing(mock_session, region_code="us-gov-west-1")

    by_alias = {i.alias: i for i in resp.items}
    # terra: pmid 정규화 매칭(us. 접두사 벗김), input 0.002(DB) vs 0.00264(AWS) → change 존재
    t = by_alias["gpt-5.6-terra"]
    assert t.matched is True and t.aws_endpoint == "mantle"
    changed_fields = {c.field for c in t.changes}
    assert "input_price_per_1k_tokens" in changed_fields
    assert "long_context_threshold_tokens" in changed_fields  # DB None → AWS 272000
    # us. runtime alias 를 mantle SKU 에 맞췄으므로 확인 노트가 붙는다.
    assert "CRIS" in t.note
    # claude: Price List 에 없음 → matched=False
    assert by_alias["claude-sonnet"].matched is False


async def test_sync_preserves_cache_1h_and_applies_aws(mock_session, admin_user):
    svc = _svc()
    terra = _model("gpt-5.6-terra", "us.openai.gpt-5.6-terra")
    current = _pricing(inp="0.002", cache_1h="0.009")

    with patch("app.services.model_service.fetch_bedrock_prices", return_value=_AWS), \
         patch("app.services.model_service.audit_logger") as mock_audit, \
         patch("app.services.model_service.ModelRepository") as MockRepo:
        repo = MockRepo.return_value
        repo.get_by_alias = AsyncMock(return_value=terra)
        repo.get_current_pricing = AsyncMock(return_value=current)
        repo.close_current_pricing = AsyncMock()
        repo.create_pricing = AsyncMock()
        mock_audit.log = AsyncMock()

        resp = await svc.sync_aws_pricing(
            mock_session, aliases=["gpt-5.6-terra"],
            region_code="us-gov-west-1", actor=admin_user,
        )

    assert resp.synced == ["gpt-5.6-terra"] and resp.skipped == []
    written = repo.create_pricing.call_args.args[0]
    # AWS 값 반영
    assert written.input_price_per_1k_tokens == Decimal("0.00264")
    assert written.long_context_threshold_tokens == 272_000
    assert written.long_context_input_price_per_1k_tokens == Decimal("0.00528")
    # long 캐시 쓰기는 5m·1h 둘 다 30m SKU 값으로 채운다(단일 AWS 캐시-쓰기 SKU).
    assert written.long_context_cache_creation_5m_price_per_1k_tokens == Decimal("0.0066")
    assert written.long_context_cache_creation_1h_price_per_1k_tokens == Decimal("0.0066")
    # ★ AWS 가 안 주는 short 1h 는 현재 행에서 보존 — clobber 방지
    assert written.cache_creation_1h_price_per_1k_tokens == Decimal("0.009")


async def test_sync_clears_tier_when_aws_drops_long(mock_session, admin_user):
    """AWS 가 더 이상 long 티어를 노출하지 않으면(has_long False) sync 는 티어를 **제거**해야 한다.

    set_pricing 의 preserve-on-omit 이 명시적 null 을 구별하지 못하면, 이전에 티어가 있던
    행에 대해 sync 가 threshold=None 을 보내도 _keep 이 이전 티어를 되살려 제거가 무시됐다.
    sync 는 long 필드를 명시적으로(model_fields_set 포함) None 으로 보내므로 제거가 반영된다.
    """
    svc = _svc()
    terra = _model("gpt-5.6-terra", "us.openai.gpt-5.6-terra")
    prior_tiered = _pricing()
    prior_tiered.long_context_threshold_tokens = 272000
    prior_tiered.long_context_input_price_per_1k_tokens = Decimal("0.004")
    prior_tiered.long_context_output_price_per_1k_tokens = Decimal("0.018")
    # AWS 가 이번엔 long 을 안 준다 (input/output 없음)
    aws_no_long = {
        "openai.gpt-5.6-terra": AwsModelPrice(
            provider_model_id="openai.gpt-5.6-terra", region_code="us-gov-west-1", endpoint="mantle",
            short={"input": Decimal("0.00264"), "output": Decimal("0.01584")},
            long={},
        )
    }
    with patch("app.services.model_service.fetch_bedrock_prices", return_value=aws_no_long), \
         patch("app.services.model_service.audit_logger") as mock_audit, \
         patch("app.services.model_service.ModelRepository") as MockRepo:
        repo = MockRepo.return_value
        repo.get_by_alias = AsyncMock(return_value=terra)
        repo.get_current_pricing = AsyncMock(return_value=prior_tiered)
        repo.close_current_pricing = AsyncMock()
        repo.create_pricing = AsyncMock()
        mock_audit.log = AsyncMock()
        resp = await svc.sync_aws_pricing(
            mock_session, aliases=["gpt-5.6-terra"],
            region_code="us-gov-west-1", actor=admin_user,
        )
    assert resp.synced == ["gpt-5.6-terra"]
    written = repo.create_pricing.call_args.args[0]
    # ★ 티어가 실제로 제거됐다 — 이전 티어가 되살아나지 않는다
    assert written.long_context_threshold_tokens is None
    assert written.long_context_input_price_per_1k_tokens is None


async def test_sync_skips_unmatched_alias(mock_session, admin_user):
    svc = _svc()
    claude = _model("claude-sonnet", "anthropic.claude-3-5-sonnet-20241022-v2:0")
    with patch("app.services.model_service.fetch_bedrock_prices", return_value=_AWS), \
         patch("app.services.model_service.ModelRepository") as MockRepo:
        repo = MockRepo.return_value
        repo.get_by_alias = AsyncMock(return_value=claude)
        repo.get_current_pricing = AsyncMock(return_value=_pricing())
        repo.create_pricing = AsyncMock()

        resp = await svc.sync_aws_pricing(
            mock_session, aliases=["claude-sonnet"],
            region_code="us-gov-west-1", actor=admin_user,
        )

    assert resp.synced == [] and resp.skipped == ["claude-sonnet"]
    repo.create_pricing.assert_not_called()


def test_pricing_request_has_no_web_search_field():
    """⚠️ 회귀 방지(음성 대조군). pub 에는 web_search 단가 컬럼이 없다 — phase2 의 web_search
    커플링(web_search_price_per_query_usd)이 이식 중 다시 새어들면 여기서 실패한다."""
    assert not any("web_search" in f for f in PricingRequest.model_fields)
