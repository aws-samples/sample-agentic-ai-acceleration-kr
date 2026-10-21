# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""count_tokens for models Bedrock CountTokens refuses, and the thinking/max_tokens 400.

Grounded in live measurements (services/token_count_fallback.py docstring): Bedrock
answers ValidationException "The provided model doesn't support counting tokens." for
Opus 4.7+ and the 5 family — permission is irrelevant. Claude Code sends ``thinking``
without ``max_tokens`` whenever the conversation holds thinking blocks, which the
gateway's ``max_tokens: 1`` placeholder turned into a 400 even on countable models.
"""

from __future__ import annotations

import asyncio
import copy
import json
import pickle
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from botocore.exceptions import ClientError
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.providers.base import CountTokensResult
from app.providers.mantle_adapter import MantleAdapter
from app.providers.registry import ProviderRegistry
from app.routers import messages as messages_router
from app.schemas.domain import (
    ApiFormat,
    ModelConfigSchema,
    ModelPricingSchema,
    ModelStatus,
    ProviderType,
)
from app.schemas.routing import RoutingProfileSchema
from app.services import token_count_fallback as tcf
from app.services.tokenizer import TokenizerService

_REFUSED = CountTokensResult(
    400, 0, "ValidationException", "The provided model doesn't support counting tokens."
)


@pytest.fixture(autouse=True)
def _fresh_caches():
    tcf.clear_caches()
    yield
    tcf.clear_caches()


@pytest.fixture
def fallback_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "count_tokens_mantle_fallback", True)
    monkeypatch.setattr(get_settings(), "aws_region", "ap-northeast-2")


def _cfg(alias: str, provider_model_id: str, **extra) -> ModelConfigSchema:
    return ModelConfigSchema(
        alias=alias,
        provider=ProviderType.BEDROCK,
        api_format=ApiFormat.BEDROCK_NATIVE,
        provider_model_id=provider_model_id,
        endpoint="ap-northeast-2",
        status=ModelStatus.ACTIVE,
        pricing=ModelPricingSchema(input_per_1k=Decimal("0.003"), output_per_1k=Decimal("0.015")),
        **extra,
    )


class _Mantle:
    """Records Mantle count calls; answers per model from ``answers`` (default 404)."""

    def __init__(self, answers: dict[str, CountTokensResult]):
        self.answers = answers
        self.calls: list[dict] = []

    async def count_tokens(self, body, model_id, *, profile, endpoint, account=None, **kw):
        self.calls.append({
            "model": model_id, "body": json.loads(body), "profile": profile,
            "endpoint": endpoint, "account": account,
        })
        return self.answers.get(model_id, CountTokensResult(404, 0, "HTTP404", "not found"))


@pytest.fixture(autouse=True)
def _router_service(monkeypatch):
    """Module-global RouterService methods are patched per test and restored after."""
    svc = messages_router._router_service
    monkeypatch.setattr(svc, "resolve_bedrock_model", AsyncMock())
    monkeypatch.setattr(svc, "check_key_scope", MagicMock())
    return svc


def _app(bedrock, config, *, mantle=None, profile=None, account_provider=None):
    app = FastAPI()
    registry = ProviderRegistry()
    registry.register(ProviderType.BEDROCK, bedrock)
    if mantle is not None:
        registry.register(ProviderType.BEDROCK_MANTLE, mantle)
    app.state.provider_registry = registry
    if profile is not None:
        app.state.routing_profile_loader = MagicMock(load=AsyncMock(return_value=profile))
    if account_provider is not None:
        app.state.bedrock_account_client_provider = account_provider
    messages_router._router_service.resolve_bedrock_model.return_value = config

    @app.middleware("http")
    async def inject_state(request, call_next):
        request.scope["state"] = {
            "auth_context": None, "_redis": None, "_session_factory": None,
            "_degradation_manager": None, "request_id": "t", "client": "claude-code",
        }
        return await call_next(request)

    app.include_router(messages_router.router)
    return app


async def _post(app, body):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        return await c.post("/v1/messages/count_tokens", json=body)


def _msg(**extra):
    return {"model": "alias", "messages": [{"role": "user", "content": "hi"}], **extra}


# ── thinking / max_tokens: the 400 on models Bedrock *can* count ──────────────────


@pytest.mark.asyncio
async def test_thinking_without_max_tokens_gets_a_valid_placeholder():
    bedrock = MagicMock(count_tokens=AsyncMock(return_value=CountTokensResult(200, 56)))
    app = _app(bedrock, _cfg("sonnet46", "global.anthropic.claude-sonnet-4-6"))
    resp = await _post(app, _msg(thinking={"type": "enabled", "budget_tokens": 1024}))
    assert resp.status_code == 200 and resp.json() == {"input_tokens": 56}
    sent = json.loads(bedrock.count_tokens.call_args.args[0])
    # max_tokens: 1 with budget 1024 is Bedrock's "max_tokens must be greater than
    # thinking.budget_tokens" — the 400 every thinking conversation hit.
    assert sent["max_tokens"] == 1025
    assert bedrock.count_tokens.call_args.args[1] == "anthropic.claude-sonnet-4-6"
    assert resp.headers["x-llm-gateway-token-count-source"] == "bedrock"


@pytest.mark.asyncio
async def test_count_body_gets_the_same_thinking_normalisation_as_messages():
    """Haiku 4.5 runs only `enabled`; /v1/messages converts `adaptive` — the count must too."""
    bedrock = MagicMock(count_tokens=AsyncMock(return_value=CountTokensResult(200, 60)))
    app = _app(bedrock, _cfg("haiku45", "global.anthropic.claude-haiku-4-5-20251001-v1:0"))
    resp = await _post(app, _msg(thinking={"type": "adaptive"}))
    assert resp.status_code == 200
    sent = json.loads(bedrock.count_tokens.call_args.args[0])
    assert sent["thinking"] == {"type": "enabled", "budget_tokens": 4096}
    assert sent["max_tokens"] == 4097
    assert sent["anthropic_version"] == "bedrock-2023-05-31"


@pytest.mark.asyncio
async def test_output_config_is_not_sent_to_count_endpoints(fallback_on):
    """Bedrock CountTokens 400s on output_config.effort that InvokeModel accepts; effort
    never changes the input count (measured), so neither count body carries it."""
    bedrock = MagicMock(count_tokens=AsyncMock(return_value=CountTokensResult(200, 220)))
    app = _app(bedrock, _cfg("sonnet46", "global.anthropic.claude-sonnet-4-6"))
    resp = await _post(app, _msg(thinking={"type": "adaptive"}, output_config={"effort": "high"}))
    assert resp.status_code == 200
    assert "output_config" not in json.loads(bedrock.count_tokens.call_args.args[0])

    refused = MagicMock(count_tokens=AsyncMock(return_value=_REFUSED))
    mantle = _Mantle({"anthropic.claude-sonnet-5": CountTokensResult(200, 329)})
    app = _app(refused, _cfg("sonnet5", "anthropic.claude-sonnet-5"), mantle=mantle)
    await _post(app, _msg(thinking={"type": "adaptive"}, output_config={"effort": "high"}))
    assert "output_config" not in mantle.calls[0]["body"]


@pytest.mark.asyncio
async def test_no_thinking_keeps_the_minimal_placeholder():
    bedrock = MagicMock(count_tokens=AsyncMock(return_value=CountTokensResult(200, 9)))
    app = _app(bedrock, _cfg("sonnet46", "anthropic.claude-sonnet-4-6"))
    await _post(app, _msg())
    assert json.loads(bedrock.count_tokens.call_args.args[0])["max_tokens"] == 1


# ── models Bedrock refuses ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refused_model_with_fallback_off_is_an_explicit_400_not_501():
    bedrock = MagicMock(count_tokens=AsyncMock(return_value=_REFUSED))
    mantle = _Mantle({})
    app = _app(bedrock, _cfg("sonnet5", "anthropic.claude-sonnet-5"), mantle=mantle)
    resp = await _post(app, _msg())
    # 501 would make Claude Code replace the count with a real, billed /v1/messages call.
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["type"] == "invalid_request_error"
    assert "sonnet5" in err["message"] and "does not support" in err["message"]
    assert "COUNT_TOKENS_MANTLE_FALLBACK" in err["message"]
    assert mantle.calls == []


@pytest.mark.asyncio
async def test_refused_model_is_counted_on_mantle_with_the_same_model(fallback_on):
    bedrock = MagicMock(count_tokens=AsyncMock(return_value=_REFUSED))
    mantle = _Mantle({"anthropic.claude-sonnet-5": CountTokensResult(200, 547)})
    app = _app(bedrock, _cfg("sonnet5", "anthropic.claude-sonnet-5"), mantle=mantle)
    resp = await _post(app, _msg(thinking={"type": "enabled", "budget_tokens": 1024}))
    assert resp.status_code == 200 and resp.json() == {"input_tokens": 547}
    assert resp.headers["x-llm-gateway-token-count-source"] == "mantle"
    assert resp.headers["x-llm-gateway-token-count-model"] == "anthropic.claude-sonnet-5"
    call = mantle.calls[0]
    assert call["endpoint"] == "https://bedrock-mantle.ap-northeast-2.api.aws/anthropic"
    assert call["profile"].region == "ap-northeast-2"
    # Mantle gets an Anthropic body for the model it counts: no Bedrock-only fields,
    # thinking in the form that model runs (the 5 family is adaptive-only).
    assert call["body"]["model"] == "anthropic.claude-sonnet-5"
    assert call["body"]["thinking"] == {"type": "adaptive"}
    assert "max_tokens" not in call["body"] and "anthropic_version" not in call["body"]


@pytest.mark.asyncio
async def test_sibling_is_used_only_when_mantle_lacks_the_model(fallback_on):
    bedrock = MagicMock(count_tokens=AsyncMock(return_value=_REFUSED))
    mantle = _Mantle({"anthropic.claude-opus-5": CountTokensResult(200, 1045)})
    app = _app(bedrock, _cfg("sonnet55", "global.anthropic.claude-sonnet-5-5"), mantle=mantle)
    resp = await _post(app, _msg())
    assert resp.status_code == 200 and resp.json() == {"input_tokens": 1045}
    assert resp.headers["x-llm-gateway-token-count-model"] == "anthropic.claude-opus-5"
    assert [c["model"] for c in mantle.calls] == [
        "anthropic.claude-sonnet-5-5", "anthropic.claude-opus-5-5",
        "anthropic.claude-haiku-5-5", "anthropic.claude-opus-5",
    ]
    # A model Mantle does not serve here is not asked again on the next request.
    mantle.calls.clear()
    await _post(app, _msg())
    assert [c["model"] for c in mantle.calls] == ["anthropic.claude-opus-5"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pmid", ["global.anthropic.claude-fable-5", "global.anthropic.claude-opus-4-7"]
)
async def test_models_with_a_different_tokenizer_never_use_a_sibling(fallback_on, pmid):
    """Fable 5 (+8–10% on code/Korean) and Opus 4.7 (+26% with tools) count differently."""
    bedrock = MagicMock(count_tokens=AsyncMock(return_value=_REFUSED))
    mantle = _Mantle({"anthropic.claude-opus-5": CountTokensResult(200, 1045)})
    app = _app(bedrock, _cfg("x", pmid), mantle=mantle)
    resp = await _post(app, _msg())
    assert resp.status_code == 400
    assert [c["model"] for c in mantle.calls] == [pmid.removeprefix("global.")]


@pytest.mark.asyncio
async def test_a_401_blocks_the_account_and_region(fallback_on):
    bedrock = MagicMock(count_tokens=AsyncMock(return_value=_REFUSED))
    mantle = _Mantle({"anthropic.claude-opus-5": CountTokensResult(401, 0, "HTTP401", "denied")})
    app = _app(bedrock, _cfg("opus5", "anthropic.claude-opus-5"), mantle=mantle)
    assert (await _post(app, _msg())).status_code == 400
    assert len(mantle.calls) == 1  # no sibling tried with the same refused credentials
    await _post(app, _msg())
    assert len(mantle.calls) == 1  # and not retried for this account/region for a while


@pytest.mark.asyncio
async def test_a_403_is_one_model_and_the_next_candidate_still_counts(fallback_on):
    """Mantle reports IAM denial as 401; 403 is a missing entitlement for that model."""
    bedrock = MagicMock(count_tokens=AsyncMock(return_value=_REFUSED))
    mantle = _Mantle({
        "anthropic.claude-opus-5-5": CountTokensResult(403, 0, "HTTP403", "access_denied"),
        "anthropic.claude-opus-5": CountTokensResult(200, 1045),
    })
    app = _app(bedrock, _cfg("opus55", "global.anthropic.claude-opus-5-5"), mantle=mantle)
    resp = await _post(app, _msg())
    assert resp.status_code == 200 and resp.json() == {"input_tokens": 1045}
    mantle.calls.clear()
    await _post(app, _msg())
    assert "anthropic.claude-opus-5-5" not in [c["model"] for c in mantle.calls]


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [
    CountTokensResult(500, 0, "HTTP500", "internal"),
    CountTokensResult(429, 0, "HTTP429", "slow down"),
])
async def test_transient_upstream_failures_are_not_remembered(fallback_on, answer):
    bedrock = MagicMock(count_tokens=AsyncMock(return_value=_REFUSED))
    mantle = _Mantle({"anthropic.claude-opus-5": answer})
    app = _app(bedrock, _cfg("opus5", "anthropic.claude-opus-5"), mantle=mantle)
    assert (await _post(app, _msg())).status_code == 400
    mantle.answers["anthropic.claude-opus-5"] = CountTokensResult(200, 10)
    assert (await _post(app, _msg())).json() == {"input_tokens": 10}


@pytest.mark.asyncio
async def test_unreachable_mantle_is_skipped_for_a_short_while(fallback_on):
    bedrock = MagicMock(count_tokens=AsyncMock(return_value=_REFUSED))
    unreachable = CountTokensResult(502, 0, "MantleUnreachable", "x")
    mantle = _Mantle({"anthropic.claude-opus-5": unreachable})
    app = _app(bedrock, _cfg("opus5", "anthropic.claude-opus-5"), mantle=mantle)
    await _post(app, _msg())
    await _post(app, _msg())
    assert len(mantle.calls) == 1  # the second count does not wait out a connect timeout


@pytest.mark.asyncio
async def test_one_slow_call_does_not_block_the_account(fallback_on):
    """A read timeout on one large prompt is that request's, not the endpoint's state."""
    bedrock = MagicMock(count_tokens=AsyncMock(return_value=_REFUSED))
    slow = CountTokensResult(502, 0, "MantleCallFailed", "x")
    mantle = _Mantle({"anthropic.claude-opus-5": slow})
    app = _app(bedrock, _cfg("opus5", "anthropic.claude-opus-5"), mantle=mantle)
    await _post(app, _msg())
    mantle.answers["anthropic.claude-opus-5"] = CountTokensResult(200, 3)
    assert (await _post(app, _msg())).json() == {"input_tokens": 3}


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_body", [
    {"model": "a", "messages": 5},
    {"model": "a", "messages": [{"role": "user", "content": [{"type": [], "text": "x"}]}]},
    {"model": "a", "messages": [{"role": "user", "content": [{"type": {"k": 1}}]}]},
])
async def test_malformed_bodies_are_never_a_gateway_500(fallback_on, bad_body):
    bedrock = MagicMock(count_tokens=AsyncMock(return_value=_REFUSED))
    tcf.BEDROCK_UNSUPPORTED.add("anthropic.claude-sonnet-5-5")  # the usual warm state
    mantle = _Mantle({})
    app = _app(bedrock, _cfg("sonnet55", "global.anthropic.claude-sonnet-5-5"), mantle=mantle)
    resp = await _post(app, bad_body)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_an_exception_in_the_fallback_is_a_400_not_a_500(fallback_on):
    bedrock = MagicMock(count_tokens=AsyncMock(return_value=_REFUSED))
    mantle = MagicMock(count_tokens=AsyncMock(side_effect=RuntimeError("unexpected")))
    app = _app(bedrock, _cfg("opus5", "anthropic.claude-opus-5"), mantle=mantle)
    resp = await _post(app, _msg())
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "invalid_request_error"


def _nested(depth: int) -> dict:
    content = [{"type": "text", "text": "leaf"}]
    for _ in range(depth):
        content = [{"type": "tool_result", "tool_use_id": "t", "content": content}]
    return {"model": "a", "messages": [{"role": "user", "content": content}]}


@pytest.mark.asyncio
async def test_deeply_nested_bodies_reach_bedrock_as_before():
    """origin/main sent this to Bedrock; a deepcopy of client content made it a 500."""
    bedrock = MagicMock(count_tokens=AsyncMock(return_value=CountTokensResult(200, 42)))
    app = _app(bedrock, _cfg("sonnet46", "anthropic.claude-sonnet-4-6"))
    resp = await _post(app, _nested(400))
    assert resp.status_code == 200 and resp.json() == {"input_tokens": 42}


@pytest.mark.asyncio
async def test_deeply_nested_bodies_are_counted_on_the_requested_model_only(fallback_on):
    bedrock = MagicMock(count_tokens=AsyncMock(return_value=_REFUSED))
    mantle = _Mantle({"anthropic.claude-sonnet-5-5": CountTokensResult(200, 5)})
    app = _app(bedrock, _cfg("sonnet55", "global.anthropic.claude-sonnet-5-5"), mantle=mantle)
    resp = await _post(app, _nested(400))
    assert resp.status_code == 200
    assert [c["model"] for c in mantle.calls] == ["anthropic.claude-sonnet-5-5"]


@pytest.mark.asyncio
async def test_a_sibling_400_stops_but_the_requested_model_400_does_not(fallback_on):
    bedrock = MagicMock(count_tokens=AsyncMock(return_value=_REFUSED))
    bad = CountTokensResult(400, 0, "HTTP400", "invalid_request_error")
    mantle = _Mantle({"anthropic.claude-sonnet-5-5": bad, "anthropic.claude-opus-5-5": bad,
                      "anthropic.claude-opus-5": CountTokensResult(200, 1)})
    app = _app(bedrock, _cfg("sonnet55", "global.anthropic.claude-sonnet-5-5"), mantle=mantle)
    assert (await _post(app, _msg())).status_code == 400
    # The requested model's 400 moved on; the first sibling's 400 ended the attempt —
    # the body would be refused everywhere and must not be uploaded seven times.
    assert [c["model"] for c in mantle.calls] == [
        "anthropic.claude-sonnet-5-5", "anthropic.claude-opus-5-5",
    ]


@pytest.mark.asyncio
async def test_fable_5_1_is_never_a_sibling_but_may_borrow_one(fallback_on):
    """Fable runs under the aws_review retention mode, approved for Fable traffic only."""
    bedrock = MagicMock(count_tokens=AsyncMock(return_value=_REFUSED))
    mantle = _Mantle({})
    app = _app(bedrock, _cfg("opus48", "global.anthropic.claude-opus-4-8"), mantle=mantle)
    await _post(app, _msg())
    assert "anthropic.claude-fable-5-1" not in [c["model"] for c in mantle.calls]

    tcf.clear_caches()  # the empty Mantle above marked every model missing
    mantle = _Mantle({"anthropic.claude-opus-5": CountTokensResult(200, 1941)})
    app = _app(bedrock, _cfg("fable51", "global.anthropic.claude-fable-5-1"), mantle=mantle)
    resp = await _post(app, _msg())
    assert resp.status_code == 200
    assert resp.headers["x-llm-gateway-token-count-model"] == "anthropic.claude-opus-5"


@pytest.mark.asyncio
async def test_images_and_documents_are_counted_on_the_requested_model_only(fallback_on):
    bedrock = MagicMock(count_tokens=AsyncMock(return_value=_REFUSED))
    mantle = _Mantle({"anthropic.claude-opus-5": CountTokensResult(200, 1)})
    app = _app(bedrock, _cfg("sonnet55", "global.anthropic.claude-sonnet-5-5"), mantle=mantle)
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "x"}}
    nested = {"type": "tool_result", "tool_use_id": "t", "content": [image]}
    for block in (image, nested):
        tcf.clear_caches()
        mantle.calls.clear()
        body = {"model": "a", "messages": [{"role": "user", "content": [block]}]}
        assert (await _post(app, body)).status_code == 400
        assert [c["model"] for c in mantle.calls] == ["anthropic.claude-sonnet-5-5"]


@pytest.mark.asyncio
async def test_a_message_that_merely_quotes_the_phrase_is_not_a_refusal():
    """Validation messages quote request field names; only AWS's whole sentence counts."""
    crafted = CountTokensResult(
        400, 0, "ValidationException",
        "messages.0.content.0.support counting tokens: Extra inputs are not permitted",
    )
    bedrock = MagicMock(count_tokens=AsyncMock(return_value=crafted))
    app = _app(bedrock, _cfg("sonnet46", "anthropic.claude-sonnet-4-6"))
    resp = await _post(app, _msg())
    assert resp.status_code == 400 and "Extra inputs" in resp.json()["error"]["message"]
    assert "anthropic.claude-sonnet-4-6" not in tcf.BEDROCK_UNSUPPORTED


