# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""웹서치 분기가 **입장 심사를 건너뛰지 않는지**.

무엇이 문제였나
---------------
``/v1/messages`` 에서 이 세 검사는 오직 ``run_fallback_loop`` 안에만 있었다:

    check_key_scope           사용자 × 모델 허용목록
    check_client_model_scope  앱 × 모델 허용목록 (migration 0035)
    enforce_rate_limits       RPM / TPM / 비용 한도

웹서치 분기는 그 폴백 루프보다 **먼저 리턴**한다. 그래서 ``web_search_enabled`` 프로파일의
요청은 세 검사를 전부 우회했다 — 접근권 없는 모델이 호출되고, 한도를 넘겨도 429 가 나지
않았다. 프로파일 플래그가 켜진 클라이언트에서만 그랬으므로 다른 경로의 테스트로는 드러나지
않았고, 우회 자체는 아무 오류도 남기지 않는다.

왜 구조 검사만으로는 부족한가
-----------------------------
바로 앞에 같은 부류의 사고가 있었다: 모델 × 앱 게이트가 ORM 컬럼 누락으로 무력화됐는데,
``_orm_to_schema`` 의 kwarg 이름만 AST 로 확인하는 테스트가 **통과**하고 있었다. 이름이
있는 것과 값이 흐르는 것은 다르다. 그래서 이 파일은 ``messages()`` 를 **실제로 호출**해서
응답 코드와 "웹서치 루프가 불렸는지" 를 본다. 구조 검사는 보조로만 둔다.
"""

from __future__ import annotations

import ast
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.schemas.domain import (
    ApiFormat,
    AuthContext,
    AuthType,
    ModelConfigSchema,
    ModelPricingSchema,
    ModelStatus,
    ProviderType,
    Role,
)
from app.services.fallback_loop import (
    AdmissionRejection,
    enforce_candidate_admission,
    scope_denial_body,
)
from app.services.router_service import RouterService

_SRC = Path(__file__).resolve().parents[2] / "src" / "app"


def _pricing() -> ModelPricingSchema:
    return ModelPricingSchema(
        input_per_1k=Decimal("0.003"),
        output_per_1k=Decimal("0.015"),
    )


def _config(alias: str = "claude-opus", allowed_clients=None) -> ModelConfigSchema:
    return ModelConfigSchema(
        provider_model_id="anthropic.claude-opus",
        alias=alias,
        provider=ProviderType.BEDROCK,
        api_format=ApiFormat.ANTHROPIC_MESSAGES,
        pricing=_pricing(),
        status=ModelStatus.ACTIVE,
        created_at=datetime(2026, 1, 1),
        allowed_clients=allowed_clients,
    )


def _auth(allowed_models=None) -> AuthContext:
    return AuthContext(
        user_id="00000000-0000-0000-0000-000000000001",
        team_id="00000000-0000-0000-0000-000000000002",
        dept_id="00000000-0000-0000-0000-000000000003",
        roles=[Role.DEVELOPER],
        auth_type=AuthType.VIRTUAL_KEY,
        key_id="k1",
        allowed_models=allowed_models,
        sso_subject="sub-1",
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. 헬퍼의 동작
# ─────────────────────────────────────────────────────────────────────────────


async def _admit(*, auth, config, client, rl_reject=None, monkeypatch=None):
    if monkeypatch is not None:
        async def _fake_rl(**kwargs):
            return rl_reject

        monkeypatch.setattr(
            "app.services.fallback_loop.enforce_rate_limits", _fake_rl, raising=True
        )
    state: dict = {"client": client}
    return await enforce_candidate_admission(
        router_service=RouterService(),
        auth_context=auth,
        candidate_config=config,
        redis=None,
        req_data={"model": config.alias, "messages": []},
        state=state,
        request_id="req-1",
    )


async def test_key_scope_denial_is_a_400(monkeypatch):
    """계정에 없는 모델 → 400.

    ⚠️ 403 이 아니다. Anthropic 클라이언트들은 403 을 자격증명 문제로 읽고 재인증
       루프를 돈다 — 모델 접근권 문제는 재시도로 풀리지 않으므로 무한히 돈다.
    """
    r = await _admit(
        auth=_auth(allowed_models=["some-other-model"]),
        config=_config(),
        client="claude-code",
        monkeypatch=monkeypatch,
    )
    assert isinstance(r, AdmissionRejection)
    assert r.status == 400
    body = json.loads(r.body)
    assert body["error"]["type"] == "invalid_request_error"
    assert "your account does not have access" in body["error"]["message"].lower()


async def test_client_model_scope_denial_is_a_400_with_a_different_message(monkeypatch):
    """앱 축 거부는 계정 축과 **다른** 안내여야 한다.

    합치면 Codex 에서만 막힌 사용자가 계정 권한을 요청하러 가고, 운영자는 이미 권한이
    있다고 답한다 — 양쪽 다 맞는 말인데 아무도 원인을 못 찾는다.
    """
    r = await _admit(
        auth=_auth(),  # 계정 축은 제한 없음
        config=_config(allowed_clients=["claude-code"]),
        client="codex",
        monkeypatch=monkeypatch,
    )
    assert r is not None and r.status == 400
    msg = json.loads(r.body)["error"]["message"].lower()
    assert "this application" in msg or "this app" in msg
    assert "your account does not have access" not in msg


def test_the_two_denial_messages_are_not_identical():
    """대조군 — 위 두 단정이 "아무 문구나 통과" 가 아님을 보인다."""
    cfg = _config()
    account = json.loads(scope_denial_body(cfg, client_scoped=False))["error"]["message"]
    app = json.loads(scope_denial_body(cfg, client_scoped=True))["error"]["message"]
    assert account != app
    assert cfg.alias in account and cfg.alias in app


async def test_empty_allow_list_denies_every_app(monkeypatch):
    """``[]`` = 명시적 전면 거부. 운영자가 마지막 앱의 체크를 해제하면 나오는 값이다."""
    for client in ("claude-code", "codex", "cowork", None):
        r = await _admit(
            auth=_auth(),
            config=_config(allowed_clients=[]),
            client=client,
            monkeypatch=monkeypatch,
        )
        assert r is not None and r.status == 400, f"client={client!r} 가 통과했다"


async def test_rate_limit_rejection_is_propagated_verbatim(monkeypatch):
    """429 의 상태·본문·헤더가 그대로 나가야 한다 — Retry-After 를 잃으면 안 된다."""
    rejected = SimpleNamespace(
        status_code=429,
        body=json.dumps({"error": {"type": "rate_limit_error", "message": "TPM"}}).encode(),
        headers={"retry-after": "37"},
    )
    r = await _admit(
        auth=_auth(),
        config=_config(),
        client="claude-code",
        rl_reject=rejected,
        monkeypatch=monkeypatch,
    )
    assert r is not None and r.status == 429
    assert json.loads(r.body)["error"]["type"] == "rate_limit_error"
    assert r.headers.get("retry-after") == "37"


async def test_everything_allowed_returns_none(monkeypatch):
    """대조군 — 위 단정들이 "항상 거절" 이 아님을 보인다."""
    r = await _admit(
        auth=_auth(),
        config=_config(allowed_clients=["claude-code"]),
        client="claude-code",
        monkeypatch=monkeypatch,
    )
    assert r is None


async def test_no_auth_context_is_admitted(monkeypatch):
    """인증 컨텍스트가 없는 내부 호출은 기존 동작대로 통과한다.

    이 함수가 인증을 대체하지는 않는다 — 인증은 미들웨어의 책임이다.
    """
    r = await _admit(auth=None, config=_config(allowed_clients=[]), client="codex",
                     monkeypatch=monkeypatch)
    assert r is None


# ─────────────────────────────────────────────────────────────────────────────
# 2. messages() 를 실제로 호출한다 — 배선이 살아 있는지
# ─────────────────────────────────────────────────────────────────────────────


class _FakeRequest:
    """messages() 가 실제로 읽는 것만 갖춘 Request 대역."""

    def __init__(self, body: dict, state: dict, app_state):
        self._body = json.dumps(body).encode()
        self.scope = {"state": state}
        self.app = SimpleNamespace(state=app_state)
        self.headers = {}

    async def body(self) -> bytes:
        return self._body

    async def is_disconnected(self) -> bool:
        return False


class _FakeRouter(RouterService):
    """DB 접근만 대역화한다.

    ⚠️ ``check_key_scope`` / ``check_client_model_scope`` 는 **상속받은 진짜 구현**을
       쓴다. 게이트까지 대역화하면 이 파일이 검증하려는 것 자체가 사라진다.
    """

    def __init__(self, config: ModelConfigSchema):
        self._config = config

    async def alias_provider(self, redis, db, alias):
        return ProviderType.BEDROCK

    async def resolve_bedrock_model(self, redis, db, alias):
        return self._config


def _app_state(*, web_search: bool):
    profile = SimpleNamespace(
        backend="bedrock",
        default_model=None,
        web_search_enabled=web_search,
        region=None,
        account_role_arn=None,
    )
    loader = MagicMock()
    loader.load = AsyncMock(return_value=profile)
    st = SimpleNamespace()
    st.provider_registry = MagicMock()
    st.cost_recorder = MagicMock()
    st.routing_profile_loader = loader
    # 웹서치 분기의 두 번째 조건. None 이면 분기 자체가 안 돌아 이 검사가 공허해진다.
    st.agentcore_mcp_client = MagicMock() if web_search else None
    st.body_logger = None
    st.circuit_breaker = None
    st.fallback_resolver = None
    st.alias_provider_map = {}
    st.tokenizer = None
    return st


async def _call_messages(*, monkeypatch, auth, config, client, web_search=True, rl_reject=None):
    """messages() 를 호출하고 (응답, 웹서치루프 호출여부) 를 돌려준다."""
    import app.routers.messages as messages_mod
    import app.services.web_search_loop as wsl_mod

    monkeypatch.setattr(messages_mod, "_router_service", _FakeRouter(config))

    called: list[bool] = []

    async def _fake_loop(**kwargs):
        called.append(True)
        return MagicMock(status_code=200)

    monkeypatch.setattr(wsl_mod, "run_web_search_loop", _fake_loop, raising=True)

    async def _fake_rl(**kwargs):
        return rl_reject

    monkeypatch.setattr(
        "app.services.fallback_loop.enforce_rate_limits", _fake_rl, raising=True
    )

    # 폴백 경로로 새면(= 웹서치 분기를 못 탐) 곧바로 드러나게 한다.
    async def _boom(**kwargs):
        raise AssertionError("run_fallback_loop 로 갔다 — 웹서치 분기를 타지 않았다")

    monkeypatch.setattr(messages_mod, "run_fallback_loop", _boom, raising=True)

    state = {
        "auth_context": auth,
        "client": client,
        "_redis": None,
        "_session_factory": None,
        "request_id": "req-1",
    }
    req = _FakeRequest({"model": config.alias, "messages": [], "max_tokens": 16},
                       state, _app_state(web_search=web_search))
    resp = await messages_mod.messages(req)
    return resp, bool(called)


async def test_websearch_path_enforces_the_key_scope(monkeypatch):
    """⚠️ 이 파일의 핵심. 계정에 없는 모델이 웹서치 경로로 호출되면 안 된다."""
    resp, loop_called = await _call_messages(
        monkeypatch=monkeypatch,
        auth=_auth(allowed_models=["another-model"]),
        config=_config(),
        client="claude-code",
    )
    assert resp.status_code == 400, f"{resp.status_code} — 접근권 없는 모델이 통과했다"
    assert not loop_called, "웹서치 루프가 불렸다 — 상류를 호출했다는 뜻이다"


async def test_websearch_path_enforces_the_app_scope(monkeypatch):
    """앱 × 모델 허용목록(migration 0035)도 이 경로에서 걸려야 한다."""
    resp, loop_called = await _call_messages(
        monkeypatch=monkeypatch,
        auth=_auth(),
        config=_config(allowed_clients=["claude-code"]),
        client="codex",
    )
    assert resp.status_code == 400
    assert not loop_called
    msg = json.loads(bytes(resp.body))["error"]["message"].lower()
    assert "this application" in msg or "this app" in msg


async def test_websearch_path_enforces_rate_limits(monkeypatch):
    """한도를 넘으면 429 다. 우회되면 웹서치 프로파일이 무한 예산이 된다."""
    rejected = SimpleNamespace(
        status_code=429,
        body=json.dumps({"error": {"type": "rate_limit_error", "message": "RPM"}}).encode(),
        headers={},
    )
    resp, loop_called = await _call_messages(
        monkeypatch=monkeypatch,
        auth=_auth(),
        config=_config(),
        client="claude-code",
        rl_reject=rejected,
    )
    assert resp.status_code == 429
    assert not loop_called


async def test_websearch_path_still_runs_when_admitted(monkeypatch):
    """⚠️ 대조군. 위 셋이 "웹서치가 항상 막힌다" 로 통과하는 것이 아님을 보인다.

    이것이 없으면 입장 심사를 무조건 거절로 만들어도 위 세 테스트가 통과한다.
    """
    resp, loop_called = await _call_messages(
        monkeypatch=monkeypatch,
        auth=_auth(),
        config=_config(allowed_clients=["claude-code"]),
        client="claude-code",
    )
    assert loop_called, "허용된 요청인데 웹서치 루프가 불리지 않았다"
    assert resp.status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
# 3. 구조 — 두 경로가 같은 구현을 쓰는지(드리프트 방지)
# ─────────────────────────────────────────────────────────────────────────────


def _tree(rel: str) -> ast.Module:
    src = (_SRC / rel).read_text(encoding="utf-8")
    assert len(src) > 2000, f"{rel} 가 너무 짧다 — 경로 확인"
    return ast.parse(src)


def _func(tree: ast.Module, name: str):
    fn = next(
        (
            n
            for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
        ),
        None,
    )
    assert fn is not None, f"{name} 를 찾지 못했다"
    return fn


def _call_lines(node, name: str) -> list[int]:
    return [
        n.lineno
        for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name
    ]


def test_admission_runs_before_the_web_search_loop():
    fn = _func(_tree("routers/messages.py"), "messages")
    admit = _call_lines(fn, "enforce_candidate_admission")
    loop = _call_lines(fn, "run_web_search_loop")
    assert admit, "messages() 가 입장 심사를 부르지 않는다"
    assert loop, "run_web_search_loop 호출을 찾지 못했다 — 이 검사의 전제가 깨졌다"
    assert min(admit) < min(loop), (
        f"입장 심사(L{admit})가 웹서치 루프(L{loop}) 뒤에 있다 — 상류를 먼저 부른다"
    )


def test_the_fallback_loop_uses_the_same_helper():
    """폴백 루프가 검사를 인라인으로 되돌리면 두 경로가 갈라진다.

    한쪽만 고친 정책 변경이 다른 쪽에 반영되지 않는 것이 이 결함의 원인이었다.
    """
    tree = _tree("services/fallback_loop.py")
    fn = _func(tree, "run_fallback_loop")
    assert _call_lines(fn, "enforce_candidate_admission"), (
        "run_fallback_loop 이 공용 헬퍼를 쓰지 않는다"
    )
    # 루프 본문에 인라인 검사가 되살아나지 않았는지.
    inline = [
        n.lineno
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in ("check_key_scope", "check_client_model_scope")
    ]
    assert inline == [], f"L{inline}: 스코프 검사가 루프 안에 인라인으로 돌아왔다"


def test_client_model_scope_raises_the_distinguishable_error():
    """구별 가능한 예외가 없으면 두 축의 안내를 나눌 수 없다."""
    from app.services.router_service import ClientModelScopeError, check_client_model_scope

    assert issubclass(ClientModelScopeError, PermissionError), (
        "PermissionError 하위가 아니면 기존 except 절들이 못 잡아 500 이 된다"
    )
    with pytest.raises(ClientModelScopeError):
        check_client_model_scope(_config(allowed_clients=["claude-code"]), "codex")
    # 계정 축은 이 타입을 쓰지 않는다(그래야 문구가 갈린다).
    rs = RouterService()
    with pytest.raises(PermissionError) as ei:
        rs.check_key_scope(_auth(allowed_models=["other"]), _config())
    assert not isinstance(ei.value, ClientModelScopeError)
