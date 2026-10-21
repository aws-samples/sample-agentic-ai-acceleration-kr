# Copyright 2026 © Amazon.com and Affiliates.

"""웹서치 **비스트리밍** 경로가 첫 턴 실패에서도 예약을 정확히 한 번 정산하는지.

무엇이 문제였나
---------------
``enforce_rate_limits`` 는 요청을 받아들일 때 TPM 과 비용(CPM/CPH)을 미리 깎는다(예약).
웹서치 분기(``/v1/responses`` · ``/v1/messages``)에서 그 예약을 정산하는 곳은 루프가 쏘는
``on_usage`` → 라우터의 ``_ws_record`` → ``CostRecorder.finalize`` **하나뿐**이다. 라우터의
웹서치 분기는 ``release_reservations`` 를 부르지 않는다.

비스트리밍 출구 넷 — 두 루프(``_anthropic_nonstream`` · ``_responses_nonstream``)와 두
패스스루(F-7 클라이언트 자체 ``web_search`` 도구, AgentCore 핸드셰이크 실패 폴백) — 는 토큰이
0 이면 ``on_usage`` 를 건너뛰었다. 그래서 **첫 턴이** 4xx/5xx 면 아무도 정산하지 않아 예약이
창이 끝날 때까지 남았다(CPM 1분, CPH 최대 1시간). 스트리밍 스티처는 항상 쏘므로 영향이 없었다.

main 에서도 첫 턴이 4xx/5xx 면 같다. alias 계정(0043)은 그 트리거를 **지속적으로** 만든다 —
AssumeRole 이 실패하면 그 alias 의 모든 요청이 500 이다. CPM/CPH 는 사용자 단위라, 잘못 설정된
alias 하나에 클라이언트 재시도가 겹치면 그 사용자의 **다른** 모델까지 CPH 429 로 최대 1시간
막힌다.

이 파일
-------
실 Redis 와 실 Lua 로 예약한다(``enforce_rate_limits`` 그대로, 한도 로더만 대역). 실 라우터 ·
실 웹서치 루프 · 실 어댑터 · 실 ``SigV4Signer``/``BedrockAccountClientProvider`` · 실
``CostRecorder.finalize`` 를 탄다. 대역은 STS · 상류(HTTP·boto3) · AgentCore MCP · 파드 세션 ·
모델 해석 · 프로필 로더 · Request 이고, 핸들러는 미들웨어 없이 직접 부른다.

  (a) alias 계정 AssumeRole 실패 → 500. 카운터는 정확히 0.0, 원장 행은 없다
      (zero-usage finalize 는 XADD 전에 return 한다). 비스트리밍 출구 셋 × 두 라우트.
      스트리밍 경로의 같은 실패도 세 출구 모두 같은 정산이다. HTTP 는 도구 루프만 200 +
      ``event: error`` 이고, 두 패스스루는 어댑터의 500 을 그대로 쓴다.
  (b) 1턴 성공(usage) 뒤 2턴 실패 → 한 번만 정산, 원장에 1턴 usage, 음수 없음.
  (c) 성공 경로 → 한 번 정산, 카운터 = 실제 비용·토큰, 원장 한 행.

팀 한도도 건다. 두 스코프를 모두 예약하게 해야 "정확히 한 번" 을 두 스코프에서 볼 수 있다.
(정산은 예약한 스코프만 조정한다. 예전 정산은 팀 비용 한도가 없어 예약하지 않은 팀 CPM/CPH
카운터도 움직여 (실제 비용 − 예약액)만큼 음수·TTL 없는 키를 남겼다(실측: 환불 −0.013237, 성공
−0.012027) — ``test_medium_cost_settle_reserved_scope.py``.)

⚠️ 실 Redis 가 필요하다(``REDIS_PROOF_URL``). 정산은 incrbyfloat 로 카운터를 움직이는 것이라
   호출 여부만 보는 가짜로는 부호가 반대인 환불도 통과한다.

실행:
    REDIS_PROOF_URL=redis://127.0.0.1:56379/0 \\
      pytest tests/regression/test_medium_websearch_nonstream_reservation_release.py
"""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import json
import os
import time
import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from botocore.credentials import Credentials
from botocore.exceptions import ClientError
from starlette.datastructures import Headers