@pytest.mark.asyncio
async def test_a_refusal_is_remembered_so_bedrock_is_not_asked_again(fallback_on):
    bedrock = MagicMock(count_tokens=AsyncMock(return_value=_REFUSED))
    mantle = _Mantle({"anthropic.claude-opus-5": CountTokensResult(200, 10)})
    app = _app(bedrock, _cfg("opus5", "anthropic.claude-opus-5"), mantle=mantle)
    await _post(app, _msg())
    await _post(app, _msg())
    assert bedrock.count_tokens.await_count == 1


@pytest.mark.asyncio
async def test_other_upstream_errors_carry_the_real_reason():
    denied = CountTokensResult(
        403, 0, "AccessDeniedException", "not authorized to perform: bedrock:CountTokens"
    )
    bedrock = MagicMock(count_tokens=AsyncMock(return_value=denied))
    app = _app(bedrock, _cfg("sonnet46", "anthropic.claude-sonnet-4-6"))
    resp = await _post(app, _msg())
    assert resp.status_code == 403
    assert resp.json()["error"] == {
        "type": "permission_error",
        "message": "not authorized to perform: bedrock:CountTokens",
    }


@pytest.mark.asyncio
async def test_a_plain_tuple_adapter_still_works():
    bedrock = MagicMock(count_tokens=AsyncMock(return_value=(429, 0)))
    app = _app(bedrock, _cfg("sonnet46", "anthropic.claude-sonnet-4-6"))
    resp = await _post(app, _msg())
    assert resp.status_code == 429
    assert resp.json()["error"]["type"] == "rate_limit_error"


