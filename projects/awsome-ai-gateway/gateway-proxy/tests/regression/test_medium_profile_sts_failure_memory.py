# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""라우팅 프로필 경로의 짧은 AssumeRole 실패 기억.

무엇이 문제였나
---------------
라우팅 프로필 경로 셋 — claude-code native(``BedrockAccountClientProvider.get_client``, 실패 시
파드 계정으로 투명 폴백), runtime ``SigV4Signer._resolve_creds``, Mantle
``MantleCredentialBroker.bearer_token`` — 에는 실패 기억이 없었다. 같은 키의 동시 요청은 lock
뒤에서 차례로 STS 를 다시 불러 k 번째 요청이 k × T 를 기다렸다(T = AssumeRole 한 번이 실패하기까지의
시간, botocore 기본 설정이면 응답 없는 STS 에서 약 315초 — ``test_medium_sts_client_timeouts.py``).
그래서 STS 가 멈추면 claude-code 의 "절대 안 죽는" 폴백이 k × T 뒤에야 걸렸다.

고친 것
-------
* 세 프로필 경로: 실패를 ``PROFILE_FAILURE_BACKOFF``(5초) 동안 (역할, ExternalId, 세션 이름)마다
  기억한다. 시계는 실패를 기록할 때 읽는다. 그 동안 새 요청과 lock 뒤에서 기다리던 요청은 STS 를
  부르지 않고 그 경로의 오늘 폴백으로 간다(native = 파드 계정, runtime·Mantle = 502). 기억이
  끝나면 다음 요청 하나가 다시 부르고, 그 재시도가 진행 중일 때 온 같은 키의 다른 요청은 그
  결과를 최대 ``PROFILE_RETRY_WAIT``(1초) 기다린다 — 복구된 STS 면 모두 프로필 계정으로 가고,
  멈춘 STS 면 그 뒤 폴백한다.
* 갱신 창(만료 300초 전부터)에서 1분 넘게 남은 캐시 자격증명은, 기억 안이거나 갱신이 진행
  중이거나 갱신이 실패했을 때 그대로 쓴다(세 경로가 같은 규칙). 1분 이하로 남았거나 만료된
  자격증명은 쓰지 않는다.
* alias 경로는 그대로다 — 500 + 실패 시각부터 30초.

대역은 STS 하나뿐이다(제어 시계를 STS 안에서 쓴 만큼 민다). 프로바이더·서명기·브로커·어댑터는
실물이다.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import threading
import time
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ConnectTimeoutError

from app.providers.bedrock_adapter import BedrockAdapter
from app.providers.bedrock_openai_adapter import BedrockOpenAIAdapter
from app.providers.mantle_adapter import MantleAdapter
from app.schemas.routing import RoutingProfileSchema
from app.services import mantle_credentials as mc
from app.services.account_credentials import (
    ACCOUNT_UNAVAILABLE_STATUS,
    PROFILE_FAILURE_BACKOFF,
    PROFILE_RETRY_WAIT,
    SESSION_NAME_NATIVE,
    AccountCredentialsUnavailable,
    AccountTarget,
)
from app.services.bedrock_account_client import (
    FAILURE_BACKOFF,
    BedrockAccountClientProvider,
)
from app.services.sigv4_signer import SigV4Signer

ROLE = "arn:aws:iam::444455556666:role/app-bedrock"
OTHER_ROLE = "arn:aws:iam::111122223333:role/other-app"
EXT = "app-ext"
RUNTIME_ENDPOINT = "https://bedrock-runtime.us-east-2.amazonaws.com/openai"
STS_SECONDS = 35.0  # 실패하기까지 STS 안에서 쓰는 시간(제어 시계)