import app
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
from app.services.account_credentials import ACCOUNT_UNAVAILABLE_MESSAGE
from app.services.cost_recorder import CostRecorder

_SRC = Path(__file__).resolve().parents[2] / "src" / "app"
#: 테스트하는 패키지 자신의 Lua(대조군을 다른 트리로 돌리면 그 트리의 스크립트를 쓴다).
_SCRIPTS = Path(app.__file__).resolve().parent / "redis_scripts"
REDIS_PROOF_URL = os.environ.get("REDIS_PROOF_URL")

_redis_required = pytest.mark.skipif(
    not REDIS_PROOF_URL,
    reason=(
        "REDIS_PROOF_URL 미설정 — 실 Redis 필요. 정산은 incrbyfloat 로 카운터를 움직이는 것이라, "
        "호출 여부만 보는 가짜로는 부호가 반대인 환불도 통과한다."
    ),
)

ALIAS_ROLE = "arn:aws:iam::111122223333:role/in-region-bedrock"
RUNTIME_ENDPOINT = "https://bedrock-runtime.us-east-2.amazonaws.com/openai"
#: 상류 사용량 (input, output) — 1턴(검색 요청 또는 단일 턴 답), 2턴(최종 답).
TURN1 = (100, 20)
TURN2 = (150, 30)
#: 단가($/1K). 기대 비용을 calculate_cost 를 쓰지 않고 손으로 계산하려고 상수로 둔다.
_PRICE = {
    "responses": (Decimal("0.0022"), Decimal("0.0132")),
    "messages": (Decimal("0.003"), Decimal("0.015")),
}


def _cost(route: str, tokens: tuple[int, int]) -> Decimal:
    inp, out = _PRICE[route]
    return (Decimal(tokens[0]) * inp + Decimal(tokens[1]) * out) / 1000


def _config(route: str, *, account: bool) -> ModelConfigSchema:
    acct = {"account_role_arn": ALIAS_ROLE, "account_external_id": "alias-ext"} if account else {}
    inp, out = _PRICE[route]
    pricing = ModelPricingSchema(input_per_1k=inp, output_per_1k=out)
    if route == "responses":
        return ModelConfigSchema(
            provider_model_id="us.openai.gpt-5.6-terra",
            alias="gpt-5.6-terra",
            provider=ProviderType.BEDROCK_RUNTIME_OPENAI,
            api_format=ApiFormat.OPENAI_RESPONSES,
            endpoint=RUNTIME_ENDPOINT,
            pricing=pricing,
            status=ModelStatus.ACTIVE,
            **acct,
        )
    return ModelConfigSchema(
        provider_model_id="anthropic.claude-sonnet-4-6",
        alias="claude-sonnet-4-6",
        provider=ProviderType.BEDROCK,
        api_format=ApiFormat.ANTHROPIC_MESSAGES,
        pricing=pricing,
        status=ModelStatus.ACTIVE,
        created_at=dt.datetime(2026, 1, 1),
        **acct,
    )


async def _limits(**_kwargs):
    from app.services.rate_limit_config_loader import AllScopeLimits, ScopeLimits

    def _scope():
        return ScopeLimits(rpm=1000, tpm=1_000_000, cpm=Decimal("100"), cph=Decimal("1000"))

    return AllScopeLimits(user=_scope(), team=_scope(), global_=ScopeLimits())