# ── which account and region Mantle counts in ────────────────────────────────────


def _refusing_boto_client():
    client = MagicMock()
    client.count_tokens.side_effect = ClientError(
        {"Error": {"Code": "ValidationException",
                   "Message": "The provided model doesn't support counting tokens."}},
        "CountTokens",
    )
    return client


@pytest.mark.asyncio
async def test_cross_account_profile_counts_in_the_profile_account_and_region(fallback_on):
    """The real cross-account BedrockAdapter is built (provider wired, as in main.py)."""
    xacct_client = _refusing_boto_client()
    provider = MagicMock(get_client=AsyncMock(return_value=xacct_client))
    inacct = MagicMock(count_tokens=AsyncMock(side_effect=AssertionError("pod account used")))
    mantle = _Mantle({"anthropic.claude-opus-5": CountTokensResult(200, 7)})
    profile = RoutingProfileSchema(
        client="claude-code", backend="invoke", region="us-west-2",
        account_role_arn="arn:aws:iam::111111111111:role/x", external_id="ext",
    )
    app = _app(inacct, _cfg("opus5", "anthropic.claude-opus-5"), mantle=mantle,
               profile=profile, account_provider=provider)
    assert (await _post(app, _msg())).status_code == 200
    provider.get_client.assert_awaited_with("arn:aws:iam::111111111111:role/x", "us-west-2", "ext")
    assert xacct_client.count_tokens.call_count == 1
    call = mantle.calls[0]
    assert call["endpoint"] == "https://bedrock-mantle.us-west-2.api.aws/anthropic"
    assert call["profile"].region == "us-west-2"
    assert call["profile"].account_role_arn == "arn:aws:iam::111111111111:role/x"
    assert call["profile"].external_id == "ext"
    assert call["account"] is None


