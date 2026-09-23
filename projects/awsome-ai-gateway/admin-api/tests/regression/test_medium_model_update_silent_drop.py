# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""Regression: PUT /admin/models/{alias} 가 provider/api_format 을 조용히 버리고, 단가 오버플로가 500 으로 새던 문제.

**결함 ①(medium) — 조용한 필드 유실.**
``ModelUpdateRequest``(admin-api/src/app/schemas/models.py)는 provider_model_id /
endpoint_url / description / display_name 만 선언한다. pydantic 기본값이
``extra="ignore"`` 라서 클라이언트가 ``provider`` 나 ``api_format`` 을 함께 보내면
**아무 말 없이 버려지고** API 는 200 을 준다. 그런데 provider/api_format 은 update 경로
(services/model_service.py:update_model → repositories/model_repository.py:update_model)
에 아예 없어서 admin API 로 바꿀 방법이 없는 **불변** 필드다. 그 결과가 반쪽 상태다:
새 provider_model_id/endpoint_url + 옛 provider/api_format 으로 DB 행이 남고, 그 alias 로
들어오는 게이트웨이 호출은 전부 런타임에만 실패한다(편집 시점 경고 0).

도달 경로는 실재했다 — admin-ui 편집 다이얼로그의 Provider ``<select>`` 에는
``disabled={isEditMode}`` 가 없었고(같은 파일의 alias input 에는 있다) 초기값을
``editModel.provider`` 로 채워 준다. 즉 운영자는 Provider 를 바꿔 저장하고 초록색 성공
토스트를 받는다. 수정은 두 겹이다: 스키마가 ``extra="forbid"`` 로 422 를 주고, UI 는
저장할 수 없는 컨트롤을 편집 모드에서 잠근다(같은 클러스터의 vitest 가 UI 쪽을 지킨다).

**결함 ②(low) — 단가 상한 부재.**
단가 필드는 ``ge=0, decimal_places=6`` 만 있었고 컬럼은 ``NUMERIC(10,6)``
(app/models/model.py:100~109, db/init/02_create_tables.sql:222~226)이다. 정수부가 4자리뿐이라
10000 이상은 저장이 불가능한데 pydantic 은 통과시켜서 asyncpg 가 INSERT 에서
NumericValueOutOfRange 를 던졌다 → 입력 오류인데 **필드명도 없는 맨 500**. 이제 컬럼에서
유도한 ``le=9999.999999`` 로 422 를 준다.

⚠️ 이 파일의 하네스는 **HTTP 레벨**이다(라우터 → 스키마 → 서비스 → 리포지토리). 스키마
   객체를 직접 만들어 검사하면 이 결함을 못 잡는다 — 실제로 unit/test_model_service.py 는
   항상 ``ModelUpdateRequest(description=...)`` 만 만들었기 때문에 몇 달간 못 봤다.
   extra 키는 **직렬화된 요청 본문**에만 존재하므로 반드시 JSON 을 보내야 한다.
   에러 봉투도 손으로 복제하지 않고 프로덕션의 register_exception_handlers 를 그대로 쓴다.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock, MagicMock, patch

from app.core.auth import CurrentUser, require_admin
from app.core.cache_invalidation import CacheInvalidationManager
from app.core.db import (
    SESSION_STATE_ATTR,
    get_db_session,
    install_commit_before_response,
)
from app.main import register_exception_handlers
from app.models.auth import UserRole
from app.models.model import ApiFormat, ModelStatus, Provider
from app.schemas.models import MAX_PRICE_PER_1K
from app.services.model_service import ModelService

ADMIN = CurrentUser(
    user_id=uuid.UUID("00000000-0000-0000-0000-0000000000ad"),
    email="admin@test.com",
    role=UserRole.ADMIN,
    team_id=uuid.UUID("00000000-0000-0000-0000-0000000000a1"),
)

ALIAS = "claude-sonnet-regression"