def _counter_keys(auth: AuthContext, alias: str) -> dict[str, str]:
    from app.services.rate_limit_scope import RateLimitScope, build_tpm_key_group

    now = int(time.time())
    keys: dict[str, str] = {}
    for name, sid, scope in (
        ("user", auth.user_id, RateLimitScope.USER),
        ("team", auth.team_id, RateLimitScope.TEAM),
    ):
        keys[f"{name}_cpm"] = f"rl:cost:{name}:{{{sid}}}:cpm:{(now // 60) * 60}"
        keys[f"{name}_cph"] = f"rl:cost:{name}:{{{sid}}}:cph:{(now // 3600) * 3600}"
        keys[f"{name}_tpm"] = build_tpm_key_group(scope, sid, alias)[0]
    return keys


async def _read(r, keys: dict[str, str]) -> dict[str, float]:
    out = {}
    for name, key in keys.items():
        raw = await r.get(key)
        out[name] = float(raw) if raw is not None else 0.0
    return out


async def _clear_of_window_edge() -> None:
    """CPM 키는 분 단위 창이다 — 예약과 정산 사이에 분이 바뀌면 둘이 다른 키에 떨어진다."""
    while time.time() % 60 > 58:
        await asyncio.sleep(0.2)


def _async_return(value):
    async def _fn(*_a, **_k):
        return value

    return _fn


class _RouteRequest:
    def __init__(self, body: dict, state: dict, app_state) -> None:
        self._body = json.dumps(body).encode()
        self.scope = {"state": state}
        self.app = SimpleNamespace(state=app_state)
        self.headers = Headers()

    async def body(self) -> bytes:
        return self._body

    async def is_disconnected(self) -> bool:
        return False


class _Mcp:
    """AgentCore MCP 대역. ``down`` 이면 핸드셰이크가 실패한다(검색 없는 폴백 경로)."""

    def __init__(self, *, down: bool) -> None:
        self.down = down
        self.searches: list[str] = []

    async def ensure_initialized(self) -> str:
        if self.down:
            raise RuntimeError("AgentCore handshake failed")
        return "web-search-tool___WebSearch"

    async def search(self, query: str, max_results: int = 10):
        from app.services.agentcore_mcp_client import WebSearchResponse

        self.searches.append(query)
        return WebSearchResponse(
            results=[], raw_text='{"results":[{"text":"hit","url":"https://example.com"}]}'
        )


class _PodSession:
    def get_credentials(self):
        raw = MagicMock()
        raw.get_frozen_credentials.return_value = Credentials("AKIA_POD", "ps", "pt")
        return raw


def _failing_account_provider(calls: list[dict]):
    """alias 계정 AssumeRole 이 AccessDenied 로 실패하는 실 BedrockAccountClientProvider."""
    from app.services.bedrock_account_client import BedrockAccountClientProvider

    def _assume(**kw):
        calls.append(kw)
        raise ClientError(
            {"Error": {"Code": "AccessDenied",
                       "Message": "not authorized to perform sts:AssumeRole"}},
            "AssumeRole",
        )

    sts = MagicMock()
    sts.assume_role = MagicMock(side_effect=_assume)
    return BedrockAccountClientProvider(sts_client=sts, boto_config=None)


def _runtime_reply(step: str):
    """bedrock-runtime /openai/v1/responses 비스트리밍 응답 대역(httpx.Response 모양)."""
    if step == "fail":
        return MagicMock(
            status_code=429,
            headers={"x-amzn-requestid": "rid-fail"},
            content=b'{"error":{"type":"throttling_error","message":"slow down"}}',
        )
    tokens = TURN2 if step == "final2" else TURN1
    if step == "search":
        output = [{"type": "function_call", "name": "web_search", "call_id": "call_1",
                   "arguments": json.dumps({"query": "q"})}]
    else:
        output = [{"type": "message", "role": "assistant",
                   "content": [{"type": "output_text", "text": "answer"}]}]
    body = {
        "id": f"resp-{step}",
        "status": "completed",
        "output": output,
        "usage": {
            "input_tokens": tokens[0],
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": tokens[1],
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": sum(tokens),
        },
    }
    return MagicMock(
        status_code=200,
        headers={"x-amzn-requestid": f"rid-{step}"},
        content=json.dumps(body).encode(),
    )