@pytest.mark.asyncio
async def test_alias_account_wins_over_the_profile(fallback_on):
    bedrock = MagicMock(count_tokens=AsyncMock(return_value=_REFUSED))
    mantle = _Mantle({"anthropic.claude-opus-5": CountTokensResult(200, 7)})
    cfg = _cfg("opus5", "anthropic.claude-opus-5",
               account_role_arn="arn:aws:iam::222222222222:role/alias")
    app = _app(bedrock, cfg, mantle=mantle)
    assert (await _post(app, _msg())).status_code == 200
    call = mantle.calls[0]
    assert call["account"].role_arn == "arn:aws:iam::222222222222:role/alias"
    assert call["profile"].account_role_arn is None
    # Same account for the Bedrock attempt — invoke and count stay on one account.
    assert bedrock.count_tokens.call_args.kwargs["account"].role_arn.endswith("role/alias")


# ── helpers ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("pmid", "base"), [
    ("global.anthropic.claude-sonnet-5", "anthropic.claude-sonnet-5"),
    ("apac.anthropic.claude-sonnet-4-20250514-v1:0", "anthropic.claude-sonnet-4-20250514-v1:0"),
    ("jp.anthropic.claude-opus-4-8", "anthropic.claude-opus-4-8"),
    ("arn:aws:bedrock:ap-northeast-2::inference-profile/global.anthropic.claude-opus-5",
     "anthropic.claude-opus-5"),
    ("anthropic.claude-haiku-4-5-20251001-v1:0", "anthropic.claude-haiku-4-5-20251001-v1:0"),
    ("meta.llama3-70b-instruct-v1:0", "meta.llama3-70b-instruct-v1:0"),
])
def test_count_base_model_id(pmid, base):
    assert tcf.count_base_model_id(pmid) == base


