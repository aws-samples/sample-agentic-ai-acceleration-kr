# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Per-request model selection on /v1/responses (GPT-5.6 Sol / Terra / Luna).

Before this feature the routing profile's ``default_model`` was the only reachable
model on this route, so the three GPT-5.6 aliases added by migration 0025 would have
been unreachable. These tests pin both halves of the contract:

  * an ACTIVE BEDROCK_MANTLE_OPENAI alias sent by the client WINS, and the outgoing
    body carries that alias's provider_model_id (Mantle rejects our alias names);
  * anything else (unknown alias, wrong provider, inactive, absent) silently FALLS
    BACK to default_model — the pre-existing behaviour, which is what keeps existing
    Codex clients (they send upstream names like "gpt-5.5-codex") working.

The fallback is the regression-critical half: a strict lookup would turn every
existing Codex request into a 404.

Two exceptions to "anything else falls back", both pinned below:
  * an INACTIVE alias is an operator kill switch → 404, never a silent substitution;
  * a value that cannot even be used as a lookup key (non-str, lone surrogate) is
    rejected before the resolver, because those raise DBAPIError / UnicodeEncodeError
    rather than LookupError and would otherwise escape the fallback as a 500.
"""

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.responses import JSONResponse

from app.routers.openai_compat import _handle_responses
from app.schemas.domain import (
    ApiFormat,
    ModelConfigSchema,
    ModelPricingSchema,
    ModelStatus,
    ProviderType,
    TokenUsage,
)
from app.schemas.routing import RoutingProfileSchema

ENDPOINT = "https://bedrock-mantle.us-east-2.api.aws/openai"

# alias -> provider_model_id, mirroring migration 0025 + 0017.
MANTLE_OPENAI_ALIASES = {
    "codex-gpt": "openai.gpt-5.5",
    "codex-gpt-5.6-sol": "openai.gpt-5.6-sol",
    "codex-gpt-5.6-terra": "openai.gpt-5.6-terra",
    "codex-gpt-5.6-luna": "openai.gpt-5.6-luna",
}


def _model_config(alias: str) -> ModelConfigSchema:
    return ModelConfigSchema(
        provider_model_id=MANTLE_OPENAI_ALIASES[alias],
        alias=alias,
        provider=ProviderType.BEDROCK_MANTLE_OPENAI,
        api_format=ApiFormat.OPENAI_RESPONSES,
        endpoint=ENDPOINT,
        pricing=ModelPricingSchema(
            input_per_1k=Decimal("0.002200"), output_per_1k=Decimal("0.013200")
        ),
        status=ModelStatus.ACTIVE,
    )


def _profile(default_model: str = "codex-gpt") -> RoutingProfileSchema:
    # Codex = in-account (account_role_arn NULL), Ohio.
    return RoutingProfileSchema(
        client="codex",
        backend="mantle",
        account_role_arn=None,
        region="us-east-2",
        default_model=default_model,
        external_id=None,
    )


class _FakeRequest:
    """Minimal Request stand-in: _handle_responses reads scope["state"], app.state, body()."""

    def __init__(self, body: dict, state: dict, app_state) -> None:
        self._body = json.dumps(body).encode()
        self.scope = {"state": state}
        self.app = MagicMock()
        self.app.state = app_state

    async def body(self) -> bytes:
        return self._body

    async def is_disconnected(self) -> bool:
        return False


class _FakeSessionFactory:
    """async-context-manager session factory, exercising the NON-degraded DB branch.

    The default _build path leaves _session_factory None, which takes the db=None
    branch. Selection has to behave identically when a session IS available, since
    that is the real production path.
    """

    def __init__(self) -> None:
        self.session = MagicMock(name="db_session")
        self.entered = 0

    def __call__(self):
        return self

    async def __aenter__(self):
        self.entered += 1
        return self.session

    async def __aexit__(self, *exc) -> bool:
        return False


def _build(
    requested_model: str | None,
    *,
    default_model: str = "codex-gpt",
    include_model_key: bool = True,
    with_session: bool = False,
    stream: bool = False,
    web_search: bool = False,
):
    """Wire up _handle_responses with fakes; returns (request, adapter, resolver_calls)."""
    profile = _profile(default_model)
    if web_search:
        profile = profile.model_copy(update={"web_search_enabled": True})

    # Resolver stands in for RouterService.resolve_codex_model: it knows exactly the
    # aliases migration 0025/0017 register and raises LookupError for anything else,
    # which is what the real resolver does for a wrong-provider/inactive/unknown ref.
    resolver_calls: list[str] = []

    async def _resolve(redis, db, model_ref):
        resolver_calls.append(model_ref)
        if model_ref in MANTLE_OPENAI_ALIASES:
            return _model_config(model_ref)
        raise LookupError(f"Model alias '{model_ref}' not found")

    routing_loader = MagicMock()
    routing_loader.load = AsyncMock(return_value=profile)

    adapter = MagicMock()
    adapter.invoke = AsyncMock(
        return_value=(
            200,
            json.dumps(
                {"status": "completed", "usage": {"input_tokens": 5, "output_tokens": 2,
                                                 "total_tokens": 7}}
            ).encode(),
            {},
            TokenUsage(input_tokens=5, output_tokens=2, total_tokens=7),
        )
    )

    async def _chunks():
        yield b'data: {"type":"response.completed"}\n\n'

    adapter.invoke_stream = AsyncMock(
        return_value=(200, _chunks(), {}, TokenUsage(input_tokens=5, output_tokens=2,
                                                     total_tokens=7))
    )

    app_state = MagicMock()
    app_state.provider_registry.get = MagicMock(return_value=adapter)
    app_state.cost_recorder.finalize = AsyncMock()
    app_state.routing_profile_loader = routing_loader
    # No AgentCore MCP client → the web-search loop branch stays out of the way,
    # unless a test explicitly opts in (web_search=True).
    app_state.agentcore_mcp_client = MagicMock() if web_search else None

    state = {
        "auth_context": None,     # skips check_key_scope + rate limits; selection is what we test
        "client": "codex",
        "_redis": None,
        # None → the is_db_degraded/no-factory branch (db=None); a factory takes the
        # real production branch that opens a session.
        "_session_factory": _FakeSessionFactory() if with_session else None,
        "request_id": "req-1",
    }

    body: dict = {"input": "hello"}
    if include_model_key:
        body["model"] = requested_model
    if stream:
        body["stream"] = True

    request = _FakeRequest(body, state, app_state)
    return request, adapter, resolver_calls, _resolve


def _sent_model(adapter) -> str:
    """The model id actually put on the wire to Mantle."""
    sent_body = adapter.invoke.await_args.args[0]
    return json.loads(sent_body)["model"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "alias",
    ["codex-gpt-5.6-sol", "codex-gpt-5.6-terra", "codex-gpt-5.6-luna"],
)
async def test_requested_gpt56_alias_wins_over_default(monkeypatch, alias):
    """All three GPT-5.6 aliases are reachable per request; default_model is NOT used."""
    request, adapter, calls, resolve = _build(alias)
    monkeypatch.setattr(
        "app.routers.openai_compat._router_service.resolve_codex_model", resolve
    )

    resp = await _handle_responses(request)

    assert resp.status_code == 200
    # Resolved the requested alias, and never fell back to the default.
    assert calls == [alias]
    # Mantle gets the provider model id, not our alias.
    assert _sent_model(adapter) == MANTLE_OPENAI_ALIASES[alias]
    # Cost/usage is attributed to the alias actually served.
    assert request.scope["state"]["model_config"].alias == alias


@pytest.mark.asyncio
async def test_unknown_model_falls_back_to_default_no_404(monkeypatch):
    """REGRESSION GUARD: Codex sends upstream names we do not register.

    Before per-request selection these were ignored; a strict lookup would 404 every
    existing Codex request. The fallback must keep them on default_model.
    """
    request, adapter, calls, resolve = _build("gpt-5.5-codex")
    monkeypatch.setattr(
        "app.routers.openai_compat._router_service.resolve_codex_model", resolve
    )

    resp = await _handle_responses(request)

    assert resp.status_code == 200, "unknown model must NOT 404 — it falls back"
    # Tried the requested value, then the default.
    assert calls == ["gpt-5.5-codex", "codex-gpt"]
    assert _sent_model(adapter) == "openai.gpt-5.5"


@pytest.mark.asyncio
async def test_absent_model_uses_default(monkeypatch):
    """No "model" key at all → default_model, with no wasted resolver call."""
    request, adapter, calls, resolve = _build(None, include_model_key=False)
    monkeypatch.setattr(
        "app.routers.openai_compat._router_service.resolve_codex_model", resolve
    )

    resp = await _handle_responses(request)

    assert resp.status_code == 200
    assert calls == ["codex-gpt"]  # empty requested alias short-circuits
    assert _sent_model(adapter) == "openai.gpt-5.5"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_model", [123, ["codex-gpt"], {"name": "x"}, True, None])
async def test_non_string_model_falls_back_without_500(monkeypatch, bad_model):
    """REGRESSION GUARD: "model" is untrusted client JSON and may not be a string.

    A non-text bind against the VARCHAR alias column raises DBAPIError from asyncpg —
    NOT LookupError — so it would escape the fallback and 500. Verified against real
    Postgres: 123 / ["a"] / {"x":1} / True all raise ProgrammingError/DBAPIError.
    The isinstance(str) guard must keep these on default_model.
    """
    async def _resolve(redis, db, model_ref):
        if not isinstance(model_ref, str):
            raise AssertionError(f"non-str {model_ref!r} must never reach the resolver")
        if model_ref in MANTLE_OPENAI_ALIASES:
            return _model_config(model_ref)
        raise LookupError(model_ref)

    request, adapter, _calls, _ = _build(bad_model)
    monkeypatch.setattr(
        "app.routers.openai_compat._router_service.resolve_codex_model", _resolve
    )

    resp = await _handle_responses(request)

    assert resp.status_code == 200
    assert _sent_model(adapter) == "openai.gpt-5.5"  # profile default


@pytest.mark.asyncio
@pytest.mark.parametrize("surrogate", ["\ud800", "codex\ud800gpt", "\udfff"])
async def test_lone_surrogate_model_falls_back_without_500(monkeypatch, surrogate):
    """REGRESSION GUARD: a lone UTF-16 surrogate is valid JSON and a real str.

    It passes isinstance(str) and is non-empty, so a str-only guard lets it through to
    redis.get(f"model:{ref}") -- which raises UnicodeEncodeError (redis-py encodes with
    encoding_errors="strict"), NOT LookupError, so it escapes the fallback and 500s a
    request that returned 200 before per-request selection existed.

    The resolver here encodes the key exactly like redis-py does, so removing the
    surrogate half of the guard makes this test fail rather than silently pass.
    """
    async def _resolve(redis, db, model_ref):
        model_ref.encode("utf-8")  # what redis-py does to build the cache key
        if model_ref in MANTLE_OPENAI_ALIASES:
            return _model_config(model_ref)
        raise LookupError(model_ref)

    request, adapter, _calls, _ = _build(surrogate)
    monkeypatch.setattr(
        "app.routers.openai_compat._router_service.resolve_codex_model", _resolve
    )

    resp = await _handle_responses(request)

    assert resp.status_code == 200
    assert _sent_model(adapter) == "openai.gpt-5.5"  # profile default


@pytest.mark.asyncio
@pytest.mark.parametrize("nul", ["\x00", "codex\x00gpt", "codex-gpt\x00"])
async def test_nul_byte_model_falls_back_without_500(monkeypatch, nul):
    """REGRESSION GUARD: a NUL byte passes isinstance AND encodes to UTF-8 cleanly.

    So neither the str check nor the surrogate check catches it, but Postgres has no NUL
    in text: asyncpg raises CharacterNotInRepertoireError (a DBAPIError, NOT LookupError).
    Verified against real Postgres. The resolver here raises the same class the driver
    does, so dropping the NUL guard makes this test fail.

    Asserting on the RESPONSE alone would not pin the guard: the except DBAPIError
    backstop also yields 200, so the guard could be deleted unnoticed. This asserts the
    NUL value never reaches the resolver at all, which is the guard's actual job.
    """
    from sqlalchemy.exc import DBAPIError

    seen: list[str] = []

    async def _resolve(redis, db, model_ref):
        seen.append(model_ref)
        if "\x00" in model_ref:
            # What asyncpg really raises: CharacterNotInRepertoireError, a DBAPIError.
            raise DBAPIError("SELECT alias", {}, Exception("invalid byte sequence 0x00"))
        if model_ref in MANTLE_OPENAI_ALIASES:
            return _model_config(model_ref)
        raise LookupError(model_ref)

    request, adapter, _calls, _ = _build(nul)
    monkeypatch.setattr(
        "app.routers.openai_compat._router_service.resolve_codex_model", _resolve
    )

    resp = await _handle_responses(request)

    assert resp.status_code == 200
    assert _sent_model(adapter) == "openai.gpt-5.5"  # profile default
    # The guard rejected it up front — the DB was never asked about a NUL-bearing value.
    assert seen == ["codex-gpt"], f"NUL value reached the resolver: {seen!r}"


@pytest.mark.asyncio
async def test_overlong_model_falls_back_and_log_is_bounded(monkeypatch):
    """An over-long value is harmless to the DB (compared by value, not column width),
    but must NOT be logged verbatim — it is unbounded client input, so one request would
    become a multi-MB log line.

    caplog is useless here: structlog is unconfigured in tests (PrintLoggerFactory), so
    nothing reaches stdlib logging. Capture the structlog call itself instead.
    """
    huge = "a" * 100_000
    request, adapter, _calls, resolve = _build(huge)
    monkeypatch.setattr(
        "app.routers.openai_compat._router_service.resolve_codex_model", resolve
    )

    logged: list[dict] = []
    monkeypatch.setattr(
        "app.routers.openai_compat.logger.info",
        lambda event, **kw: logged.append({"event": event, **kw}),
    )

    resp = await _handle_responses(request)

    assert resp.status_code == 200
    assert _sent_model(adapter) == "openai.gpt-5.5"

    fallback = [r for r in logged
                if r["event"] == "responses_requested_model_unresolved_using_default"]
    assert fallback, "the silent substitution was not logged at INFO"
    # 128 = the alias column's own width, so nothing legitimate is ever truncated.
    assert len(fallback[0]["requested"]) <= 128, (
        f"unbounded client value logged verbatim ({len(fallback[0]['requested'])} chars)"
    )


@pytest.mark.asyncio
async def test_explicit_null_model_falls_back(monkeypatch):
    """{"model": null} is present-but-null — a different code path from an absent key."""
    request, adapter, _calls, resolve = _build(None, include_model_key=True)
    monkeypatch.setattr(
        "app.routers.openai_compat._router_service.resolve_codex_model", resolve
    )

    resp = await _handle_responses(request)

    assert resp.status_code == 200
    assert _sent_model(adapter) == "openai.gpt-5.5"


@pytest.mark.asyncio
async def test_inactive_alias_is_denied_not_substituted(monkeypatch):
    """SECURITY/BILLING: status=INACTIVE is an operator kill switch.

    A disabled alias must 404, NOT silently fall back to default_model — that would
    both defeat the kill switch and bill the request to the wrong alias. This is why
    the resolver raises ModelInactiveError (a LookupError subclass) for inactive and
    the fallback re-raises it.
    """
    from app.services.router_service import ModelInactiveError

    async def _resolve(redis, db, model_ref):
        if model_ref == "codex-gpt-5.6-sol":
            raise ModelInactiveError("Model 'codex-gpt-5.6-sol' is inactive")
        return _model_config(model_ref)

    request, adapter, _calls, _ = _build("codex-gpt-5.6-sol")
    monkeypatch.setattr(
        "app.routers.openai_compat._router_service.resolve_codex_model", _resolve
    )

    resp = await _handle_responses(request)

    assert resp.status_code == 404
    assert b"inactive" in resp.body
    adapter.invoke.assert_not_awaited()  # never served as another model


@pytest.mark.asyncio
async def test_selection_works_with_db_session(monkeypatch):
    """The production branch: a DB session is available (not degraded)."""
    request, adapter, calls, resolve = _build("codex-gpt-5.6-sol", with_session=True)
    monkeypatch.setattr(
        "app.routers.openai_compat._router_service.resolve_codex_model", resolve
    )

    resp = await _handle_responses(request)

    assert resp.status_code == 200
    assert calls == ["codex-gpt-5.6-sol"]
    assert _sent_model(adapter) == "openai.gpt-5.6-sol"
    assert request.scope["state"]["_session_factory"].entered == 1


@pytest.mark.asyncio
async def test_selection_applies_on_stream_path(monkeypatch):
    """stream=true takes invoke_stream; the selected model must reach it too."""
    request, adapter, _calls, resolve = _build("codex-gpt-5.6-luna", stream=True)
    monkeypatch.setattr(
        "app.routers.openai_compat._router_service.resolve_codex_model", resolve
    )

    resp = await _handle_responses(request)

    assert resp.status_code == 200
    sent = json.loads(adapter.invoke_stream.await_args.args[0])
    assert sent["model"] == "openai.gpt-5.6-luna"
    adapter.invoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_selection_applies_on_web_search_path(monkeypatch):
    """The web-search loop is the LIVE path wherever it is enabled (migration 0021 sets
    web_search_enabled=true for codex), so selection must reach it as well."""
    request, _adapter, _calls, resolve = _build("codex-gpt-5.6-terra", web_search=True)
    monkeypatch.setattr(
        "app.routers.openai_compat._router_service.resolve_codex_model", resolve
    )

    captured: dict = {}

    async def _capture_invoke(body, pmid, **kw):
        captured["model"] = json.loads(body)["model"]
        captured["pmid"] = pmid
        return (200, b"{}", {}, TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2))

    async def _fake_loop(**kwargs):
        # Drive the router's own per-turn invoke wrapper, which is what rewrites the
        # model for each web-search turn.
        status, _body, _headers, _usage = await kwargs["invoke"](kwargs["initial_req_data"])
        return JSONResponse(status_code=status, content={"ok": True})

    request.app.state.provider_registry.get().invoke = AsyncMock(side_effect=_capture_invoke)
    monkeypatch.setattr("app.services.web_search_loop.run_web_search_loop", _fake_loop)

    resp = await _handle_responses(request)

    assert resp.status_code == 200
    # The web-search turn carried the REQUESTED model, not the profile default.
    assert captured["model"] == "openai.gpt-5.6-terra"
    assert captured["pmid"] == "openai.gpt-5.6-terra"


@pytest.mark.asyncio
async def test_requested_equals_default_resolves_once(monkeypatch):
    """Sending the default explicitly must not resolve twice (it is the same lookup)."""
    request, _adapter, calls, resolve = _build("codex-gpt", default_model="codex-gpt")
    monkeypatch.setattr(
        "app.routers.openai_compat._router_service.resolve_codex_model", resolve
    )

    resp = await _handle_responses(request)

    assert resp.status_code == 200
    assert calls == ["codex-gpt"]


@pytest.mark.asyncio
async def test_provider_model_id_is_also_accepted(monkeypatch):
    """The real resolver falls back to provider_model_id matching, so a client may send
    the raw Mantle id. Selection must not block that path."""
    async def _resolve(redis, db, model_ref):
        if model_ref == "openai.gpt-5.6-sol":
            return _model_config("codex-gpt-5.6-sol")
        if model_ref in MANTLE_OPENAI_ALIASES:
            return _model_config(model_ref)
        raise LookupError(model_ref)

    request, adapter, _calls, _ = _build("openai.gpt-5.6-sol")
    monkeypatch.setattr(
        "app.routers.openai_compat._router_service.resolve_codex_model", _resolve
    )

    resp = await _handle_responses(request)

    assert resp.status_code == 200
    assert _sent_model(adapter) == "openai.gpt-5.6-sol"


@pytest.mark.asyncio
async def test_default_model_unresolvable_still_404s(monkeypatch):
    """A broken default (e.g. alias deleted under it) must surface as 404, not be
    swallowed by the fallback."""
    request, _adapter, _calls, resolve = _build(
        None, include_model_key=False, default_model="deleted-alias"
    )
    monkeypatch.setattr(
        "app.routers.openai_compat._router_service.resolve_codex_model", resolve
    )

    resp = await _handle_responses(request)

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_scope_denial_applies_to_requested_model(monkeypatch):
    """SECURITY: per-request selection is not an entitlement bypass.

    A key scoped to codex-gpt must not reach a GPT-5.6 alias by asking for it — the
    scope check runs on the RESOLVED model, so it denies with 400.
    """
    request, adapter, _calls, resolve = _build("codex-gpt-5.6-sol")
    monkeypatch.setattr(
        "app.routers.openai_compat._router_service.resolve_codex_model", resolve
    )

    auth_context = MagicMock()
    auth_context.allowed_models = ["codex-gpt"]
    request.scope["state"]["auth_context"] = auth_context

    denied: dict = {}

    def _check(ctx, model_config):
        denied["alias"] = getattr(model_config, "alias", model_config)
        raise PermissionError("not allowed")

    monkeypatch.setattr(
        "app.routers.openai_compat._router_service.check_key_scope", _check
    )

    resp = await _handle_responses(request)

    assert resp.status_code == 400  # 400 not 403 (see test_scope_denial_400.py)
    # The check saw the REQUESTED model, not the profile default.
    assert denied["alias"] == "codex-gpt-5.6-sol"
    adapter.invoke.assert_not_awaited()