class _HungSTS:
    """STS 대역. 실패할 때 제어 시계를 ``seconds`` 만큼 밀고 ConnectTimeoutError 를 낸다.

    ``gate`` 가 있으면 그 이벤트가 설 때까지 호출 안에 머문다 — 다른 요청이 그동안 무엇을 하는지
    보려고. ``fail`` 을 끄면 그 시각부터 1시간짜리 자격증명을 돌려준다.
    """

    def __init__(self, clock: list[float], *, seconds: float = STS_SECONDS) -> None:
        self.clock = clock
        self.seconds = seconds
        self.fail = True
        self.gate: threading.Event | None = None
        self.calls: list[tuple[float, str, str | None, str]] = []

    def assume_role(self, **kw):
        self.calls.append(
            (self.clock[0], kw["RoleArn"], kw.get("ExternalId"), kw["RoleSessionName"])
        )
        if self.gate is not None:
            self.gate.wait(timeout=5)
        if not self.fail:
            n = len(self.calls)
            return {
                "Credentials": {
                    "AccessKeyId": f"ASIA{n}",
                    "SecretAccessKey": "s",
                    "SessionToken": f"t{n}",
                    "Expiration": dt.datetime.fromtimestamp(self.clock[0] + 3600, dt.UTC),
                }
            }
        self.clock[0] += self.seconds
        raise ConnectTimeoutError(endpoint_url="https://sts.ap-northeast-2.amazonaws.com")


def _profile(role=ROLE, ext=EXT, region="us-west-2"):
    return RoutingProfileSchema(
        client="cowork",
        backend="mantle",
        account_role_arn=role,
        region=region,
        default_model=None,
        external_id=ext,
        enabled=True,
    )


class _Path:
    """경로 하나를 실물로 조립한다. ``request()`` = 그 경로의 요청 하나 →
    ``"account"``(프로필 계정 자격증명) 또는 ``"fallback"``(그 경로의 오늘 폴백: native 는 파드 계정
    클라이언트, runtime·Mantle 은 자격증명 실패 → 어댑터 502)."""

    def __init__(self, name: str, sts: _HungSTS, monkeypatch) -> None:
        self.name = name
        self.sts = sts
        clock = sts.clock
        if name == "native":
            monkeypatch.setattr(
                "app.services.bedrock_account_client.boto3.client", lambda *a, **k: object()
            )
            self.provider = BedrockAccountClientProvider(
                sts_client=sts, boto_config=None, now=lambda: clock[0]
            )
        elif name == "runtime":
            self.signer = SigV4Signer(sts_client=sts, now=lambda: clock[0])
        else:

            class _Gen:
                def get_token(self, creds, region):
                    return f"BEARER-{creds.access_key}-{region}"

            monkeypatch.setattr(mc, "BedrockTokenGenerator", lambda: _Gen())
            self.broker = mc.MantleCredentialBroker(sts_client=sts, now=lambda: clock[0])

    async def request(self, *, role=ROLE, ext=EXT) -> str:
        if self.name == "native":
            pod = object()
            adapter = BedrockAdapter(
                bedrock_client=None,
                client_resolver=lambda: self.provider.get_client(role, "ap-northeast-2", ext),
                fallback_client=pod,
            )
            client = await adapter._get_client()
            return "fallback" if client is pod else "account"
        try:
            if self.name == "runtime":
                await self.signer.sign(
                    method="POST",
                    url=f"{RUNTIME_ENDPOINT}/v1/responses",
                    body=b"{}",
                    region="us-east-2",
                    role_arn=role,
                    external_id=ext,
                )
            else:
                await self.broker.bearer_token(_profile(role, ext))
        except Exception:
            return "fallback"
        return "account"


_PATHS = ["native", "runtime", "mantle"]