@pytest.mark.parametrize("body", [
    {"messages": 5},
    {"messages": [None, "x", {"content": "plain string"}]},
    {"messages": [{"content": [{"type": []}]}]},
    {"messages": [{"content": [{"type": {"k": 1}}]}]},
])
def test_candidate_selection_tolerates_any_body_shape(body):
    """Never raises on client data; anything odd is counted on the requested model only."""
    assert tcf.mantle_candidates("anthropic.claude-sonnet-5-5", body)[0] == (
        "anthropic.claude-sonnet-5-5"
    )


def test_candidate_selection_stops_at_a_depth_bound():
    deep = _nested(tcf._MAX_BLOCK_DEPTH + 5)
    assert tcf.mantle_candidates("anthropic.claude-sonnet-5-5", deep) == [
        "anthropic.claude-sonnet-5-5"
    ]
    shallow = _nested(3)
    assert len(tcf.mantle_candidates("anthropic.claude-sonnet-5-5", shallow)) > 1


def test_count_tokens_result_survives_copy_and_pickle():
    r = CountTokensResult(400, 0, "ValidationException", "m")
    for clone in (copy.copy(r), copy.deepcopy(r), pickle.loads(pickle.dumps(r))):
        assert tuple(clone) == (400, 0)
        assert (clone.error_code, clone.error_message) == ("ValidationException", "m")


