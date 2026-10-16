# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""Cross-account Bedrock credential + client broker.

원래 역할(claude-code → 다른 계정 native): Bedrock NATIVE(boto3 invoke_model)는 Mantle 과 달리
cross-account 를 미지원했다(startup 에 in-account 클라이언트 고정). 이 브로커는 대상 계정
role 을 STS AssumeRole 하여 그 계정의 bedrock-runtime 클라이언트를 빌드·캐시한다.

alias 별 대상 계정(0043/0044) 이후 이 클래스는 **alias 계정 자격증명의 단일 캐시**이기도 하다:

  * ``get_client(role, region, external_id)`` — 라우팅 프로필 경로(claude-code). 같은 STS 인자
    (RoleSessionName ``gw-bedrock-xacct``, 1h, ExternalId), 만료 300s 전 재빌드, 실패는 raise 해
    호출자(BedrockAdapter._get_client)가 in-account 로 투명 폴백한다.
    실패는 ``PROFILE_FAILURE_BACKOFF``(5초)만 기억한다(``ProfileFailureMemory``, alias 의
    ``_failures`` 와 따로다). 그 동안 이 경로의 요청은 STS 를 부르지 않고 곧바로 in-account 로
    가고, 기억이 끝난 뒤의 재시도는 ``PROFILE_RETRY_WAIT``(1초)까지만 기다린다 — STS 가 응답 없이
    멈춰도 줄 선 요청이 각자 타임아웃을 차례로 기다리지 않는다. 1분 넘게 남은 캐시 자격증명이
    있으면(갱신 창) 기억 안에서도, 갱신이 진행 중일 때도 그것을 쓴다. 이 경로의 실패는 alias 의
    30초 기억에 쓰지 않는다 — 같은 역할을 고른 alias 의 요청이 이 경로의 일시 실패 때문에 500 이
    되지 않는다.
  * ``get_account_client(target, region)`` — alias 계정의 native 호출.
  * ``credentials(target, session_name=...)`` — alias 계정의 runtime(SigV4)·Mantle(bearer) 서명.
    ``(자격증명, 그 만료)`` 를 한 lock 안에서 함께 돌려준다 — 만료를 따로 읽으면 그 사이 다른
    요청이 갱신한 항목의 만료를 받는다. 세 경로가 같은 (역할, ExternalId, 세션 이름) 키를
    공유하므로 AssumeRole 이 경로마다 따로 일어나지 않는다.
    실패는 :class:`AccountCredentialsUnavailable` 이고 ``FAILURE_BACKOFF`` 초 동안 기억한다.

- 갱신 실패(만료 ``_CLIENT_REFRESH_SKEW`` 초 전부터 재assume)는 캐시된 자격증명이 아직
  ``_STALE_SERVE_MIN`` 초 넘게 남았으면 거절이 아니다: 경고를 남기고 그 자격증명으로 계속 서명한다.
  재시도 간격은 같은 실패 기억(alias 경로)으로 조절한다. STS 오류 한 번이 유효한 자격증명을 버리고
  그 계정의 모든 요청을 30초 동안 500 으로 만들던 동작을 없앤다.

- 자격증명 캐시 키 = (role_arn, external_id, session_name). ExternalId 가 키에 없으면 같은 역할을
  ExternalId 없이 지목한 요청이 ExternalId 로 assume 한 자격증명을 캐시 적중으로 받는다 —
  confused-deputy 검사가 매 적중마다 건너뛰어진다.
- 클라이언트 캐시 키 = (role_arn, region, external_id). region-bound 클라이언트 cross-serve 방지.
  자격증명 객체가 바뀌면(=재assume) 재빌드한다 — static creds 는 botocore 가 갱신하지 않는다.
- 키별 single-flight lock. 전역 lock 하나는 느리거나 실패하는 한 계정의 STS 왕복 뒤에 다른
  모든 계정을 줄 세웠다.
- assume/build 는 blocking → run_in_executor. 클라이언트 빌드만은 모듈 lock 으로 직렬화한다
  (``_BUILD_LOCK``): ``boto3.client`` 는 기본 세션을 쓰고, 세션은 스레드 안전하지 않다.
