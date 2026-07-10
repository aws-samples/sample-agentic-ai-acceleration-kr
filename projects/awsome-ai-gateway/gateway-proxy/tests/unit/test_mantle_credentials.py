# Copyright 2026 © Amazon.com and Affiliates.
from __future__ import annotations

import datetime as _dt
from unittest.mock import MagicMock, patch

import pytest

from app.schemas.routing import RoutingProfileSchema
from app.services.mantle_credentials import MantleCredentialBroker


def _profile():
    return RoutingProfileSchema(
        client="cowork", backend="mantle",
        account_role_arn="arn:aws:iam::234567890123:role/llm-gateway-cowork-bedrock",
        region="ap-northeast-1", default_model="cowork-opus", external_id="cowork-bedrock",
    )


def _future():
    return _dt.datetime(2099, 1, 1, tzinfo=_dt.timezone.utc)


class _Clock:
    def __init__(self, start: float):
        self._t = start
    def now(self) -> float:
        return self._t
    def advance(self, secs: float):
        self._t += secs


@pytest.mark.asyncio
async def test_bearer_token_assumes_role_then_mints_token():
    sts = MagicMock()
    sts.assume_role.return_value = {
        "Credentials": {
            "AccessKeyId": "ASIAEXAMPLE", "SecretAccessKey": "secret",
            "SessionToken": "token", "Expiration": _future(),
        }
    }
    clock = _Clock(start=1000.0)
    with patch("app.services.mantle_credentials.BedrockTokenGenerator") as GenCls:
        GenCls.return_value.get_token.return_value = "bedrock-api-key-abc"
        broker = MantleCredentialBroker(sts_client=sts, now=clock.now)
        token = await broker.bearer_token(_profile())

    assert token == "bedrock-api-key-abc"
    sts.assume_role.assert_called_once()
    kwargs = sts.assume_role.call_args.kwargs
    assert kwargs["RoleArn"].endswith("llm-gateway-cowork-bedrock")
    assert kwargs["ExternalId"] == "cowork-bedrock"


@pytest.mark.asyncio
async def test_assumed_creds_are_cached_across_calls():
    sts = MagicMock()
    sts.assume_role.return_value = {
        "Credentials": {
            "AccessKeyId": "ASIA", "SecretAccessKey": "s", "SessionToken": "t",
            "Expiration": _future(),
        }
    }
    clock = _Clock(start=1000.0)
    with patch("app.services.mantle_credentials.BedrockTokenGenerator") as GenCls:
        GenCls.return_value.get_token.return_value = "bedrock-api-key-abc"
        broker = MantleCredentialBroker(sts_client=sts, now=clock.now)
        await broker.bearer_token(_profile())
        clock.advance(60)              # 1 min later, well within TTL
        await broker.bearer_token(_profile())

    sts.assume_role.assert_called_once()   # creds reused, not re-assumed


@pytest.mark.asyncio
async def test_bearer_expiry_never_outlives_assumed_creds():
    # Creds expire soon (now+600s); bearer TTL is 1800s. The cached bearer must
    # be capped at creds_expiry - skew, NOT now+1800 (else it would 401 once the
    # creds expire).
    clock = _Clock(start=1000.0)
    creds_expiry_epoch = clock.now() + 600  # creds live only 10 min
    exp_dt = _dt.datetime.fromtimestamp(creds_expiry_epoch, tz=_dt.timezone.utc)
    sts = MagicMock()
    sts.assume_role.return_value = {
        "Credentials": {
            "AccessKeyId": "ASIA", "SecretAccessKey": "s", "SessionToken": "t",
            "Expiration": exp_dt,
        }
    }
    with patch("app.services.mantle_credentials.BedrockTokenGenerator") as GenCls:
        GenCls.return_value.get_token.return_value = "bedrock-api-key-abc"
        broker = MantleCredentialBroker(sts_client=sts, now=clock.now)
        await broker.bearer_token(_profile())

    cached = broker._bearers[(_profile().account_role_arn, "ap-northeast-1")]
    # bearer expiry capped to creds_expiry - 60, which is < now+1800
    assert cached.expires_at == creds_expiry_epoch - 60
    assert cached.expires_at < clock.now() + 1800


