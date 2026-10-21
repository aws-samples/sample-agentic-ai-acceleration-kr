# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""SigV4 request signer for the Bedrock **runtime** OpenAI wires (httpx, not boto3).

Why this exists at all: ``bedrock-runtime`` serves the OpenAI dialect at
``/openai/v1/responses`` and ``/openai/v1/chat/completions`` as plain HTTPS — those paths
are NOT botocore operations, so boto3 cannot call them and the request must be signed by
hand (verified live 2026-09-03: SigV4 service name ``bedrock``, both paths return 200 and
a real SSE stream). Everything else on the Bedrock runtime plane (InvokeModel/Converse)
keeps using ``providers/bedrock_adapter.py`` + boto3.

Two credential paths, mirroring :class:`~app.services.mantle_credentials.MantleCredentialBroker`:

* in-account (``role_arn`` is None) — the pod's own IRSA credentials.
* cross-account (``role_arn`` set) — STS AssumeRole, creds cached ~1h. A failed AssumeRole is
  remembered for ``PROFILE_FAILURE_BACKOFF`` seconds per (role, ExternalId, session name): until
  then requests for that key fail at once (the adapter's usual 502) instead of each calling STS
  again behind the lock and waiting out its timeout. Cached credentials with more than
  ``STALE_SERVE_MIN`` seconds left keep signing while a refresh is in flight, after a refresh
  fails, and during that memory; a retry in flight after the memory is waited for at most
  ``PROFILE_RETRY_WAIT`` seconds.

It vends SIGNATURES where the Mantle broker vends BEARER TOKENS; the duplication of the
assume+cache block is deliberate. Folding both into one broker would mean editing the
live Cowork/Codex bearer path to add a runtime-plane feature, and a regression there takes
down two shipped clients. The two caches are also keyed differently: a bearer is bound to
(creds, region) and reusable for 30 min, while a signature is per-request (it covers the
body hash) so only the CREDENTIALS are worth caching here.

A third credential source, for alias accounts (migration 0043): ``sign(account=...)`` takes
the credentials from the shared ``BedrockAccountClientProvider`` — the one cache every plane
uses for alias accounts — and leaves the two paths above byte-identical. ``account`` changes
WHICH credentials sign, never the URL or the region: the SigV4 scope
(``<key>/<date>/<region>/bedrock``) has no account field, so the signing identity is the only
thing that selects an account.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Callable, Optional

import structlog
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

from app.services.account_credentials import (
    SESSION_NAME_RUNTIME,
    STALE_SERVE_MIN,
    AccountCredentialsUnavailable,
    ProfileAssumeBackoff,
    ProfileFailureMemory,
)

logger = structlog.get_logger(__name__)

_ASSUME_DURATION = 3600      # 1h STS session (same as the Mantle broker)
_CRED_REFRESH_SKEW = 300     # refresh this many seconds before hard expiry


@dataclass
class _CachedCreds:
    creds: Credentials
    expires_at: float  # epoch seconds (from STS Expiration)


class SigV4Signer:
    """Sign bedrock-runtime HTTPS requests, in-account or cross-account.

    ``sts_client`` may be None when only the in-account path is used (then a
    cross-account call raises rather than silently signing with the pod's own identity,
    which would bill the wrong account and hide a misconfiguration).
    """

    def __init__(
        self,
        sts_client=None,
        service_name: str = "bedrock",
        now: Callable[[], float] = time.time,
        account_provider=None,
    ) -> None:
        self._sts = sts_client
        self._service = service_name
        self._now = now
        # 프로필 역할의 자격증명 — 키 (역할, ExternalId, gw-bedrock-openai). alias 경로(공유
        # provider)와 같은 키다. 역할만으로 키를 잡으면 같은 역할을 다른 ExternalId 로 지목한
        # 프로필이 앞 프로필의 ExternalId 로 assume 한 자격증명을 캐시 적중으로 받는다 —
        # confused-deputy 검사(신뢰 정책의 ExternalId 조건)가 매 적중마다 건너뛰어진다.
        self._creds: dict[tuple[str, str, str], _CachedCreds] = {}
        self._lock = asyncio.Lock()
        # 프로필 경로의 짧은 AssumeRole 실패 기억 — 키 (역할, ExternalId, gw-bedrock-openai).
        self._profile_failures = ProfileFailureMemory(now)
        # BedrockAccountClientProvider (0043). alias 계정 서명에만 쓴다. None 이면 account 가 온
        # 서명은 파드 자격증명으로 대신하지 않고 거부한다.
        self._account_provider = account_provider
        # One long-lived botocore session for the in-account path. It owns the
        # refreshable credential object, so building it ONCE is what makes the IRSA
        # web-identity session reusable; a per-request boto3.Session() would re-run the
        # whole credential chain (token-file read + AssumeRoleWithWebIdentity) on every
        # single request.
        self._session = None

    async def sign(
        self,
        *,
        method: str,
        url: str,
        body: bytes,
        region: str,
        headers: Optional[dict] = None,
        role_arn: Optional[str] = None,
        external_id: Optional[str] = None,
        account=None,
    ) -> dict:
        """Return the headers to send, including Authorization/X-Amz-Date/security token.

        The signature covers the exact ``body`` bytes and the host implied by ``url``, so
        the caller MUST send both verbatim (no re-serialising the JSON, no redirect
        following) or the service answers 403 SignatureDoesNotMatch.

        ``account`` (an ``AccountTarget``, alias account) wins over ``role_arn`` — the alias
        account takes precedence over the routing profile's. Raises
        ``AccountCredentialsUnavailable`` when it cannot be used; never falls back to the
        profile's role or the pod's identity.
        """
        if account is not None:
            creds = await self._account_creds(account)
        else:
            creds = await self._resolve_creds(role_arn, external_id)
        base = {"content-type": "application/json", **(headers or {})}
        request = AWSRequest(method=method, url=url, data=body, headers=base)
        # SigV4Auth is pure CPU (hmac over the canonical request) — no I/O, safe inline.
        SigV4Auth(creds, self._service, region).add_auth(request)
        return dict(request.headers)

    async def _account_creds(self, account) -> Credentials:
        """alias 계정의 자격증명 — 공유 provider 에서(키: 역할, ExternalId, gw-bedrock-openai)."""
        if self._account_provider is None:
            # 파드 자격증명으로 서명하면 다른 계정에 청구되는 **성공** 호출이 된다.
            raise AccountCredentialsUnavailable(
                account.role_arn,
                "alias requests a target AWS account but no account provider is wired into "
                "SigV4Signer; refusing to sign with the pod's own credentials",
            )
        creds, _expires_at = await self._account_provider.credentials(
            account, session_name=SESSION_NAME_RUNTIME
        )
        return creds

    async def _resolve_creds(
        self, role_arn: Optional[str], external_id: Optional[str]
    ) -> Credentials:
        if not role_arn:
            return await asyncio.get_running_loop().run_in_executor(
                None, self._in_account_creds
            )
        # lock 밖에서 먼저 본다. lock 은 모든 역할이 하나를 쓰므로, 캐시가 신선한 요청까지 다른
        # 역할의 느린 STS 호출 뒤에 줄 서지 않게 한다. 그 키의 AssumeRole 이 방금 실패했거나
        # 재시도가 진행 중이면 STS 를 기다리지 않고 실패한다 — 어댑터가 오늘처럼 502 로 답한다
        # (ProfileFailureMemory).
        key = (role_arn, external_id or "", SESSION_NAME_RUNTIME)
        cached = self._creds.get(key)
        if cached and cached.expires_at - _CRED_REFRESH_SKEW > self._now():
            return cached.creds
        # 갱신(또는 첫 assume) 차례다. 그 키의 AssumeRole 이 방금 실패했거나(기억 5초) 그 키의 STS
        # 호출이 진행 중이면 STS 도 lock 도 기다리지 않는다 — native 와 같은 규칙
        # (BedrockAccountClientProvider.credentials): 1분 넘게 남은 캐시 자격증명이면 그것으로
        # 서명하고, 없으면 기억이 끝난 뒤의 재시도를 잠깐(PROFILE_RETRY_WAIT) 기다린 뒤 실패한다.
        blocked = self._profile_failures.blocked(key)
        if blocked or self._profile_failures.in_flight(key):
            usable = self._usable(key)
            if usable is None and blocked:
                await self._profile_failures.wait_for_retry(key)
                usable = self._usable(key)
                if usable is None:
                    raise ProfileAssumeBackoff(role_arn)
            if usable is not None:
                return usable.creds
        async with self._lock:
            now = self._now()
            cached = self._creds.get(key)
            if cached and cached.expires_at - _CRED_REFRESH_SKEW > now:
                return cached.creds
            if self._sts is None:
                raise RuntimeError(
                    "SigV4Signer has no STS client; cannot assume "
                    f"{role_arn} for the cross-account runtime path"
                )
            if self._profile_failures.blocked(key):  # lock 을 기다리는 사이 앞 요청이 실패했다
                usable = self._usable(key)
                if usable is None:
                    raise ProfileAssumeBackoff(role_arn)
                return usable.creds
            try:
                with self._profile_failures.attempt(key):
                    creds, expires_at = await asyncio.get_running_loop().run_in_executor(
                        None, lambda: self._assume(role_arn, external_id)
                    )
            except Exception:
                # 갱신 실패 — 쓸 수 있는지는 실패한 **지금** 다시 본다(native 와 같다).
                usable = self._usable(key)
                if usable is None:
                    raise
                logger.warning(
                    "sigv4_xacct_refresh_failed_serving_cached",
                    role_arn=role_arn,
                    seconds_left=int(usable.expires_at - self._now()),
                )
                return usable.creds
            self._creds[key] = _CachedCreds(creds=creds, expires_at=expires_at)
            logger.info("sigv4_xacct_creds_assumed", role_arn=role_arn)
            return creds

    def _usable(self, key: tuple) -> _CachedCreds | None:
        """1분 넘게 남은 캐시 자격증명(STALE_SERVE_MIN) — STS 를 건너뛰거나 갱신이 실패했을 때 쓴다.

        만료됐거나 곧 만료될 자격증명은 돌려주지 않는다.
        """
        cached = self._creds.get(key)
        if cached and cached.expires_at - STALE_SERVE_MIN > self._now():
            return cached
        return None

    def _assume(self, role_arn: str, external_id: Optional[str]) -> tuple[Credentials, float]:
        kwargs: dict = {
            "RoleArn": role_arn,
            "RoleSessionName": "gw-bedrock-openai",
            "DurationSeconds": _ASSUME_DURATION,
        }
        if external_id:
            kwargs["ExternalId"] = external_id
        resp = self._sts.assume_role(**kwargs)
        c = resp["Credentials"]
        return (
            Credentials(c["AccessKeyId"], c["SecretAccessKey"], c["SessionToken"]),
            c["Expiration"].timestamp(),
        )

    def _in_account_creds(self) -> Credentials:
        """The pod's own IRSA credentials.

        ``get_frozen_credentials()`` is what refreshes an expiring IRSA web-identity
        session, and it can block on a token-file read plus an STS call — hence the
        executor hop in :meth:`_resolve_creds`. The frozen copy is deliberately NOT
        cached: botocore's session-owned credential object already caches and refreshes,
        and holding the frozen copy would pin an expired session.
        """
        import boto3

        if self._session is None:
            self._session = boto3.Session()
        raw = self._session.get_credentials()
        if raw is None:
            raise RuntimeError(
                "No AWS credentials available for the in-account Bedrock runtime path. "
                "Ensure IRSA is configured on the pod."
            )
        return raw.get_frozen_credentials()
