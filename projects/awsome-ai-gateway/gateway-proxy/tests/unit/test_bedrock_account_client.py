# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Unit tests for cross-account Bedrock native (claude-code → 374).

Covers: BedrockAccountClientProvider assume+cache+expiry-rebuild, and
BedrockAdapter's client_resolver + transparent 859 fallback on resolver failure.
"""
from __future__ import annotations

import datetime

import pytest

from app.providers.bedrock_adapter import BedrockAdapter
from app.services.bedrock_account_client import BedrockAccountClientProvider


class _FakeSTS:
    def __init__(self, expiry_epoch: float):
        self.calls = 0
        self._expiry = expiry_epoch

    def assume_role(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        return {
            "Credentials": {
                "AccessKeyId": f"ASIA{self.calls}",
                "SecretAccessKey": "sk",
                "SessionToken": "tok",
                "Expiration": datetime.datetime.fromtimestamp(self._expiry, tz=datetime.timezone.utc),
            }
        }


@pytest.mark.asyncio
async def test_provider_assumes_and_caches(monkeypatch):
    now = [1000.0]
    sts = _FakeSTS(expiry_epoch=1000.0 + 3600)
    built = []
    prov = BedrockAccountClientProvider(sts_client=sts, boto_config=object(), now=lambda: now[0])
    # boto3.client 를 가짜로
    import app.services.bedrock_account_client as mod
    monkeypatch.setattr(mod.boto3, "client", lambda svc, **kw: {"svc": svc, "region": kw.get("region_name"), "creds": kw.get("aws_access_key_id")})

    c1 = await prov.get_client("arn:aws:iam::345678901234:role/x", "ap-northeast-2", "ext-id")
    assert c1["svc"] == "bedrock-runtime" and c1["region"] == "ap-northeast-2"
    assert sts.calls == 1
    assert sts.last_kwargs["ExternalId"] == "ext-id"
    assert sts.last_kwargs["RoleArn"].endswith(":role/x")

    # 캐시 적중 (같은 role+region) → assume 재호출 안 함
    c2 = await prov.get_client("arn:aws:iam::345678901234:role/x", "ap-northeast-2", "ext-id")
    assert sts.calls == 1 and c2 is c1

    # 다른 region → 별도 클라이언트
    c3 = await prov.get_client("arn:aws:iam::345678901234:role/x", "us-east-1", "ext-id")
    assert sts.calls == 2 and c3 is not c1 and c3["region"] == "us-east-1"


@pytest.mark.asyncio
async def test_provider_rebuilds_near_expiry(monkeypatch):
    now = [1000.0]
    sts = _FakeSTS(expiry_epoch=1000.0 + 3600)  # expires at 4600
    prov = BedrockAccountClientProvider(sts_client=sts, boto_config=object(), now=lambda: now[0])
    import app.services.bedrock_account_client as mod
    monkeypatch.setattr(mod.boto3, "client", lambda svc, **kw: {"creds": kw.get("aws_access_key_id")})

    c1 = await prov.get_client("r", "ap-northeast-2", None)
    assert sts.calls == 1
    # 만료 skew(300s) 안쪽으로 시간 진행 → 재빌드
    now[0] = 1000.0 + 3600 - 200  # within refresh skew
    sts._expiry = now[0] + 3600
    c2 = await prov.get_client("r", "ap-northeast-2", None)
    assert sts.calls == 2 and c2["creds"] != c1["creds"]


@pytest.mark.asyncio
async def test_adapter_backward_compat_inaccount():
    # resolver 없으면 기존 고정 클라이언트 그대로 (zero-regression)
    a = BedrockAdapter("in-account-client")
    got = await a._get_client()
    assert got == "in-account-client"


@pytest.mark.asyncio
async def test_adapter_resolver_used_when_present():
    async def resolver():
        return "xacct-374-client"
    a = BedrockAdapter(bedrock_client=None, client_resolver=resolver, fallback_client="in-account-859")
    got = await a._get_client()
    assert got == "xacct-374-client"


@pytest.mark.asyncio
async def test_adapter_transparent_fallback_on_resolver_failure():
    # 핵심 안전장치: assume 실패 시 859 로 투명 폴백 (claude-code 안 죽음)
    async def failing_resolver():
        raise RuntimeError("assume failed / bad trust")
    a = BedrockAdapter(bedrock_client=None, client_resolver=failing_resolver, fallback_client="in-account-859")
    got = await a._get_client()
    assert got == "in-account-859"


@pytest.mark.asyncio
async def test_adapter_resolver_failure_no_fallback_raises():
    async def failing_resolver():
        raise RuntimeError("boom")
    a = BedrockAdapter(bedrock_client=None, client_resolver=failing_resolver, fallback_client=None)
    with pytest.raises(RuntimeError):
        await a._get_client()