def _native_reply(step: str):
    """bedrock-runtime InvokeModel(boto3) 응답 대역. ``fail`` 은 ThrottlingException(→ 429)."""
    if step == "fail":
        return ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "InvokeModel"
        )
    tokens = TURN2 if step == "final2" else TURN1
    if step == "search":
        content = [{"type": "tool_use", "id": "toolu_1", "name": "web_search",
                    "input": {"query": "q"}}]
        stop = "tool_use"
    else:
        content = [{"type": "text", "text": "answer"}]
        stop = "end_turn"
    body = {
        "id": f"msg-{step}",
        "type": "message",
        "role": "assistant",
        "content": content,
        "stop_reason": stop,
        "usage": {"input_tokens": tokens[0], "output_tokens": tokens[1]},
    }
    return {
        "body": io.BytesIO(json.dumps(body).encode()),
        "ResponseMetadata": {"RequestId": f"rid-{step}"},
    }


class _Recorder(CostRecorder):
    """실 finalize 를 그대로 부르고, 몇 번 · 어떤 usage 로 불렸는지만 기록한다."""

    def __init__(self) -> None:
        super().__init__()
        self.usages: list = []

    async def finalize(self, redis, auth_context, model_config, usage, *args, **kwargs):
        self.usages.append(usage.model_copy(deep=True))
        return await super().finalize(redis, auth_context, model_config, usage, *args, **kwargs)