@pytest.mark.asyncio
async def test_text_counter_uses_the_pod_account_and_region():
    mantle = _Mantle({"anthropic.claude-sonnet-5": CountTokensResult(200, 547)})
    count = tcf.make_text_counter(mantle, "ap-northeast-2")
    assert await count("abc", "global.anthropic.claude-sonnet-5") == 547
    call = mantle.calls[0]
    assert call["body"] == {
        "messages": [{"role": "user", "content": "abc"}], "model": "anthropic.claude-sonnet-5",
    }
    assert call["profile"].region == "ap-northeast-2"
    assert call["profile"].account_role_arn is None and call["account"] is None


def test_ttl_set_expires(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(tcf.time, "monotonic", lambda: now[0])
    s = tcf._TTLSet(10, max_entries=2)
    s.add("a")
    assert "a" in s
    now[0] = 111.0
    assert "a" not in s
    for key in ("b", "c", "d"):
        s.add(key)
    assert "d" in s  # bounded: never grows past max_entries


# ── Mantle adapter ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mantle_adapter_counts_on_the_count_endpoint():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"input_tokens": 771})

    broker = MagicMock(bearer_token=AsyncMock(return_value="tok"))
    adapter = MantleAdapter(httpx.AsyncClient(transport=httpx.MockTransport(handler)), broker)
    profile = RoutingProfileSchema(client="c", backend="mantle", region="ap-northeast-2")
    result = await adapter.count_tokens(
        b'{"model":"anthropic.claude-sonnet-5","messages":[]}', "anthropic.claude-sonnet-5",
        profile=profile, endpoint="https://bedrock-mantle.ap-northeast-2.api.aws/anthropic",
    )
    assert tuple(result) == (200, 771)
    assert seen["url"].endswith("/anthropic/v1/messages/count_tokens")
    assert seen["auth"] == "Bearer tok"


@pytest.mark.asyncio
@pytest.mark.parametrize(("exc", "code"), [
    (httpx.ConnectError("refused"), "MantleUnreachable"),
    (httpx.ConnectTimeout("syn"), "MantleUnreachable"),
    (httpx.ReadTimeout("slow"), "MantleCallFailed"),
    (httpx.PoolTimeout("full"), "MantleCallFailed"),
])
async def test_mantle_adapter_tells_unreachable_from_a_failed_call(exc, code):
    def handler(request):
        raise exc

    adapter = MantleAdapter(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        MagicMock(bearer_token=AsyncMock(return_value="tok")),
    )
    profile = RoutingProfileSchema(client="c", backend="mantle", region="ap-northeast-2")
    result = await adapter.count_tokens(b"{}", "m", profile=profile, endpoint="https://x/a")
    assert result[0] == 502 and result.error_code == code


@pytest.mark.asyncio
async def test_mantle_adapter_reports_failures_instead_of_a_zero_count():
    adapter = MantleAdapter(
        httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(404, json={"error": {"type": "not_found_error"}}))),
        MagicMock(bearer_token=AsyncMock(return_value="tok")),
    )
    profile = RoutingProfileSchema(client="c", backend="mantle", region="ap-northeast-2")
    r404 = await adapter.count_tokens(b"{}", "m", profile=profile, endpoint="https://x/anthropic")
    assert r404[0] == 404 and r404.error_code == "HTTP404"
    # The old stub answered (200, 0) — a silent zero for any caller.
    misconfigured = await adapter.count_tokens(b"{}", "m")
    assert misconfigured[0] == 500