class _FakeModel:
    """ModelAlias 중 _to_response 와 update 경로가 만지는 표면만 가진 행 대역."""

    def __init__(self) -> None:
        self.alias = ALIAS
        self.provider = Provider.BEDROCK
        self.provider_model_id = "anthropic.claude-3-5-sonnet-20241022-v2:0"
        self.endpoint_url = None
        self.api_format = ApiFormat.BEDROCK_NATIVE
        self.status = ModelStatus.ACTIVE
        self.description = "before"
        self.display_name = "Before"
        # ⚠️ 실물 ModelAlias 에 있는 필드는 여기도 있어야 한다. 없으면 _to_response 가
        #    AttributeError 로 터져 500 이 되고, 그 500 은 "이 테스트가 검증하려는 결함"
        #    처럼 보인다(실제로 그렇게 오독될 뻔했다). None = per-app 제한 없음.
        self.allowed_clients = None
        self.created_at = datetime.now(timezone.utc)
        self.updated_at = datetime.now(timezone.utc)


class _FakePricing:
    def __init__(self) -> None:
        self.input_price_per_1k_tokens = Decimal("0.003")
        self.output_price_per_1k_tokens = Decimal("0.015")
        self.cache_creation_5m_price_per_1k_tokens = Decimal("0.00375")
        self.cache_creation_1h_price_per_1k_tokens = Decimal("0.006")
        self.cache_read_price_per_1k_tokens = Decimal("0.0003")
        # long-context 티어 (0038) — 실 ModelPricing 과 같은 모양이어야 set_pricing 의 _keep
        # (prior 승계)과 _to_response 가 읽을 수 있다. 티어 없는 모델이라 전부 None.
        self.long_context_threshold_tokens = None
        self.long_context_input_price_per_1k_tokens = None
        self.long_context_output_price_per_1k_tokens = None
        self.long_context_cache_creation_5m_price_per_1k_tokens = None
        self.long_context_cache_creation_1h_price_per_1k_tokens = None
        self.long_context_cache_read_price_per_1k_tokens = None
        self.effective_from = datetime.now(timezone.utc)
        self.effective_until = None


class _RecordingRepo:
    """ModelRepository 대역 — 실제 리포지토리와 같은 방식으로 kwargs 를 행에 반영한다.

    ⚠️ AsyncMock 대신 이걸 쓰는 이유: "무엇이 DB 로 갔는가" 를 봐야 조용한 유실을 잡는다.
       ``update_calls`` 가 비어 있으면 라우트가 서비스에 닿지도 않았다는 뜻이므로
       공허한 어서션을 구분할 수 있다.
    """

    def __init__(self, model: _FakeModel, pricing: _FakePricing) -> None:
        self._model = model
        self._pricing = pricing
        self.update_calls: list[dict[str, Any]] = []
        self.created_pricing: list[Any] = []
        self.closed: list[datetime] = []

    async def update_model(self, alias: str, **kwargs: Any):
        self.update_calls.append({"alias": alias, **kwargs})
        if alias != self._model.alias:
            return None
        for key, value in kwargs.items():
            if value is not None:
                setattr(self._model, key, value)
        self._model.updated_at = datetime.now(timezone.utc)
        return self._model

    async def get_by_alias(self, alias: str):
        return self._model if alias == self._model.alias else None

    async def get_current_pricing(self, alias: str):
        return self._pricing

    async def close_current_pricing(self, alias: str, effective_until: datetime) -> int:
        self.closed.append(effective_until)
        return 1

    async def create_pricing(self, pricing: Any):
        self.created_pricing.append(pricing)
        return pricing


def _mock_session(request: Request):
    """프로덕션 get_db_session 대역 — 실제 Postgres 를 절대 열지 않는다.

    ⚠️ request.state 에 세션을 매달아야 CommittingRoute(install_commit_before_response)가
       세션을 찾는다. 안 매달면 커밋 경로를 한 번도 밟지 않는 하네스가 된다
       (tests/integration/conftest.py 의 같은 함정 주석 참조).
    """
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock())
    session.get = AsyncMock(return_value=None)
    session.add = MagicMock()
    session.flush = AsyncMock()
    open_tx = {"value": True}
    session.in_transaction = MagicMock(side_effect=lambda: open_tx["value"])

    async def _close(*_a, **_k):
        open_tx["value"] = False

    session.commit = AsyncMock(side_effect=_close)
    session.rollback = AsyncMock(side_effect=_close)
    setattr(request.state, SESSION_STATE_ATTR, session)
    return session