async def _drive(monkeypatch, r, *, route: str, exit_: str, scenario: str, stream: bool = False):
    """웹서치 분기 한 요청을 실 라우터로 끝까지 돌리고, 정산 결과를 모은다.

    ``exit_``: ``loop`` (도구 루프) · ``client_tool`` (F-7 패스스루) ·
    ``mcp_down`` (검색 없는 폴백).
    ``scenario``: ``account_failure`` (alias AssumeRole 실패) · ``late_failure`` (1턴 성공, 2턴 429)
    · ``success``.
    """
    from app.providers.bedrock_adapter import BedrockAdapter
    from app.providers.bedrock_openai_adapter import BedrockOpenAIAdapter
    from app.schemas.routing import RoutingProfileSchema
    from app.services import bg_tasks
    from app.services.cost_recorder import COST_STREAM_KEY
    from app.services.lua_loader import LuaScriptLoader
    from app.services.router_service import RouterService
    from app.services.sigv4_signer import SigV4Signer

    LuaScriptLoader.load_all(_SCRIPTS)
    monkeypatch.setattr("app.services.rate_limit_enforcement.load_all_scope_limits", _limits)
    await _clear_of_window_edge()

    auth = AuthContext(
        user_id=str(uuid.uuid4()),
        team_id=str(uuid.uuid4()),
        dept_id=str(uuid.uuid4()),
        roles=[Role.USER],
        auth_type=AuthType.VIRTUAL_KEY,
    )
    account = scenario == "account_failure"
    cfg = _config(route, account=account)
    keys = _counter_keys(auth, cfg.alias)
    if scenario == "account_failure":
        script: list[str] = []
    elif scenario == "late_failure":
        script = ["search", "fail"]
    elif exit_ == "loop":
        script = ["search", "final2"]
    else:
        script = ["final1"]
    unreachable = AssertionError("상류까지 갔다 — 계정 해석이 먼저 실패해야 한다")

    sts_calls: list[dict] = []
    if route == "responses":
        http = MagicMock()
        http.post = AsyncMock(side_effect=[_runtime_reply(s) for s in script] or unreachable)
        http.stream = MagicMock(side_effect=unreachable)
        signer = SigV4Signer(
            account_provider=_failing_account_provider(sts_calls) if account else None
        )
        signer._session = _PodSession()
        adapter = BedrockOpenAIAdapter(http_client=http, signer=signer)

        def _upstream_calls() -> int:
            return http.post.await_count + http.stream.call_count
    else:
        boto = MagicMock()
        boto.invoke_model = MagicMock(side_effect=[_native_reply(s) for s in script] or unreachable)
        boto.invoke_model_with_response_stream = MagicMock(side_effect=unreachable)
        adapter = (
            BedrockAdapter(
                boto,
                account_clients=_failing_account_provider(sts_calls),
                account_region="us-east-2",
            )
            if account
            else BedrockAdapter(boto)
        )

        def _upstream_calls() -> int:
            return boto.invoke_model.call_count + boto.invoke_model_with_response_stream.call_count

    # 전제 확인용: 상류 호출 직전(= enforce 가 예약한 뒤, 정산 전)의 카운터.
    reserved: list[dict[str, float]] = []
    for name in ("invoke", "invoke_stream"):
        real = getattr(adapter, name)

        async def _spy(*a, _real=real, **k):
            reserved.append(await _read(r, keys))
            return await _real(*a, **k)

        setattr(adapter, name, _spy)

    mcp = _Mcp(down=exit_ == "mcp_down")
    recorder = _Recorder()
    if route == "responses":
        from app.routers import openai_compat

        monkeypatch.setattr(
            openai_compat._router_service, "resolve_codex_model", _async_return(cfg)
        )
        profile = RoutingProfileSchema(
            client="codex", backend="bedrock_openai", account_role_arn=None,
            region="us-east-2", default_model=cfg.alias, external_id=None,
            web_search_enabled=True,
        )
        client = "codex"
        body: dict = {"model": cfg.alias, "max_output_tokens": 1000, "input": "hi"}
        if exit_ == "client_tool":
            body["tools"] = [{"type": "function", "name": "web_search",
                              "parameters": {"type": "object", "properties": {}}}]
    else:
        import app.routers.messages as messages_mod

        class _Router(RouterService):
            async def alias_provider(self, redis, db, alias):
                return ProviderType.BEDROCK

            async def resolve_bedrock_model(self, redis, db, alias):
                return cfg

        monkeypatch.setattr(messages_mod, "_router_service", _Router())
        profile = SimpleNamespace(
            backend="bedrock", default_model=None, web_search_enabled=True,
            region=None, account_role_arn=None,
        )
        client = "claude-code"
        body = {"model": cfg.alias, "max_tokens": 1000,
                "messages": [{"role": "user", "content": "hi"}]}
        if exit_ == "client_tool":
            body["tools"] = [{"name": "web_search", "description": "client's own",
                              "input_schema": {"type": "object", "properties": {}}}]
    if stream:
        body["stream"] = True

    app_state = SimpleNamespace(
        provider_registry=SimpleNamespace(get=lambda _p: adapter),
        cost_recorder=recorder,
        tokenizer=None,
        body_logger=None,
        metrics=None,
        agentcore_mcp_client=mcp,
        circuit_breaker=None,
        fallback_resolver=None,
        alias_provider_map={},
        bedrock_account_client_provider=None,
        routing_profile_loader=SimpleNamespace(load=_async_return(profile)),
    )
    state = {
        "auth_context": auth,
        "client": client,
        "_redis": r,
        "_session_factory": None,
        "request_id": f"req-ws-{route}-{exit_}-{scenario}",
    }
    req = _RouteRequest(body, state, app_state)
    try:
        if route == "responses":
            resp = await openai_compat._handle_responses(req)
        else:
            resp = await messages_mod.messages(req)
        if hasattr(resp, "body_iterator"):
            raw = b"".join([c async for c in resp.body_iterator])
        else:
            raw = bytes(resp.body)
        await bg_tasks.drain(timeout=2.0)
        after = await _read(r, keys)
        ledger = [
            json.loads(f["payload"])
            for _, f in await r.xrange(COST_STREAM_KEY, "-", "+")
            if json.loads(f["payload"]).get("user_id") == auth.user_id
        ]
    finally:
        own = [
            eid
            for eid, f in await r.xrange(COST_STREAM_KEY, "-", "+")
            if json.loads(f["payload"]).get("user_id") == auth.user_id
        ]
        if own:
            await r.xdel(COST_STREAM_KEY, *own)
        for sid in (auth.user_id, auth.team_id):
            async for k in r.scan_iter(match=f"*{sid}*"):
                await r.delete(k)
    return SimpleNamespace(
        status=resp.status_code,
        raw=raw,
        reserved=reserved,
        after=after,
        usages=recorder.usages,
        ledger=ledger,
        sts_calls=sts_calls,
        upstream_calls=_upstream_calls(),
        searches=mcp.searches,
    )


