# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""라우팅 프로필의 자격증명 캐시는 (역할, ExternalId, 세션 이름)마다 따로다.

무엇이 문제였나
---------------
runtime ``SigV4Signer`` 와 Mantle ``MantleCredentialBroker`` 의 프로필 경로는 assume 한 자격증명을
**역할 ARN 만으로** 캐시했다(Mantle 은 bearer 도 (역할, 리전)으로). 같은 역할을 다른 ExternalId 로
지목한 두 프로필이 있으면, 뒤 프로필은 STS 를 부르지 않고 앞 프로필의 ExternalId 로 assume 한
자격증명(bearer)을 받았다. ExternalId 는 대상 계정의 신뢰 정책이 confused-deputy 를 막으려고
요구하는 값인데, 캐시 적중이 그 검사를 매번 건너뛴다. alias 경로(``BedrockAccountClientProvider``)와
claude-code native 프로필 경로는 이미 (역할, ExternalId, 세션 이름)으로 키를 잡는다 — 이제 세 경로가
같다.

대역은 STS 하나(호출마다 다른 키를 돌려 어느 AssumeRole 의 자격증명인지 구별한다). 서명기·브로커·
프로바이더는 실물이다.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock

import pytest

from app.providers.bedrock_adapter import BedrockAdapter
from app.schemas.routing import RoutingProfileSchema
from app.services import mantle_credentials as mc
from app.services.bedrock_account_client import BedrockAccountClientProvider
from app.services.sigv4_signer import SigV4Signer

ROLE = "arn:aws:iam::444455556666:role/shared-app-bedrock"
RUNTIME_URL = "https://bedrock-runtime.us-east-2.amazonaws.com/openai/v1/responses"


class _STS:
    """호출마다 다른 키(ASIA1, ASIA2 …)를 돌려준다. 받은 ExternalId 를 기록한다.

    ``lifetime`` 을 주면 그 ExternalId 의 자격증명은 (지금 + lifetime) 에 끝난다(없으면 2099년).
    """

    def __init__(self, clock=None, lifetime=None) -> None:
        self.external_ids: list[str | None] = []
        self.clock = clock
        self.lifetime = lifetime or {}

    def assume_role(self, **kw):
        ext = kw.get("ExternalId")
        self.external_ids.append(ext)
        n = len(self.external_ids)
        if ext in self.lifetime:
            expires = dt.datetime.fromtimestamp(self.clock[0] + self.lifetime[ext], dt.UTC)
        else:
            expires = dt.datetime(2099, 1, 1, tzinfo=dt.UTC)
        return {
            "Credentials": {
                "AccessKeyId": f"ASIA{n}",
                "SecretAccessKey": "s",
                "SessionToken": f"t{n}",
                "Expiration": expires,
            }
        }


def _profile(ext: str | None):
    return RoutingProfileSchema(
        client="cowork",
        backend="mantle",
        account_role_arn=ROLE,
        region="us-west-2",
        default_model=None,
        external_id=ext,
        enabled=True,
    )


async def _runtime_key_id(signer: SigV4Signer, ext: str | None) -> str:
    headers = await signer.sign(
        method="POST",
        url=RUNTIME_URL,
        body=b"{}",
        region="us-east-2",
        role_arn=ROLE,
        external_id=ext,
    )
    # Authorization: AWS4-HMAC-SHA256 Credential=<access key>/<date>/<region>/bedrock/…
    return headers["Authorization"].split("Credential=")[1].split("/")[0]


@pytest.fixture
def _token_gen(monkeypatch):
    class _Gen:
        def get_token(self, creds, region):
            return f"BEARER-{creds.access_key}-{region}"

    monkeypatch.setattr(mc, "BedrockTokenGenerator", lambda: _Gen())


async def test_runtime_profiles_with_different_external_ids_get_separate_credentials():
    """같은 역할, ExternalId a·b → STS 두 번(각자의 ExternalId), 서로 다른 자격증명으로 서명. 다시
    서명하면 각자 자기 캐시에서 — STS 는 더 부르지 않는다. ExternalId 없는 프로필도 따로다."""
    sts = _STS()
    signer = SigV4Signer(sts_client=sts, now=lambda: 1000.0)
    assert await _runtime_key_id(signer, "ext-a") == "ASIA1"
    assert await _runtime_key_id(signer, "ext-b") == "ASIA2"
    assert await _runtime_key_id(signer, None) == "ASIA3"
    assert sts.external_ids == ["ext-a", "ext-b", None]
    assert [await _runtime_key_id(signer, e) for e in ("ext-a", "ext-b", None)] == [
        "ASIA1", "ASIA2", "ASIA3"
    ]
    assert len(sts.external_ids) == 3


async def test_mantle_profiles_with_different_external_ids_get_separate_bearers(_token_gen):
    """같은 역할·리전, ExternalId a·b → STS 두 번, bearer 도 둘(각자의 자격증명으로 mint). 다시
    부르면 각자 자기 bearer — STS 는 더 부르지 않는다."""
    sts = _STS()
    broker = mc.MantleCredentialBroker(sts_client=sts, now=lambda: 1000.0)
    a = await broker.bearer_token(_profile("ext-a"))
    b = await broker.bearer_token(_profile("ext-b"))
    assert (a, b) == ("BEARER-ASIA1-us-west-2", "BEARER-ASIA2-us-west-2")
    assert sts.external_ids == ["ext-a", "ext-b"]
    assert await broker.bearer_token(_profile("ext-a")) == a
    assert await broker.bearer_token(_profile("ext-b")) == b
    assert len(sts.external_ids) == 2