# ── cut-stream estimates (billing) ────────────────────────────────────────────────


_refusing_bedrock_client = _refusing_boto_client


@pytest.mark.asyncio
async def test_cut_stream_estimate_uses_mantle_when_bedrock_refuses():
    bedrock_client = _refusing_bedrock_client()
    mantle_count = AsyncMock(return_value=547)
    tok = TokenizerService(bedrock_client=bedrock_client, mantle_count=mantle_count)
    n = await tok.estimate_output_tokens(
        "text", provider=ProviderType.BEDROCK, model_id="global.anthropic.claude-sonnet-5"
    )
    assert n == 547
    mantle_count.assert_awaited_once_with("text", "global.anthropic.claude-sonnet-5")
    await tok.estimate_output_tokens(
        "text", provider=ProviderType.BEDROCK, model_id="global.anthropic.claude-sonnet-5"
    )
    assert bedrock_client.count_tokens.call_count == 1  # refusal remembered


@pytest.mark.asyncio
async def test_cut_stream_estimate_asks_mantle_only_after_a_capability_refusal():
    throttled = MagicMock()
    throttled.count_tokens.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "CountTokens"
    )
    mantle_count = AsyncMock(return_value=999)
    tok = TokenizerService(bedrock_client=throttled, mantle_count=mantle_count)
    n = await tok.estimate_output_tokens(
        "hello", provider=ProviderType.BEDROCK, model_id="anthropic.claude-sonnet-4-6"
    )
    assert n != 999 and mantle_count.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["raises", "hangs"])
async def test_a_failing_or_slow_mantle_never_costs_the_estimate(monkeypatch, failure):
    """The usage row is written after this estimate; a stuck Mantle must not block it."""
    from app.services import tokenizer as tokenizer_module

    monkeypatch.setattr(tokenizer_module, "_MANTLE_COUNT_BUDGET_SECONDS", 0.05)

    async def broken(text, model_id):
        if failure == "raises":
            raise RuntimeError("boom")
        await asyncio.sleep(10)

    tok = TokenizerService(bedrock_client=_refusing_bedrock_client(), mantle_count=broken)
    n = await asyncio.wait_for(tok.estimate_output_tokens(
        "hello world", provider=ProviderType.BEDROCK, model_id="anthropic.claude-sonnet-5"
    ), timeout=2)
    assert isinstance(n, int) and n > 0  # tiktoken approximation, not None


@pytest.mark.asyncio
async def test_cut_stream_estimate_without_mantle_still_falls_back_to_tiktoken():
    tok = TokenizerService(bedrock_client=_refusing_bedrock_client())
    n = await tok.estimate_output_tokens(
        "hello world", provider=ProviderType.BEDROCK, model_id="anthropic.claude-sonnet-5"
    )
    assert isinstance(n, int) and n > 0