@pytest.fixture
async def real_redis():
    import redis.asyncio as aioredis

    r = aioredis.from_url(REDIS_PROOF_URL, decode_responses=True)
    try:
        yield r
    finally:
        await r.aclose()


def _assert_reserved_before_settlement(out) -> None:
    assert out.reserved and all(v > 0 for v in out.reserved[0].values()), (
        "전제: 상류 호출 직전에 두 스코프 모두 실제 예약(CPM·CPH·TPM)이 있어야 한다 — "
        f"{out.reserved}"
    )


def _assert_settled_at(out, route: str, tokens: tuple[int, int]) -> None:
    """두 스코프의 CPM·CPH 가 실제 비용, TPM 이 실제 토큰 — 예약 잔여도, 음수도 아니다."""
    cost = float(_cost(route, tokens))
    for scope in ("user", "team"):
        for window in ("cpm", "cph"):
            got = out.after[f"{scope}_{window}"]
            assert abs(got - cost) < 1e-9, (
                f"{scope} {window}: {got} != 실제 비용 {cost} — {out.after}"
            )
        assert out.after[f"{scope}_tpm"] == sum(tokens), out.after


# ── (a) 첫 턴 실패: alias 계정 AssumeRole 실패 ─────────────────────────────────────


@_redis_required
@pytest.mark.parametrize("exit_", ["loop", "client_tool", "mcp_down"])
@pytest.mark.parametrize("route", ["responses", "messages"])
async def test_a_first_turn_account_failure_releases_the_reservation_exactly_once(
    monkeypatch, real_redis, route, exit_
):
    """⚠️ 이 파일의 핵심. 고치기 전에는 finalize 가 한 번도 불리지 않아 예약이 그대로 남았다."""
    out = await _drive(
        monkeypatch, real_redis, route=route, exit_=exit_, scenario="account_failure"
    )

    assert out.status == 500, out.raw
    assert json.loads(out.raw)["error"]["message"] == ACCOUNT_UNAVAILABLE_MESSAGE
    # 트리거가 정말 alias 계정이다: 그 역할·ExternalId 로 AssumeRole 을 시도했고 상류는 안 갔다.
    assert out.sts_calls and {(c["RoleArn"], c.get("ExternalId")) for c in out.sts_calls} == {
        (ALIAS_ROLE, "alias-ext")
    }
    assert out.upstream_calls == 0
    _assert_reserved_before_settlement(out)
    assert all(abs(v) < 1e-9 for v in out.after.values()), (
        f"{route}/{exit_}: 예약이 정확히 한 번 환불되지 않았다"
        f"(양수=남음, 음수=이중 환불): {out.after}"
    )
    # 정산은 한 번, zero-usage 로. 원장에는 아무것도 쓰지 않는다(finalize 가 XADD 전에 return).
    assert [u.total_tokens for u in out.usages] == [0], out.usages
    assert out.ledger == []