@pytest.mark.asyncio
async def test_bearer_cache_keyed_by_region():
    # Same role ARN, two regions → two distinct bearers (region-bound tokens).
    sts = MagicMock()
    sts.assume_role.return_value = {
        "Credentials": {
            "AccessKeyId": "ASIA", "SecretAccessKey": "s", "SessionToken": "t",
            "Expiration": _future(),
        }
    }
    clock = _Clock(start=1000.0)
    with patch("app.services.mantle_credentials.BedrockTokenGenerator") as GenCls:
        GenCls.return_value.get_token.side_effect = ["tok-tokyo", "tok-seoul"]
        broker = MantleCredentialBroker(sts_client=sts, now=clock.now)
        p_tokyo = _profile()
        p_seoul = _profile()
        p_seoul.region = "ap-northeast-2"
        t1 = await broker.bearer_token(p_tokyo)
        t2 = await broker.bearer_token(p_seoul)

    assert t1 == "tok-tokyo"
    assert t2 == "tok-seoul"          # region-specific, not the cached tokyo token
    # creds re-used (same role) so assume_role only once
    sts.assume_role.assert_called_once()
    # but the token generator was invoked per region
    assert GenCls.return_value.get_token.call_count == 2


@pytest.mark.asyncio
async def test_in_account_branch_skips_assume_role(monkeypatch):
    """account_role_arn=None → no STS assume_role; bearer minted from pod creds."""
    from app.services import mantle_credentials as mc

    sts = MagicMock()
    sts.assume_role = MagicMock(side_effect=AssertionError("assume_role must not be called in-account"))
    broker = mc.MantleCredentialBroker(sts_client=sts, now=lambda: 1000.0)

    fake_creds = MagicMock()
    monkeypatch.setattr(broker, "_in_account_creds", lambda: fake_creds)
    captured = {}

    class _Gen:
        def get_token(self, creds, region):
            captured["creds"] = creds
            captured["region"] = region
            return "TOKEN-INACCT"

    monkeypatch.setattr(mc, "BedrockTokenGenerator", lambda: _Gen())

    profile = RoutingProfileSchema(client="claude-code", backend="mantle",
                                   account_role_arn=None, region="ap-northeast-1",
                                   default_model=None, external_id=None, enabled=True)
    token = await broker.bearer_token(profile)
    assert token == "TOKEN-INACCT"
    assert captured["region"] == "ap-northeast-1"
    assert captured["creds"] is fake_creds
    sts.assume_role.assert_not_called()


@pytest.mark.asyncio
async def test_cross_account_branch_still_assumes_role(monkeypatch):
    """Regression guard: account_role_arn set → existing AssumeRole path (cowork)."""
    from app.services import mantle_credentials as mc
    import datetime

    sts = MagicMock()
    sts.assume_role = MagicMock(return_value={"Credentials": {
        "AccessKeyId": "AK", "SecretAccessKey": "SK", "SessionToken": "ST",
        "Expiration": datetime.datetime.fromtimestamp(9999, tz=datetime.timezone.utc)}})
    broker = mc.MantleCredentialBroker(sts_client=sts, now=lambda: 1000.0)
    monkeypatch.setattr(mc, "BedrockTokenGenerator", lambda: type("G", (), {"get_token": lambda self, c, r: "TOK"})())
    profile = RoutingProfileSchema(client="cowork", backend="mantle",
                                   account_role_arn="arn:aws:iam::234567890123:role/llm-gateway-cowork-bedrock",
                                   region="ap-northeast-1", default_model="cowork-opus",
                                   external_id="cowork-bedrock", enabled=True)
    token = await broker.bearer_token(profile)
    assert token == "TOK"
    sts.assume_role.assert_called_once()