"""
from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import boto3
import structlog
from botocore.config import Config as BotoConfig
from botocore.credentials import Credentials

from app.services.account_credentials import (
    SESSION_NAME_NATIVE,
    STALE_SERVE_MIN,
    AccountCredentialsUnavailable,
    AccountTarget,
    ProfileFailureMemory,
)

logger = structlog.get_logger(__name__)

_ASSUME_DURATION = 3600          # 1h STS 세션 (Mantle 브로커와 동일)
_CLIENT_REFRESH_SKEW = 300       # creds 하드만료 이 초 전에 재assume → 클라이언트 재빌드
#: 실패한 AssumeRole 을 기억하는 시간(alias 계정만). 없으면 틀린 ARN/ExternalId 의 바쁜 alias 가
#: 요청마다 STS 왕복을 하나씩 쓰고 그 키의 lock 뒤에 줄을 선다. 짧게 둔다 — 신뢰 정책을 고친
#: 운영자는 재시작 없이 30초 안에 동작을 봐야 한다.
FAILURE_BACKOFF = 30
#: 갱신이 실패해도 캐시된 자격증명을 계속 쓰는 최소 잔여 시간. 이보다 짧으면 그 자격증명으로 시작한
#: 호출(긴 스트림 포함)이 도중에 만료될 수 있어 실패로 돌려준다. 세 경로가 같은 값이다.
_STALE_SERVE_MIN = STALE_SERVE_MIN
#: boto3 기본 세션의 client 생성 직렬화(모듈 docstring). 빌드는 키마다 시간당 한 번꼴이라 싸다.
_BUILD_LOCK = threading.Lock()

#: 요청 경로의 STS 클라이언트 설정(:func:`create_sts_client`). 설정이 없으면 botocore 기본값 —
#: connect 60초 · read 60초 · legacy 재시도 총 5회(백오프 최대 약 15초) — 이라, 응답 없이 멈춘 STS
#: (예: STS VPC 엔드포인트 보안 그룹 오설정) 에서 AssumeRole 한 번이 약 315초를 쓴다. 이 값이면
#: 최악 3회 × (3 + 5)초 + 백오프 3초 이내 ≈ 27초, 연결 자체가 안 되면 3회 × 3초 + 3초 ≈ 12초다.
#: 정상 AssumeRole 은 수십~수백 밀리초라 5초 read 는 넉넉하다.
STS_BOTO_CONFIG = BotoConfig(
    connect_timeout=3,
    read_timeout=5,
    retries={"mode": "standard", "total_max_attempts": 3},
)


def create_sts_client(region: str):
    """게이트웨이가 요청 경로에서 쓰는 STS 클라이언트(main.py, 세 경로가 공유).

    설정은 :data:`STS_BOTO_CONFIG`.
    """
    return boto3.client("sts", region_name=region, config=STS_BOTO_CONFIG)


@dataclass
class _CachedCreds:
    creds: Credentials
    expires_at: float            # epoch sec (STS Expiration)


@dataclass
class _CachedClient:
    client: Any
    creds: Credentials           # 이 클라이언트를 빌드한 자격증명 — 바뀌면 재빌드
    expires_at: float


_CredKey = tuple  # (role_arn, external_id or "", session_name)


class BedrockAccountClientProvider:
    """대상 계정 role 을 assume 하여 자격증명/bedrock-runtime 클라이언트를 vend·캐시."""

    def __init__(self, sts_client, boto_config, now: Callable[[], float] = time.time) -> None:
        self._sts = sts_client
        self._boto_config = boto_config
        self._now = now
        self._creds: dict[_CredKey, _CachedCreds] = {}
        self._clients: dict[tuple[str, str, str], _CachedClient] = {}
        self._failures: dict[_CredKey, tuple[float, str]] = {}
        # 라우팅 프로필 경로(get_client)의 짧은 실패 기억. 모든 AssumeRole(alias 포함)의 실패를
        # 기록하고, 프로필 경로만 읽는다 — 같은 키의 alias 실패 직후에도 프로필 요청이 STS 를 다시
        # 기다리지 않는다. alias 경로는 오늘처럼 자기 30초 기억(_failures)만 본다.
        self._profile_failures = ProfileFailureMemory(now)
        self._locks: dict[tuple, asyncio.Lock] = {}

    # ── 자격증명 ────────────────────────────────────────────────────────────────────

    async def credentials(
        self,
        target: AccountTarget,
        *,
        session_name: str,
        remember_failure: bool = True,
    ) -> tuple[Credentials, float]:
        """``target`` 계정의 ``(자격증명, 하드 만료 epoch)``.

        실패는 :class:`AccountCredentialsUnavailable`.

        만료를 같이 돌려주는 이유: 스스로 갱신할 수 없는 파생물(Mantle bearer)은 TTL 을 **그것을
        만든 자격증명의** 만료에 맞춰 잘라야 한다. 캐시를 따로 다시 읽으면 그 사이 갱신된 새 항목의
        만료를 받고, 옛 자격증명으로 만든 bearer 가 옛 자격증명이 끝난 뒤에도 남아 401 이 된다.

        파드 자신의 계정은 여기 오지 않는다 — 그 경로는 각 transport 의 기존 코드(파드 boto3
        클라이언트, SigV4Signer 의 세션, Mantle 브로커의 매 mint 새 세션)가 그대로 맡는다.
        """
        key: _CredKey = (target.role_arn, target.external_id or "", session_name)
        # 프로필 경로: 그 키의 AssumeRole 이 방금 실패했거나(기억 5초) 그 키의 STS 호출(갱신·
        # 재시도)이 진행 중이면 lock 뒤에 줄 서지 않는다 — 줄 서면 STS 가 멈춰 있는 동안 그 호출의
        # 타임아웃을 통째로 기다린다.
        #   * 1분 넘게 남은 캐시 자격증명이 있으면 그것(_profile_cached_or_raise 와 같은 규칙).
        #     갱신이 진행 중일 때도다 — 처음 갱신이 멈춘 STS 에 걸려도 다른 요청은 기다리지 않는다.
        #   * 없고 기억이 끝난 뒤의 재시도가 진행 중이면 그 결과를 잠깐 기다린다
        #     (PROFILE_RETRY_WAIT). 그래도 없으면 실패 — 호출자가 in-account 로 폴백한다.
        #   * 실패한 적 없는 첫 assume 이 진행 중이고 쓸 것이 없으면 지금처럼 그것을 기다린다.
        if not remember_failure:
            blocked = self._profile_failures.blocked(key)
            if blocked or self._profile_failures.in_flight(key):
                cached = self._creds.get(key)
                if cached and cached.expires_at - _STALE_SERVE_MIN > self._now():
                    return cached.creds, cached.expires_at
                if blocked:
                    await self._profile_failures.wait_for_retry(key)
                    return self._profile_cached_or_raise(key, target)
        async with self._locks.setdefault(("creds", *key), asyncio.Lock()):
            now = self._now()
            cached = self._creds.get(key)
            if cached and cached.expires_at - _CLIENT_REFRESH_SKEW > now:
                return cached.creds, cached.expires_at
            # 갱신 창이다. 갱신이 실패해도 쓸 수 있는 자격증명(모듈 docstring).
            usable = cached if cached and cached.expires_at - _STALE_SERVE_MIN > now else None
            failed = self._failures.get(key)
            if failed and failed[0] > now:
                if usable is not None:
                    return usable.creds, usable.expires_at  # 재시도는 백오프 뒤에
                if remember_failure:
                    raise AccountCredentialsUnavailable(target.role_arn, failed[1])
            # 프로필 경로: lock 을 기다리는 동안 앞 요청의 STS 가 실패했다 — 다시 부르지 않는다.
            if not remember_failure and self._profile_failures.blocked(key):
                return self._profile_cached_or_raise(key, target)
            try:
                fresh = await self._assume(key, target, remember_failure=remember_failure)
            except AccountCredentialsUnavailable:
                # ⚠️ 쓸 수 있는지는 실패한 **지금** 다시 본다. 위의 판단은 STS 호출 전 시각이라,
                #    STS 가 실패하기까지 오래 걸리면(봇코어 기본 connect/read 60초 × 재시도) 그 사이
                #    남은 시간이 _STALE_SERVE_MIN 아래로 내려갔거나 이미 만료된 자격증명을 돌려준다.
                now = self._now()
                if usable is None or usable.expires_at - _STALE_SERVE_MIN <= now:
                    raise
                logger.warning(
                    "account_credentials_refresh_failed_serving_cached",
                    role_arn=target.role_arn,
                    source=target.source,
                    session_name=key[2],
                    seconds_left=int(usable.expires_at - now),
                )
                return usable.creds, usable.expires_at
            return fresh.creds, fresh.expires_at

    def _profile_cached_or_raise(
        self, key: _CredKey, target: AccountTarget
    ) -> tuple[Credentials, float]:
        """프로필 경로가 STS 를 건너뛸 때: 지금 1분 넘게 남은 캐시 자격증명이면 그것(갱신 실패 때와
        같은 규칙), 아니면 :class:`AccountCredentialsUnavailable` — 호출자가 in-account 로 폴백한다.
        """
        cached = self._creds.get(key)
        if cached and cached.expires_at - _STALE_SERVE_MIN > self._now():
            return cached.creds, cached.expires_at
        raise AccountCredentialsUnavailable(
            target.role_arn, "AssumeRole failed moments ago; not calling STS again yet"
        )

    async def _assume(
        self, key: _CredKey, target: AccountTarget, *, remember_failure: bool
    ) -> _CachedCreds:
        """호출자가 그 키의 lock 을 잡고 있어야 한다.

        실패는 ``remember_failure`` 일 때만 기억한다.
        """
        kwargs: dict[str, Any] = {
            "RoleArn": target.role_arn,
            "RoleSessionName": key[2],
            "DurationSeconds": _ASSUME_DURATION,
        }
        if target.external_id:
            kwargs["ExternalId"] = target.external_id
        try:
            # 성공·실패 모두 프로필 경로의 짧은 기억에 반영된다(실패 시각부터 — attempt 참조).
            with self._profile_failures.attempt(key):
                resp = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: self._sts.assume_role(**kwargs)
                )
        except Exception as exc:  # botocore ClientError, EndpointConnectionError, ...
            err = getattr(exc, "response", None)
            code = err.get("Error", {}).get("Code") if isinstance(err, dict) else None
            reason = f"AssumeRole failed: {code or type(exc).__name__}"
            if remember_failure:
                # ⚠️ 실패를 **기록하는 지금**부터 센다 — 호출 전에 읽은 시각이 아니라. STS 가
                #    실패하기까지 FAILURE_BACKOFF 보다 오래 걸리면 "시작 시각 + 30" 은 기록하는 순간
                #    이미 지나 있다. 그러면 백오프가 한 번도 걸리지 않고, 이 키의 lock 뒤에 줄 선
                #    요청이 저마다 STS 를 다시 불러 각자의 타임아웃을 차례로 기다린다.
                self._failures[key] = (self._now() + FAILURE_BACKOFF, reason)
            logger.warning(
                "account_credentials_assume_failed",
                role_arn=target.role_arn,
                source=target.source,
                session_name=key[2],
                with_external_id=bool(target.external_id),
                error_code=code or type(exc).__name__,
            )
            raise AccountCredentialsUnavailable(target.role_arn, reason) from exc
        c = resp["Credentials"]
        creds = Credentials(c["AccessKeyId"], c["SecretAccessKey"], c["SessionToken"])
        entry = _CachedCreds(creds=creds, expires_at=c["Expiration"].timestamp())
        self._creds[key] = entry
        self._failures.pop(key, None)
        logger.info(
            "account_credentials_assumed",
            role_arn=target.role_arn,
            source=target.source,
            session_name=key[2],
            with_external_id=bool(target.external_id),
        )
        return entry

    # ── bedrock-runtime 클라이언트 (native) ─────────────────────────────────────────

    async def get_client(self, role_arn: str, region: str, external_id: str | None = None):
        """라우팅 프로필 경로(claude-code cross-account). 실패는 raise → 호출자가 in-account 폴백.

        실패는 ``PROFILE_FAILURE_BACKOFF`` 동안만 기억한다(alias 의 ``_failures`` 에는 쓰지 않는다).
        그 동안의 요청은 STS 없이 곧바로 in-account 로 서빙되고, 기억이 끝난 뒤의 첫 요청이 다시
        assume 을 시도한다 — 신뢰 정책을 고치면 최대 5초 안에 그 계정으로 돌아간다.
        """
        target = AccountTarget(role_arn=role_arn, external_id=external_id, source="profile")
        return await self._client(target, region, remember_failure=False)

    async def get_account_client(self, target: AccountTarget, region: str):
        """alias 계정의 native 클라이언트. 실패는 :class:`AccountCredentialsUnavailable`."""
        return await self._client(target, region, remember_failure=True)

    async def _client(self, target: AccountTarget, region: str, *, remember_failure: bool):
        creds, expires_at = await self.credentials(
            target, session_name=SESSION_NAME_NATIVE, remember_failure=remember_failure
        )
        # 캐시 키에 external_id 포함: external_id 회전/수정 시 stale 클라이언트 재사용을 막는다
        # (external_id 는 assume 자격의 일부라 값이 바뀌면 다른 세션으로 취급해야 함).
        ckey = (target.role_arn, region, target.external_id or "")
        async with self._locks.setdefault(("client", *ckey), asyncio.Lock()):
            cached = self._clients.get(ckey)
            # 자격증명 객체가 같으면 재사용 — 재assume 은 새 객체를 돌려주므로 그게 곧 갱신 신호다.
            if cached is not None and cached.creds is creds:
                return cached.client
            client = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._build_client(region, creds)
            )
            self._clients[ckey] = _CachedClient(client=client, creds=creds, expires_at=expires_at)
            logger.info(
                "bedrock_xacct_client_built",
                role_arn=target.role_arn,
                region=region,
                source=target.source,
            )
            return client

    def _build_client(self, region: str, creds: Credentials):
        # in-account 클라이언트와 같은 BotoConfig(main.py) — read_timeout·retries·pool 이 같아야
        # 계정만 바뀌고 나머지 동작은 그대로다. 키별 lock 은 서로 다른 키의 빌드를 동시에 돌리므로
        # 기본 세션을 건드리는 생성은 모듈 lock 으로 한 번에 하나만.
        with _BUILD_LOCK:
            return boto3.client(
                "bedrock-runtime",
                region_name=region,
                config=self._boto_config,
                aws_access_key_id=creds.access_key,
                aws_secret_access_key=creds.secret_key,
                aws_session_token=creds.token,
            )