@_redis_required
@pytest.mark.parametrize("exit_", ["loop", "client_tool", "mcp_down"])
@pytest.mark.parametrize("route", ["responses", "messages"])
async def test_a_the_stream_path_settles_the_same_failure_the_same_way(
    monkeypatch, real_redis, route, exit_
):
    """대조: 스트리밍 출구 셋은 고치기 전에도 항상 정산했다. 비스트리밍이 이것과 같아야 한다.

    정산하는 곳은 출구마다 다르다 — 도구 루프는 스티처의 finally, 두 패스스루(F-7 ·
    AgentCore 다운)는 SSE 헬퍼의 zero-usage on_usage(KI-08). 결과는 같아야 한다.

    HTTP 상태도 출구마다 다르다(docs/guides/multi-account-bedrock-setup.md §1.3). 도구 루프는
    첫 턴 전에 200 을 고정하므로 실패가 ``event: error`` 로만 보이고, 두 패스스루는 어댑터의
    500 을 그대로 쓴다.
    """
    out = await _drive(
        monkeypatch, real_redis, route=route, exit_=exit_, scenario="account_failure", stream=True
    )

    if exit_ == "loop":
        assert out.status == 200, out.raw
        assert b"event: error" in out.raw, out.raw
    else:
        assert out.status == 500, out.raw
    assert ACCOUNT_UNAVAILABLE_MESSAGE.encode() in out.raw, out.raw
    assert out.upstream_calls == 0
    _assert_reserved_before_settlement(out)
    assert all(abs(v) < 1e-9 for v in out.after.values()), out.after
    assert [u.total_tokens for u in out.usages] == [0], out.usages
    assert out.ledger == []


# ── (b) 1턴 성공 뒤 2턴 실패 ─────────────────────────────────────────────────────


@_redis_required
@pytest.mark.parametrize("route", ["responses", "messages"])
async def test_b_a_later_turn_failure_settles_the_earlier_turn_once(monkeypatch, real_redis, route):
    """1턴 토큰은 소비됐다 — 한 번만, 그 사용량으로 정산해야 한다(F-9). 고치기 전에도 통과한다.

    라우터에 release 를 하나 더 두는 식으로 고치면 여기서 이중 환불(음수)이 드러난다.
    """
    out = await _drive(monkeypatch, real_redis, route=route, exit_="loop", scenario="late_failure")

    assert out.status == 429, out.raw
    assert out.searches == ["q"] and out.upstream_calls == 2
    _assert_reserved_before_settlement(out)
    assert [(u.input_tokens, u.output_tokens, u.web_search_count) for u in out.usages] == [
        (*TURN1, 1)
    ], out.usages
    _assert_settled_at(out, route, TURN1)
    (row,) = out.ledger
    assert (row["input_tokens"], row["output_tokens"], row["web_search_count"]) == (*TURN1, 1)
    assert Decimal(str(row["cost_usd"])) == _cost(route, TURN1)


# ── (c) 성공 경로 ────────────────────────────────────────────────────────────────


@_redis_required
@pytest.mark.parametrize("exit_", ["loop", "client_tool", "mcp_down"])
@pytest.mark.parametrize("route", ["responses", "messages"])
async def test_c_the_success_path_settles_once_with_the_real_usage(
    monkeypatch, real_redis, route, exit_
):
    """성공 경로는 바뀌지 않는다 — 한 번, 실제(합산) 사용량으로. 고치기 전에도 통과한다."""
    out = await _drive(monkeypatch, real_redis, route=route, exit_=exit_, scenario="success")

    if exit_ == "loop":
        tokens, searches = (TURN1[0] + TURN2[0], TURN1[1] + TURN2[1]), 1
    else:
        tokens, searches = TURN1, 0
    assert out.status == 200, out.raw
    assert len(out.searches) == searches
    _assert_reserved_before_settlement(out)
    assert [(u.input_tokens, u.output_tokens, u.web_search_count) for u in out.usages] == [
        (*tokens, searches)
    ], out.usages
    _assert_settled_at(out, route, tokens)
    (row,) = out.ledger
    assert (row["input_tokens"], row["output_tokens"], row["web_search_count"]) == (
        *tokens,
        searches,
    )
    assert Decimal(str(row["cost_usd"])) == _cost(route, tokens)