def _build_app() -> FastAPI:
    """프로덕션과 같은 배선(422 정규화 핸들러 + 커밋 승격)으로 models 라우터만 올린다."""
    from app.routers import models as models_router

    app = FastAPI(title="Regression: model update")
    register_exception_handlers(app)
    app.include_router(models_router.router)
    install_commit_before_response(app)  # ⚠️ include_router 뒤여야 승격된다

    # ⚠️ `request: Request` 애노테이션 필수. 애노테이션을 빼면 FastAPI 가 이걸 **쿼리 파라미터**
    #    로 보고 모든 요청이 "request: Field required" 422 가 된다. 그러면 provider 422
    #    테스트는 **엉뚱한 이유로** 초록불이 되고(두 검증 오류가 한 봉투에 함께 담긴다)
    #    하네스가 죽은 걸 아무도 모른다. 위 vacuity control 이 실제로 이걸 잡아냈다.
    async def _session_override(request: Request):
        yield _mock_session(request)

    app.dependency_overrides[get_db_session] = _session_override
    # 인증은 이 결함과 무관하다. 다만 FastAPI 는 의존성을 먼저 풀기 때문에 여기서 401/403 이
    # 나면 본문 검증에 도달하지 못하고 422 테스트가 통째로 공허해진다.
    app.dependency_overrides[require_admin] = lambda: ADMIN

    mock_redis = AsyncMock()
    mock_redis.delete = AsyncMock()
    app.state.model_service = ModelService(cache_mgr=CacheInvalidationManager(mock_redis))
    return app


@pytest.fixture
def repo() -> _RecordingRepo:
    return _RecordingRepo(_FakeModel(), _FakePricing())


@pytest.fixture
async def client(repo: _RecordingRepo):
    app = _build_app()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    with patch("app.services.model_service.ModelRepository", MagicMock(return_value=repo)), \
         patch("app.services.model_service.audit_logger") as mock_audit:
        mock_audit.log = AsyncMock()
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


def _envelope(resp) -> dict:
    """프로젝트 표준 에러 봉투(main.py:258~ request_validation_handler)를 그대로 확인한다."""
    assert resp.headers["content-type"].startswith("application/json"), resp.text[:200]
    body = resp.json()
    assert "error" in body, f"정규화된 봉투가 아니다: {body}"
    err = body["error"]
    assert err["type"] == "validation_error"
    assert err["code"] == "REQUEST_VALIDATION_ERROR"
    return err


def _field_paths(err: dict) -> list[str]:
    """봉투의 fields[].loc 을 핸들러와 같은 규칙(맨 앞 'body' 제거)으로 경로화."""
    return [".".join(str(p) for p in f.get("loc", [])[1:]) for f in err["fields"]]


# ── VACUITY CONTROL ───────────────────────────────────────────────────────────


class TestHarnessActuallyRuns:
    """이 하네스가 정말 라우터 → 서비스 → 리포지토리를 통과하는지부터 증명한다.

    이게 없으면 아래 422 어서션들은 "라우트가 없어서 404" / "인증에 막혀 403" 같은
    이유로도 통과할 수 있고(422 만 보면 구분 불가), 200 어서션은 아예 성립하지 않는다.
    """

    async def test_legit_put_reaches_repository_and_persists(
        self, client: AsyncClient, repo: _RecordingRepo
    ):
        resp = await client.put(
            f"/admin/models/{ALIAS}",
            json={
                "provider_model_id": "anthropic.claude-sonnet-4-20250514-v1:0",
                "endpoint_url": "http://vllm.internal:8000/v1",
                "description": "after",
                "display_name": "After",
            },
        )

        assert resp.status_code == 200, resp.text[:400]
        assert repo.update_calls, (
            "리포지토리가 호출되지 않았다 — 라우트/서비스 배선이 끊어졌고 이 파일의 "
            "나머지 어서션은 공허하다"
        )
        sent = repo.update_calls[0]
        assert sent["alias"] == ALIAS
        assert sent["provider_model_id"] == "anthropic.claude-sonnet-4-20250514-v1:0"
        # 응답 본문(= DB 행 상태)에 실제로 반영됐는지까지 본다.
        body = resp.json()
        assert body["provider_model_id"] == "anthropic.claude-sonnet-4-20250514-v1:0"
        assert body["endpoint_url"] == "http://vllm.internal:8000/v1"
        assert body["description"] == "after"
        assert body["display_name"] == "After"

    async def test_unknown_alias_still_404(self, client: AsyncClient):
        """422 가 "본문 검증" 때문임을 구분하려면 다른 실패 모드가 살아 있어야 한다."""
        resp = await client.put(
            "/admin/models/no-such-alias", json={"description": "x"}
        )
        assert resp.status_code == 404, resp.text[:300]