async def test_a_reassume_for_one_external_id_keeps_the_other_ones_bearer(monkeypatch):
    """ExternalId a 의 자격증명을 다시 assume 하면 a 의 bearer 만 무효화된다 — b 의 bearer 는 그대로
    다시 쓰인다(새로 mint 하지 않는다). a 의 자격증명은 10분, b 는 1시간짜리다."""
    minted: list[str] = []

    class _Gen:
        def get_token(self, creds, region):
            minted.append(creds.access_key)
            return f"BEARER-{creds.access_key}-{region}"

    monkeypatch.setattr(mc, "BedrockTokenGenerator", lambda: _Gen())
    now = [1000.0]
    sts = _STS(clock=now, lifetime={"ext-a": 600, "ext-b": 3600})
    broker = mc.MantleCredentialBroker(sts_client=sts, now=lambda: now[0])
    b = await broker.bearer_token(_profile("ext-b"))
    await broker.bearer_token(_profile("ext-a"))
    assert minted == ["ASIA1", "ASIA2"]

    now[0] = 1545.0  # a 의 bearer(자격증명 만료 −60초 = 1540)가 끝났고, a 의 자격증명은 갱신 창
    a2 = await broker.bearer_token(_profile("ext-a"))
    assert a2 == "BEARER-ASIA3-us-west-2" and sts.external_ids == ["ext-b", "ext-a", "ext-a"]
    assert await broker.bearer_token(_profile("ext-b")) == b
    assert minted == ["ASIA1", "ASIA2", "ASIA3"], "b 의 bearer 가 a 의 재assume 에 휩쓸렸다"


async def test_a_reassume_invalidates_that_keys_bearers_in_every_region(monkeypatch):
    """한 리전의 요청이 자격증명을 다시 assume 하면, 같은 키로 다른 리전에 이미 mint 해 둔 bearer 도
    버린다 — 그 리전의 다음 요청은 새 자격증명으로 다시 mint 한다. 옛 자격증명의 bearer 를 그
    TTL(옛 자격증명 만료 − 60초)까지 계속 내주지 않는다. 자격증명은 10분짜리다.

    이 무효화(``_get_creds`` 의 ``k[0] != key``)가 깨져도 위 테스트들은 통과한다 — 위 재assume
    테스트에서는 무효화할 bearer 가 이미 만료돼 있다.
    """
    minted: list[tuple[str, str]] = []

    class _Gen:
        def get_token(self, creds, region):
            minted.append((creds.access_key, region))
            return f"BEARER-{creds.access_key}-{region}"

    monkeypatch.setattr(mc, "BedrockTokenGenerator", lambda: _Gen())
    now = [1000.0]
    sts = _STS(clock=now, lifetime={"ext-a": 600})
    broker = mc.MantleCredentialBroker(sts_client=sts, now=lambda: now[0])
    west, east = _profile("ext-a"), _profile("ext-a")
    east.region = "us-east-2"

    assert await broker.bearer_token(west) == "BEARER-ASIA1-us-west-2"  # bearer: 1540 까지
    now[0] = 1350.0  # 자격증명(1600 만료)은 갱신 창, us-west-2 의 bearer 는 아직 유효
    assert await broker.bearer_token(east) == "BEARER-ASIA2-us-east-2"  # 재assume
    now[0] = 1351.0
    assert await broker.bearer_token(west) == "BEARER-ASIA2-us-west-2", "옛 자격증명의 bearer"
    assert minted == [("ASIA1", "us-west-2"), ("ASIA2", "us-east-2"), ("ASIA2", "us-west-2")]
    assert len(sts.external_ids) == 2


async def test_native_profiles_with_different_external_ids_get_separate_clients(monkeypatch):
    """claude-code native 프로필 경로(공유 provider)는 이미 (역할, ExternalId, 세션 이름)으로 키를
    잡는다 — 세 경로가 같은 규칙인지 같이 본다."""
    built: list[str] = []

    def _client(*_a, **kw):
        built.append(kw["aws_access_key_id"])
        return MagicMock(name=kw["aws_access_key_id"])

    monkeypatch.setattr("app.services.bedrock_account_client.boto3.client", _client)
    sts = _STS()
    provider = BedrockAccountClientProvider(sts_client=sts, boto_config=None, now=lambda: 1000.0)

    async def _get(ext):
        adapter = BedrockAdapter(
            bedrock_client=None,
            client_resolver=lambda: provider.get_client(ROLE, "ap-northeast-2", ext),
            fallback_client=object(),
        )
        return await adapter._get_client()

    a, b = await _get("ext-a"), await _get("ext-b")
    assert a is not b and built == ["ASIA1", "ASIA2"]
    assert sts.external_ids == ["ext-a", "ext-b"]
    assert await _get("ext-a") is a and await _get("ext-b") is b