async def _until(cond, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("조건이 시간 안에 서지 않았다")
        await asyncio.sleep(0.005)


# ─────────────────────────────────────────────────────────────────────────────
# 1. 프로필 경로 셋 — 줄, 기억, 재시도
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", _PATHS)
async def test_only_the_first_queued_request_pays_the_sts_timeout(path, monkeypatch):
    """첫 요청이 STS 안에 있는 동안 같은 키로 셋이 줄을 선다. 첫 요청이 35초 뒤 실패하면 줄 선
    셋은 STS 를 부르지 않고 곧바로 폴백한다 — STS 호출 1번, 제어 시계는 35초만 간다.
    옛 코드는 줄 선 요청마다 STS 를 다시 불러 n 번째가 n × 35초를 기다렸다."""
    clock = [1000.0]
    sts = _HungSTS(clock)
    sts.gate = threading.Event()
    p = _Path(path, sts, monkeypatch)
    try:
        first = asyncio.create_task(p.request())
        await _until(lambda: sts.calls)
        queued = [asyncio.create_task(p.request()) for _ in range(3)]
        await asyncio.sleep(0.05)
        assert not any(q.done() for q in queued), "실패를 모르는 동안에는 진행 중인 호출을 기다린다"
    finally:
        sts.gate.set()
    assert await asyncio.gather(first, *queued) == ["fallback"] * 4
    assert [c[0] for c in sts.calls] == [1000.0], f"줄 선 요청이 STS 를 다시 불렀다: {sts.calls}"
    assert clock[0] == 1000.0 + STS_SECONDS, "줄 선 요청은 STS 시간을 한 초도 더 쓰지 않는다"


@pytest.mark.parametrize("path", _PATHS)
async def test_the_memory_runs_from_the_failure_and_the_next_request_retries(path, monkeypatch):
    """1000 에 시작해 35초 만에 실패 → 기억은 1035 부터 5초(1040 까지). 1036·1039.9 의 요청은
    STS 없이 폴백하고, 1040 의 요청이 다시 부른다. STS 가 돌아왔으면 그 요청부터 다시 프로필
    계정이다."""
    clock = [1000.0]
    sts = _HungSTS(clock)
    p = _Path(path, sts, monkeypatch)
    assert await p.request() == "fallback"
    assert clock[0] == 1035.0
    for at in (1036.0, 1035.0 + PROFILE_FAILURE_BACKOFF - 0.1):
        clock[0] = at
        assert await p.request() == "fallback"
    assert [c[0] for c in sts.calls] == [1000.0], f"기억 안에서 STS 를 불렀다: {sts.calls}"

    sts.fail = False
    clock[0] = 1035.0 + PROFILE_FAILURE_BACKOFF
    assert await p.request() == "account"
    assert await p.request() == "account"
    assert [c[0] for c in sts.calls] == [1000.0, 1035.0 + PROFILE_FAILURE_BACKOFF]


@pytest.mark.parametrize("path", _PATHS)
async def test_under_a_hung_sts_no_request_waits_longer_than_the_bound_for_a_retry(
    path, monkeypatch
):
    """기억이 끝나 한 요청이 다시 STS 를 부르는 중(응답 없음)일 때 같은 키의 다른 요청은 그 재시도를
    ``PROFILE_RETRY_WAIT`` 초까지만 기다리고 폴백한다 — STS 를 부르지도, 재시도의 타임아웃을
    기다리지도 않는다. 그렇지 않으면 STS 가 멈춰 있는 내내 재시도마다 줄이 다시 선다. 재시도하는
    요청 하나만 STS 시간을 쓴다(STS 클라이언트 타임아웃이 그 상한이다)."""
    clock = [1000.0]
    sts = _HungSTS(clock)
    p = _Path(path, sts, monkeypatch)
    assert await p.request() == "fallback"
    clock[0] = 1035.0 + PROFILE_FAILURE_BACKOFF
    sts.gate = threading.Event()

    async def _timed():
        started = time.monotonic()
        got = await p.request()
        return got, time.monotonic() - started

    try:
        retry = asyncio.create_task(p.request())
        await _until(lambda: len(sts.calls) == 2)
        late = await asyncio.wait_for(
            asyncio.gather(*(_timed() for _ in range(3))), timeout=PROFILE_RETRY_WAIT + 2
        )
        assert [got for got, _ in late] == ["fallback"] * 3
        waited = [round(s, 2) for _, s in late]
        bound = PROFILE_RETRY_WAIT + 0.5
        assert all(PROFILE_RETRY_WAIT * 0.9 <= s <= bound for s in waited), waited
        assert not retry.done(), "재시도는 아직 STS 안에 있다"
    finally:
        sts.gate.set()
    assert await retry == "fallback"
    assert len(sts.calls) == 2, f"재시도 중에 다른 요청이 STS 를 불렀다: {sts.calls}"


@pytest.mark.parametrize("path", _PATHS)
async def test_on_recovery_requests_that_arrive_during_the_retry_wait_for_it(path, monkeypatch):
    """STS 가 돌아온 뒤의 재시도 중에 온 같은 키의 요청은 그 재시도를 기다려 모두 프로필 계정으로
    간다(STS 는 재시도 한 번만). 예전에는 재시도가 끝나기 전에 온 요청이 전부 폴백했다 — native 는
    파드 계정으로 청구되고 runtime·Mantle 은 502 였다."""
    clock = [1000.0]
    sts = _HungSTS(clock)
    p = _Path(path, sts, monkeypatch)
    assert await p.request() == "fallback"
    clock[0] = 1035.0 + PROFILE_FAILURE_BACKOFF
    sts.fail = False
    sts.gate = threading.Event()
    try:
        retry = asyncio.create_task(p.request())
        await _until(lambda: len(sts.calls) == 2)
        during = [asyncio.create_task(p.request()) for _ in range(3)]
        await asyncio.sleep(0.2)
        assert not any(t.done() for t in during), "재시도를 잠깐 기다린다"
    finally:
        sts.gate.set()
    assert await asyncio.gather(retry, *during) == ["account"] * 4
    assert len(sts.calls) == 2, f"기다린 요청이 STS 를 불렀다: {sts.calls}"


# 갱신 창: 1000 에 받은 자격증명은 4600 에 끝난다(1시간). 갱신은 4300 부터.
_EXPIRY = 1000.0 + 3600


@pytest.mark.parametrize("path", _PATHS)
async def test_a_refresh_in_flight_does_not_hold_up_requests_with_usable_credentials(
    path, monkeypatch
):
    """갱신 창(200초 남음)에서 한 요청의 갱신이 멈춘 STS 에 걸려 있는 동안, 같은 키의 다른 요청은
    그 갱신을 기다리지 않고 캐시된 자격증명으로 곧바로 서명한다. 갱신이 실패하면 그 요청도 캐시로
    간다(남은 시간이 1분을 넘는다). 예전에는 다른 요청이 lock 뒤에서 그 STS 호출을 통째로
    기다렸다 — 세 경로 모두(native 도: 실패가 기록되기 전이라 "막힘" 이 아니었다)."""
    clock = [1000.0]
    sts = _HungSTS(clock)
    sts.fail = False
    p = _Path(path, sts, monkeypatch)
    assert await p.request() == "account"
    clock[0] = _EXPIRY - 200
    sts.fail = True
    sts.gate = threading.Event()
    try:
        refresh = asyncio.create_task(p.request())
        await _until(lambda: len(sts.calls) == 2)
        others = await asyncio.wait_for(
            asyncio.gather(*(p.request() for _ in range(3))), timeout=0.5
        )
        assert others == ["account"] * 3
        assert not refresh.done(), "갱신은 아직 STS 안에 있다"
    finally:
        sts.gate.set()
    assert await refresh == "account"
    assert len(sts.calls) == 2


@pytest.mark.parametrize("path", _PATHS)
async def test_a_failed_refresh_and_the_memory_after_it_serve_the_cached_credentials(
    path, monkeypatch
):
    """갱신이 실패해도(35초 걸려 165초 남음) 그 요청은 캐시된 자격증명으로 서명하고, 그 뒤 기억 안의
    요청도 STS 없이 캐시로 간다. 예전에 runtime·Mantle 은 둘 다 502 였다(native 는 이미 이랬다)."""
    clock = [1000.0]
    sts = _HungSTS(clock)
    sts.fail = False
    p = _Path(path, sts, monkeypatch)
    assert await p.request() == "account"
    clock[0] = _EXPIRY - 200
    sts.fail = True
    assert await p.request() == "account"
    assert clock[0] == _EXPIRY - 200 + STS_SECONDS
    clock[0] += 1.0
    assert await p.request() == "account"
    assert len(sts.calls) == 2, f"기억 안에서 STS 를 불렀다: {sts.calls}"


@pytest.mark.parametrize("path", _PATHS)
async def test_credentials_with_a_minute_or_less_left_are_never_served(path, monkeypatch):
    """1분 이하로 남았거나 만료된 자격증명은 어떤 경우에도 쓰지 않는다 — 그 자격증명으로 시작한
    호출(긴 스트림)이 도중에 만료된다. 61초 남았을 때 시작한 갱신이 1초 걸려 실패하면 남은 것은
    정확히 60초라 폴백이고, 기억 안의 요청도, 만료 뒤의 재시도 실패도 폴백이다."""
    clock = [1000.0]
    sts = _HungSTS(clock, seconds=1.0)
    sts.fail = False
    p = _Path(path, sts, monkeypatch)
    assert await p.request() == "account"
    sts.fail = True
    clock[0] = _EXPIRY - 61
    assert await p.request() == "fallback"
    clock[0] += 0.5
    assert await p.request() == "fallback"
    clock[0] = _EXPIRY + 100
    assert await p.request() == "fallback"
    assert len(sts.calls) == 3


@pytest.mark.parametrize("path", _PATHS)
async def test_the_memory_is_per_role_and_external_id(path, monkeypatch):
    """기억의 키는 (역할, ExternalId, 세션 이름)이다. 한 키의 실패가 다른 ExternalId 나 다른 역할의
    요청을 막지 않는다."""
    clock = [1000.0]
    sts = _HungSTS(clock)
    p = _Path(path, sts, monkeypatch)
    assert await p.request() == "fallback"
    clock[0] = 1036.0
    sts.fail = False
    assert await p.request() == "fallback"  # 실패한 키 자신은 기억 안이다
    assert await p.request(ext="other-ext") == "account"
    assert await p.request(role=OTHER_ROLE) == "account"
    # 같은 역할의 다른 ExternalId 가 방금 받은 자격증명을 실패한 키에 내주지도 않는다(캐시도 키별).
    assert await p.request() == "fallback"
    assert [(c[1], c[2]) for c in sts.calls] == [
        (ROLE, EXT),
        (ROLE, "other-ext"),
        (OTHER_ROLE, EXT),
    ]
    assert {c[3] for c in sts.calls} == {
        {"native": "gw-bedrock-xacct", "runtime": "gw-bedrock-openai", "mantle": "gw-mantle"}[path]
    }


# ─────────────────────────────────────────────────────────────────────────────
# 2. 폴백은 경로마다 오늘 그대로다
# ─────────────────────────────────────────────────────────────────────────────


async def test_native_skips_to_the_pod_account_and_serves_200(monkeypatch):
    """claude-code native: 기억 안의 요청도 STS 실패 때와 똑같이 파드 계정 클라이언트로 200 이다."""
    clock = [1000.0]
    sts = _HungSTS(clock)
    p = _Path("native", sts, monkeypatch)
    pod = MagicMock()
    pod.invoke_model.return_value = {
        "body": MagicMock(read=lambda: b'{"usage":{"input_tokens":1,"output_tokens":1}}'),
        "ResponseMetadata": {"RequestId": "rid"},
    }

    async def _invoke():
        adapter = BedrockAdapter(
            bedrock_client=None,
            client_resolver=lambda: p.provider.get_client(ROLE, "ap-northeast-2", EXT),
            fallback_client=pod,
        )
        status, *_ = await adapter.invoke(b"{}", "anthropic.claude-sonnet-4-6")
        return status, adapter.served_by_fallback

    assert await _invoke() == (200, True)  # STS 실패 → 파드 계정
    clock[0] = 1036.0
    assert await _invoke() == (200, True)  # 기억 → STS 없이 파드 계정
    assert len(sts.calls) == 1


async def test_runtime_and_mantle_skips_answer_the_same_502_as_an_sts_failure(monkeypatch):
    """runtime·Mantle 의 프로필 경로 폴백은 502 다. 기억으로 건너뛴 요청도 STS 실패와 **같은**
    상태·본문이어야 한다 — alias 계정의 500(폴백·서킷 밖)으로 바뀌면 안 된다."""
    rt_clock, mt_clock = [1000.0], [1000.0]
    runtime = _Path("runtime", _HungSTS(rt_clock), monkeypatch)
    mantle = _Path("mantle", _HungSTS(mt_clock), monkeypatch)
    rt_adapter = BedrockOpenAIAdapter(http_client=MagicMock(), signer=runtime.signer)
    mt_adapter = MantleAdapter(http_client=MagicMock(), broker=mantle.broker)

    async def _rt():
        status, body, _h, _u = await rt_adapter.invoke(
            b"{}", "us.openai.gpt-5.6-terra", profile=_profile(), endpoint=RUNTIME_ENDPOINT
        )
        return status, body

    async def _mt():
        status, body, _h, _u = await mt_adapter.invoke(
            b"{}", "anthropic.claude-opus-4-8", profile=_profile(), endpoint="https://mantle.invalid"
        )
        return status, body

    first_rt, first_mt = await _rt(), await _mt()
    rt_clock[0] += 1.0
    mt_clock[0] += 1.0
    skipped_rt, skipped_mt = await _rt(), await _mt()
    assert first_rt[0] == 502 and b"signing failed" in first_rt[1]
    assert first_mt[0] == 502 and b"Mantle call failed" in first_mt[1]
    assert (skipped_rt, skipped_mt) == (first_rt, first_mt)
    assert ACCOUNT_UNAVAILABLE_STATUS not in (skipped_rt[0], skipped_mt[0])
    assert len(runtime.sts.calls) == 1 and len(mantle.sts.calls) == 1


# ─────────────────────────────────────────────────────────────────────────────
# 3. alias 경로는 그대로 — 500 + 실패 시각부터 30초
# ─────────────────────────────────────────────────────────────────────────────


async def test_the_alias_path_keeps_its_500_and_its_30_second_memory(monkeypatch):
    """같은 (역할, ExternalId, gw-bedrock-xacct) 키를 alias 와 claude-code 프로필이 함께 쓴다.

    * alias 실패 → native 어댑터 500, 실패 시각부터 30초 동안 STS 를 다시 부르지 않는다.
    * 그 alias 실패 직후 프로필 경로는 5초 동안 STS 없이 파드 계정으로 가고, 5초 뒤에는 (alias 의
      30초 기억과 무관하게) 다시 부른다.
    * 프로필 경로의 실패는 alias 를 막지 않는다 — alias 요청은 자기 STS 호출을 한다.
    """
    clock = [1000.0]
    sts = _HungSTS(clock)
    p = _Path("native", sts, monkeypatch)
    prov = p.provider
    target = AccountTarget(ROLE, EXT)
    adapter = BedrockAdapter(bedrock_client=MagicMock(), account_clients=prov, account_region="r")

    status, *_ = await adapter.invoke(b"{}", "anthropic.claude-sonnet-4-6", account=target)
    assert status == ACCOUNT_UNAVAILABLE_STATUS
    failed_at = clock[0]
    assert failed_at == 1035.0

    clock[0] = failed_at + 1
    assert await p.request() == "fallback"  # 프로필: 5초 기억 안
    clock[0] = failed_at + PROFILE_FAILURE_BACKOFF
    assert await p.request() == "fallback"  # 프로필: 다시 불렀고(멈춘 STS) 실패
    assert [c[0] for c in sts.calls] == [1000.0, failed_at + PROFILE_FAILURE_BACKOFF]

    assert FAILURE_BACKOFF == 30
    clock[0] = failed_at + 30 - 0.1
    with pytest.raises(AccountCredentialsUnavailable):
        await prov.credentials(target, session_name=SESSION_NAME_NATIVE)
    assert len(sts.calls) == 2, "alias 는 자기 실패 시각부터 30초 동안 STS 를 부르지 않는다"
    sts.fail = False
    clock[0] = failed_at + 30
    creds, _ = await prov.credentials(target, session_name=SESSION_NAME_NATIVE)
    assert creds.access_key == "ASIA3"

    # 반대 방향: 프로필 경로의 실패가 alias 를 500 으로 만들지 않는다.
    clock2 = [5000.0]
    sts2 = _HungSTS(clock2, seconds=1.0)
    p2 = _Path("native", sts2, monkeypatch)
    assert await p2.request() == "fallback"
    sts2.fail = False
    creds2, _ = await p2.provider.credentials(target, session_name=SESSION_NAME_NATIVE)
    assert creds2.access_key == "ASIA2" and len(sts2.calls) == 2


# ─────────────────────────────────────────────────────────────────────────────
# 4. 전역 lock 을 쓰는 두 경로 — 다른 키의 요청은 멈춘 STS 를 기다리지 않는다
# ─────────────────────────────────────────────────────────────────────────────


async def test_runtime_other_role_with_cached_credentials_does_not_wait_for_a_hung_sts(
    monkeypatch,
):
    """SigV4Signer 의 lock 은 모든 역할이 하나를 쓴다. 한 역할의 STS 가 멈춰 있는 동안에도
    자격증명이 캐시된 다른 역할의 서명은 곧바로 끝나야 한다."""
    clock = [1000.0]
    sts = _HungSTS(clock)
    p = _Path("runtime", sts, monkeypatch)
    sts.fail = False
    assert await p.request(role=OTHER_ROLE) == "account"  # 다른 역할 자격증명 캐시
    sts.fail = True
    sts.gate = threading.Event()
    try:
        hung = asyncio.create_task(p.request())
        await _until(lambda: len(sts.calls) == 2)
        assert await asyncio.wait_for(p.request(role=OTHER_ROLE), timeout=1.0) == "account"
    finally:
        sts.gate.set()
    assert await hung == "fallback"


async def test_mantle_in_account_bearer_does_not_wait_for_a_hung_profile_sts(monkeypatch):
    """Mantle 브로커의 lock 은 in-account(codex)와 프로필 역할(cowork)이 함께 쓴다. 프로필 역할의
    STS 가 멈춰 있어도 bearer 가 캐시된 in-account 요청은 곧바로 끝나야 한다."""
    clock = [1000.0]
    sts = _HungSTS(clock)
    p = _Path("mantle", sts, monkeypatch)
    monkeypatch.setattr(
        mc.MantleCredentialBroker,
        "_in_account_creds",
        lambda self: mc.Credentials("AKIA_POD", "s", "t"),
    )
    in_account = _profile(role=None, ext=None, region="us-east-2")
    pod_bearer = await p.broker.bearer_token(in_account)
    sts.gate = threading.Event()
    try:
        hung = asyncio.create_task(p.request())
        await _until(lambda: len(sts.calls) == 1)
        got = await asyncio.wait_for(p.broker.bearer_token(in_account), timeout=1.0)
        assert got == pod_bearer
    finally:
        sts.gate.set()
    assert await hung == "fallback"