# ── 결함 ①: provider / api_format 조용한 유실 ────────────────────────────────


class TestImmutableFieldsRejected:
    async def test_put_with_provider_is_422_and_persists_nothing(
        self, client: AsyncClient, repo: _RecordingRepo
    ):
        """예전에는 200 + provider 무시(반쪽 마이그레이션). 지금은 422 이고 쓰기도 없어야 한다."""
        resp = await client.put(
            f"/admin/models/{ALIAS}",
            json={
                "provider_model_id": "openai.gpt-5.6-sol",
                "endpoint_url": "https://bedrock-runtime.us-east-2.amazonaws.com/openai",
                # ⚠️ 여기가 핵심 — 예전에는 조용히 버려졌다.
                "provider": "BEDROCK_RUNTIME_OPENAI",
            },
        )

        assert resp.status_code == 422, (
            f"provider 를 보냈는데 {resp.status_code} — 조용히 버려졌다: {resp.text[:400]}"
        )
        err = _envelope(resp)
        assert "provider" in err["message"], err["message"]
        assert "provider" in _field_paths(err), _field_paths(err)
        # 가장 중요한 부분: 반쪽 상태가 남지 않는다(provider_model_id 도 쓰이지 않았다).
        assert repo.update_calls == [], (
            "422 인데 리포지토리가 호출됐다 — 새 provider_model_id 만 반영된 반쪽 행이 남는다"
        )

    async def test_put_with_api_format_is_422(self, client: AsyncClient, repo: _RecordingRepo):
        resp = await client.put(
            f"/admin/models/{ALIAS}",
            json={"description": "x", "api_format": "OPENAI_RESPONSES"},
        )
        assert resp.status_code == 422, resp.text[:400]
        err = _envelope(resp)
        assert "api_format" in _field_paths(err), _field_paths(err)
        assert repo.update_calls == []

    async def test_error_names_the_offending_key_not_a_generic_message(
        self, client: AsyncClient
    ):
        """UI 는 fieldErrors 키로 어느 입력란인지 찾는다(CreateModelDialog 의 orphanKeys 처리).

        봉투가 "Request validation failed" 같은 총칭이면 운영자는 원인을 알 수 없다.
        """
        resp = await client.put(
            f"/admin/models/{ALIAS}", json={"description": "x", "totally_unknown": 1}
        )
        assert resp.status_code == 422
        err = _envelope(resp)
        assert "totally_unknown" in err["message"]
        assert "totally_unknown" in _field_paths(err)


# ── 결함 ②: NUMERIC(10,6) 오버플로 → 500 대신 422 ─────────────────────────────


class TestPriceUpperBound:
    async def test_bound_matches_the_column(self):
        """상한이 컬럼(NUMERIC(10,6))에서 유도된 값인지 못 박는다 — 임의 상수 표류 방지."""
        assert MAX_PRICE_PER_1K == Decimal("9999.999999")

    async def test_pricing_overflow_is_422_not_500(
        self, client: AsyncClient, repo: _RecordingRepo
    ):
        resp = await client.put(
            f"/admin/models/{ALIAS}/pricing",
            json={
                "input_price_per_1k_tokens": "12345.678900",
                "output_price_per_1k_tokens": "0.015",
                "effective_from": "2026-05-01T00:00:00Z",
            },
        )
        assert resp.status_code == 422, (
            f"NUMERIC(10,6) 를 넘는 단가인데 {resp.status_code} — asyncpg "
            f"NumericValueOutOfRange 가 맨 500 으로 새 나간다: {resp.text[:400]}"
        )
        err = _envelope(resp)
        assert "input_price_per_1k_tokens" in _field_paths(err), _field_paths(err)
        assert repo.created_pricing == [], "422 인데 단가 행을 썼다"
        assert repo.closed == [], "422 인데 기존 단가를 닫았다(시계열 훼손)"

    async def test_ten_thousand_with_five_decimals_is_also_rejected(
        self, client: AsyncClient
    ):
        """소수 자리 수는 합법인데 정수부만 넘치는 값(10000.00000)도 거부돼야 한다.

        기존 검증(``decimal_places=6``)은 소수 자리만 보므로 이 값을 통과시켰고 PG 는
        NUMERIC(10,6) 스케일로 10000.000000(11자리)을 저장하려다 오버플로했다.

        ⚠️ 정직한 한계: 이 테스트는 상한 **구현 방식**을 구분하지 못한다. ``le`` 를
        ``max_digits=10`` 으로 바꿔도 통과한다(pydantic 2.13.2 는 max_digits-decimal_places
        를 정수부 상한으로 환산한다 — 실측). 여기서 고정하는 것은 "정수부 오버플로는
        422" 라는 **동작**이고, le 를 고른 이유는 스키마 주석에 적어 두었다.
        """
        resp = await client.put(
            f"/admin/models/{ALIAS}/pricing",
            json={
                "input_price_per_1k_tokens": "10000.00000",
                "output_price_per_1k_tokens": "0.015",
                "effective_from": "2026-05-01T00:00:00Z",
            },
        )
        assert resp.status_code == 422, resp.text[:400]

    async def test_boundary_value_still_accepted(
        self, client: AsyncClient, repo: _RecordingRepo
    ):
        """상한이 정당한 입력을 막지 않는지(과잉 차단 아님) — 결함 ② 쪽 vacuity control."""
        resp = await client.put(
            f"/admin/models/{ALIAS}/pricing",
            json={
                "input_price_per_1k_tokens": str(MAX_PRICE_PER_1K),
                "output_price_per_1k_tokens": "0.015",
                "effective_from": "2026-05-01T00:00:00Z",
            },
        )
        assert resp.status_code == 200, resp.text[:400]
        assert len(repo.created_pricing) == 1, "경계값이 통과했는데 단가 행이 쓰이지 않았다"
        assert repo.created_pricing[0].input_price_per_1k_tokens == MAX_PRICE_PER_1K

    async def test_create_model_overflow_is_422_not_500(self, client: AsyncClient):
        """POST 경로(ModelCreateRequest)도 같은 상한을 갖는다."""
        resp = await client.post(
            "/admin/models",
            json={
                "alias": "overflow-model",
                "provider": "BEDROCK",
                "provider_model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
                "api_format": "BEDROCK_NATIVE",
                "input_price_per_1k_tokens": "0.003",
                "output_price_per_1k_tokens": "9999999.999999",
            },
        )
        assert resp.status_code == 422, resp.text[:400]
        assert "output_price_per_1k_tokens" in _field_paths(_envelope(resp))


class TestPricingSilentDropIsMoneyLoss:
    """⚠️ 같은 조용한 유실이 **가격** 엔드포인트에도 있었다 — 그쪽은 돈이 걸린다.

    ``PricingRequest`` 의 캐시 단가 3개는 default 가 ``Decimal("0")`` 이다. 키 이름이 틀리면
    값이 버려지고 0 이 들어간다. 게다가 이 엔드포인트는 쓰기 전에 **직전 단가 행을 close**
    하므로, 새 ACTIVE 행이 캐시 생성 비용을 0 으로 청구하게 되고 되돌아갈 행도 없다.

    적대적 검증 실측(PROBE2): 0003 이전 이름 ``cache_creation_price_per_1k_tokens`` 로 보내면
    **200**, 기록된 행은 5m=0 · 1h=0, 직전 행은 이미 닫힘. 즉 조용한 오과금.
    """

    # 0003_rename_cache_5m 이전의 실제 필드명 — 문서/스크립트에 아직 남아 있을 수 있어
    # 오타보다 훨씬 그럴듯한 실수다.
    _PRE_0003_NAME = "cache_creation_price_per_1k_tokens"

    def _body(self, **overrides) -> dict:
        body = {
            "input_price_per_1k_tokens": "0.003",
            "output_price_per_1k_tokens": "0.015",
            "cache_read_price_per_1k_tokens": "0.0003",
            "effective_from": datetime.now(timezone.utc).isoformat(),
        }
        body.update(overrides)
        return body

    async def test_harness_writes_a_pricing_row_on_a_legit_put(
        self, client: AsyncClient, repo: _RecordingRepo
    ):
        """공허성 대조군 — 정상 요청이 실제로 단가 행을 쓰는지부터 확인한다."""
        resp = await client.put(
            "/admin/models/claude-sonnet-regression/pricing",
            json=self._body(cache_creation_5m_price_per_1k_tokens="0.00375"),
        )
        assert resp.status_code == 200, resp.text[:400]
        assert len(repo.created_pricing) == 1, "정상 요청인데 단가 행이 쓰이지 않았다"
        assert repo.created_pricing[0].cache_creation_5m_price_per_1k_tokens == Decimal("0.00375")

    async def test_pre_0003_cache_key_is_422_not_a_silent_zero(
        self, client: AsyncClient, repo: _RecordingRepo
    ):
        """옛 필드명 → 422. 예전엔 200 + 캐시단가 0 이었다."""
        resp = await client.put(
            "/admin/models/claude-sonnet-regression/pricing",
            json=self._body(**{self._PRE_0003_NAME: "0.00375"}),
        )
        assert resp.status_code == 422, (
            f"옛 캐시 단가 필드명이 조용히 버려졌다 — 새 ACTIVE 단가 행이 캐시 생성 비용을 "
            f"0 으로 청구한다: {resp.status_code} {resp.text[:300]}"
        )
        assert self._PRE_0003_NAME in _field_paths(_envelope(resp))

    async def test_nothing_is_written_and_nothing_is_closed_on_reject(
        self, client: AsyncClient, repo: _RecordingRepo
    ):
        """⚠️ 핵심 — 거부 시 직전 단가 행이 닫히지 않아야 한다.

        닫혀버리면 되돌아갈 ACTIVE 행이 없어져서 '거부'가 오히려 상태를 망친다.
        """
        await client.put(
            "/admin/models/claude-sonnet-regression/pricing",
            json=self._body(**{self._PRE_0003_NAME: "0.00375"}),
        )
        assert repo.created_pricing == [], "422 인데 단가 행이 쓰였다"
        assert repo.closed == [], "422 인데 직전 단가 행을 닫았다 — ACTIVE 단가가 사라진다"

    async def test_typo_on_create_is_422_not_a_201_with_a_broken_row(
        self, client: AsyncClient
    ):
        """POST 경로 — 오타 키가 201 과 함께 런타임에만 깨지는 행을 만들던 것.

        실측(PROBE3): ``endpoint_ur1`` → endpoint_url NULL 인 BEDROCK_RUNTIME_OPENAI 행이
        201 로 생성됐다. 그 어댑터는 endpoint_url 없이 서명 리전도 유도할 수 없다.
        """
        resp = await client.post(
            "/admin/models",
            json={
                "alias": "typo-model",
                "provider": "BEDROCK_RUNTIME_OPENAI",
                "provider_model_id": "openai.gpt-5.6-sol",
                "api_format": "OPENAI_RESPONSES",
                "endpoint_ur1": "https://bedrock-runtime.us-east-2.amazonaws.com/openai",
                "input_price_per_1k_tokens": "0.003",
                "output_price_per_1k_tokens": "0.015",
            },
        )
        assert resp.status_code == 422, (
            f"오타 키가 조용히 버려져 endpoint_url NULL 행이 201 로 생성된다: "
            f"{resp.status_code} {resp.text[:300]}"
        )
        assert "endpoint_ur1" in _field_paths(_envelope(resp))

    async def test_camel_case_display_name_is_also_caught(self, client: AsyncClient):
        """대조군 — camelCase 실수도 같은 경로로 잡힌다(선언된 키만 허용)."""
        resp = await client.post(
            "/admin/models",
            json={
                "alias": "camel-model",
                "provider": "BEDROCK",
                "provider_model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
                "api_format": "BEDROCK_NATIVE",
                "displayName": "GPT 5.6",
                "input_price_per_1k_tokens": "0.003",
                "output_price_per_1k_tokens": "0.015",
            },
        )
        assert resp.status_code == 422, resp.text[:300]
        assert "displayName" in _field_paths(_envelope(resp))
