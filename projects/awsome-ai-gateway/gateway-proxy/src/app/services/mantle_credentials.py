# Copyright 2026 © Amazon.com and Affiliates.
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import structlog
from botocore.credentials import Credentials

from app.schemas.routing import RoutingProfileSchema
from app.services.account_credentials import (
    SESSION_NAME_MANTLE,
    STALE_SERVE_MIN,
    AccountCredentialsUnavailable,
    AccountTarget,
    ProfileAssumeBackoff,
    ProfileFailureMemory,
)

logger = structlog.get_logger(__name__)

# Lazy import so the module loads even if the dep is missing in some envs;
# bearer_token() raises clearly if it is truly absent.
try:
    from aws_bedrock_token_generator import BedrockTokenGenerator
except Exception:  # pragma: no cover - exercised only when dep absent
    BedrockTokenGenerator = None  # type: ignore[assignment]

# Refresh assumed creds this many seconds BEFORE their hard expiry.
_CRED_REFRESH_SKEW = 300
# Bearer tokens are minted with a 12h SigV4 expiry by the generator; we re-mint
# more often to stay well within and to follow rotated creds.
_BEARER_TTL = 1800  # 30 min
# A bearer must never outlive the assumed creds it was minted from. Cap its
# expiry at (creds_expiry - this skew) so we never serve a bearer against
# expired creds (would 401).
_BEARER_CRED_SKEW = 60  # 1 min grace before creds hard-expire
_ASSUME_DURATION = 3600  # 1h


@dataclass
class _CachedCreds:
    creds: Credentials
    expires_at: float  # epoch seconds (from STS Expiration)


@dataclass
class _CachedBearer:
    token: str = field(repr=False)  # never expose the bearer in repr/logs
    expires_at: float = 0.0  # epoch seconds from now()


class MantleCredentialBroker:
    """Mint short-lived Mantle bearer tokens, supporting two credential paths.

    - Cross-account (Cowork / 905): ``account_role_arn`` is set → AssumeRole via
      STS to obtain temporary creds, then mint bearer.
    - In-account (Claude Code / 374): ``account_role_arn`` is None → use the
      pod's own IRSA credentials directly (no AssumeRole needed).

    Holds NO long-lived account keys. Caches assumed creds (~1h) and bearer
    tokens (30 min) to avoid per-request STS calls and token minting. A failed
    AssumeRole of the profile's role is remembered for ``PROFILE_FAILURE_BACKOFF``
    seconds per (role, ExternalId, ``gw-mantle``): until then those requests fail at
    once (the adapter's usual 502) instead of each calling STS again behind the lock.
    Cached credentials with more than ``STALE_SERVE_MIN`` seconds left keep minting
    bearers while a refresh is in flight, after a refresh fails, and during that memory
    (same rule as ``SigV4Signer``); a retry in flight after the memory is waited for at
    most ``PROFILE_RETRY_WAIT`` seconds.

    alias 별 대상 계정(0043): ``bearer_token(profile, account=AccountTarget)`` 이면 프로필
    계정보다 **우선해** 그 계정으로 mint 한다. 자격증명은 세 경로가 공유하는
    ``BedrockAccountClientProvider`` 에서 받고(세션 이름은 이 경로가 쓰던 ``gw-mantle``),
    bearer 는 ((역할, ExternalId), region) 으로 따로 캐시한다. ``account`` 가 없으면 위 두
    경로가 바이트 단위로 그대로다.
    """

    def __init__(
        self,
        sts_client,
        now: Callable[[], float] = time.time,
        account_provider=None,
    ) -> None:
        self._sts = sts_client
        self._now = now
        # BedrockAccountClientProvider. alias 계정 mint 에만 쓴다. None 이면 account 가 온 요청은
        # 프로필/파드 자격증명으로 대신하지 않고 거부한다.
        self._account_provider = account_provider
        # 프로필 역할의 자격증명 — 키 (역할, ExternalId, gw-mantle). alias 경로(공유 provider)와
        # 같은 키다. 역할만으로 키를 잡으면 같은 역할을 다른 ExternalId 로 지목한 프로필이 앞
        # 프로필의 ExternalId 로 assume 한 자격증명(과 그것으로 mint 한 bearer)을 캐시 적중으로
        # 받는다 — confused-deputy 검사(신뢰 정책의 ExternalId 조건)가 매 적중마다 건너뛰어진다.
        self._creds: dict[tuple[str, str, str], _CachedCreds] = {}
        # Bearer cache is keyed by (cred_key, region): a bearer is region-bound
        # (SigV4 binds to the regional endpoint), so two profiles sharing a role
        # but differing region must not cross-serve tokens. cred_key is the profile
        # key above (or "in-account"), so the ExternalId is part of the bearer key too.
        # alias 계정 bearer(0043)는 첫 칸이 ("account", 역할, ExternalId) 튜플이다(_account_bearer).
        self._bearers: dict[tuple, _CachedBearer] = {}
        self._lock = asyncio.Lock()
        # 프로필 경로의 짧은 AssumeRole 실패 기억 — 키 (역할, ExternalId, gw-mantle).
        self._profile_failures = ProfileFailureMemory(now)

    def _in_account_creds(self) -> Credentials:
        """Pod's own IRSA credentials (no AssumeRole) for in-account Mantle (333)."""
        import boto3
        raw = boto3.Session().get_credentials()
        if raw is None:
            raise RuntimeError(
                "No AWS credentials available for in-account Mantle path. "
                "Ensure IRSA is configured on the pod."
            )
        frozen = raw.get_frozen_credentials()
        return Credentials(frozen.access_key, frozen.secret_key, frozen.token)

    async def bearer_token(
        self, profile: RoutingProfileSchema, account: AccountTarget | None = None
    ) -> str:
        if BedrockTokenGenerator is None:
            raise RuntimeError("aws-bedrock-token-generator not installed")
        if account is not None:
            return await self._account_bearer(profile, account)

        profile_key = (
            self._profile_key(profile.account_role_arn, profile.external_id)
            if profile.account_role_arn
            else None
        )
        cred_key = profile_key or "in-account"
        bearer_key = (cred_key, profile.region)
        # lock 밖에서 먼저 본다(_account_bearer 와 같다). 프로필 역할의 STS 호출은 이 lock 안에서
        # 일어나므로, 캐시된 bearer 가 있는 요청(in-account codex 포함)까지 그 호출 뒤에 줄 서지
        # 않게 한다. 그 역할의 AssumeRole 이 방금 실패했거나 재시도가 진행 중이면 STS 를 기다리지
        # 않고 실패한다 — 어댑터가 오늘처럼 502 로 답한다(ProfileFailureMemory).
        cached_bearer = self._bearers.get(bearer_key)
        if cached_bearer and cached_bearer.expires_at > self._now():
            return cached_bearer.token
        if profile_key is not None:
            # 그 키의 AssumeRole 이 방금 실패했거나(기억 5초) 그 키의 STS 호출이 진행 중이면 STS 도
            # lock 도 기다리지 않는다 — SigV4Signer._resolve_creds 와 같은 규칙: 1분 넘게 남은 캐시
            # 자격증명이면 그것으로 mint 하고, 없으면 기억이 끝난 뒤의 재시도를 잠깐
            # (PROFILE_RETRY_WAIT) 기다린 뒤 실패한다(어댑터의 502).
            blocked = self._profile_failures.blocked(profile_key)
            if blocked or self._profile_failures.in_flight(profile_key):
                usable = self._usable(profile_key)
                if usable is None and blocked:
                    await self._profile_failures.wait_for_retry(profile_key)
                    usable = self._usable(profile_key)
                    if usable is None:
                        raise ProfileAssumeBackoff(profile.account_role_arn)
                if usable is not None:
                    return await self._mint(
                        bearer_key, usable.creds, usable.expires_at, profile.region, self._now()
                    )
        async with self._lock:
            now = self._now()
            cached_bearer = self._bearers.get(bearer_key)
            if cached_bearer and cached_bearer.expires_at > now:
                return cached_bearer.token

            if profile_key is not None:
                creds = await self._get_creds(profile.account_role_arn, profile.external_id, now)
                cred_expiry = self._creds[profile_key].expires_at
            else:
                creds = await asyncio.get_running_loop().run_in_executor(None, self._in_account_creds)
                cred_expiry = now + _ASSUME_DURATION

            return await self._mint(bearer_key, creds, cred_expiry, profile.region, now)

    async def _mint(self, bearer_key, creds, cred_expiry: float, region: str, now: float) -> str:
        """``creds`` 로 bearer 를 만들어 ``bearer_key`` 에 캐시한다."""
        token = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: BedrockTokenGenerator().get_token(creds, region),
        )
        # Never let the bearer outlive the underlying creds it was minted from.
        bearer_expiry = min(now + _BEARER_TTL, cred_expiry - _BEARER_CRED_SKEW)
        self._bearers[bearer_key] = _CachedBearer(token=token, expires_at=bearer_expiry)
        return token

    def _usable(self, key: tuple) -> _CachedCreds | None:
        """1분 넘게 남은 캐시 자격증명(STALE_SERVE_MIN) — STS 를 건너뛰거나 갱신이 실패했을 때 쓴다.

        만료됐거나 곧 만료될 자격증명은 돌려주지 않는다.
        """
        cached = self._creds.get(key)
        if cached and cached.expires_at - STALE_SERVE_MIN > self._now():
            return cached
        return None

    async def _account_bearer(self, profile: RoutingProfileSchema, account: AccountTarget) -> str:
        """alias 계정으로 mint 한 bearer. region 은 프로필 그대로(bearer 는 region-bound).

        캐시 키의 첫 칸이 ``("account", 역할, ExternalId)`` 라 프로필 경로의 키(``(역할, ExternalId,
        "gw-mantle")`` / ``"in-account"``)와 겹치지 않고, 프로필 경로의 재assume 무효화
        (``k[0] != 프로필 키``)에도 걸리지 않는다 — 이 bearer 는 그 자격증명에서 나오지 않았다.
        ExternalId 가 키에 있어야, 같은 역할을 ExternalId 없이 지목한 alias 가 ExternalId 로
        assume 한 bearer 를 받지 않는다.
        """
        if self._account_provider is None:
            raise AccountCredentialsUnavailable(
                account.role_arn,
                "alias requests a target AWS account but no account provider is wired into "
                "MantleCredentialBroker; refusing to mint with the profile's or pod's credentials",
            )
        bearer_key = (("account", account.role_arn, account.external_id or ""), profile.region)
        cached_bearer = self._bearers.get(bearer_key)
        if cached_bearer and cached_bearer.expires_at > self._now():
            return cached_bearer.token
        # STS 왕복은 브로커의 전역 lock **밖에서** 한다(provider 가 키별 lock 을 갖는다). 안에서
        # 하면 느리거나 실패하는 alias 계정 하나가 라이브 프로필 경로(cowork)의 bearer mint 까지
        # 그 왕복 뒤에 줄 세운다.
        # 만료는 **이** 자격증명의 것이다(provider 가 한 lock 안에서 함께 돌려준다). 캐시를 따로
        # 다시 읽으면 그 사이 다른 요청이 갱신한 새 항목의 만료를 받아, 옛 자격증명으로 만든
        # bearer 가 옛 자격증명이 끝난 뒤에도 _BEARER_TTL 까지 남아 401 이 된다.
        creds, cred_expiry = await self._account_provider.credentials(
            account, session_name=SESSION_NAME_MANTLE
        )
        async with self._lock:
            now = self._now()
            cached_bearer = self._bearers.get(bearer_key)
            if cached_bearer and cached_bearer.expires_at > now:
                return cached_bearer.token
            token = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: BedrockTokenGenerator().get_token(creds, profile.region),
            )
            # 프로필 경로와 같은 상한: bearer 가 그 자격증명보다 오래 살지 않는다.
            bearer_expiry = min(now + _BEARER_TTL, cred_expiry - _BEARER_CRED_SKEW)
            self._bearers[bearer_key] = _CachedBearer(token=token, expires_at=bearer_expiry)
            return token

    @staticmethod
    def _profile_key(role_arn: str, external_id: str | None) -> tuple[str, str, str]:
        return (role_arn, external_id or "", SESSION_NAME_MANTLE)

    async def _get_creds(
        self, role_arn: str, external_id: Optional[str], now: float
    ) -> Credentials:
        key = self._profile_key(role_arn, external_id)
        cached = self._creds.get(key)
        if cached and cached.expires_at - _CRED_REFRESH_SKEW > now:
            return cached.creds

        if self._profile_failures.blocked(key):  # lock 을 기다리는 사이 앞 요청이 실패했다
            usable = self._usable(key)
            if usable is None:
                raise ProfileAssumeBackoff(role_arn)
            return usable.creds
        kwargs = {
            "RoleArn": role_arn,
            "RoleSessionName": "gw-mantle",
            "DurationSeconds": _ASSUME_DURATION,
        }
        if external_id:
            kwargs["ExternalId"] = external_id

        try:
            with self._profile_failures.attempt(key):
                resp = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: self._sts.assume_role(**kwargs)
                )
        except Exception:
            # 갱신 실패 — 쓸 수 있는지는 실패한 **지금** 다시 본다(native 와 같다). 그 자격증명의
            # 만료는 호출자가 같은 캐시 항목에서 읽는다 — bearer 가 그보다 오래 살지 않는다.
            usable = self._usable(key)
            if usable is None:
                raise
            logger.warning(
                "mantle_xacct_refresh_failed_serving_cached",
                role_arn=role_arn,
                seconds_left=int(usable.expires_at - self._now()),
            )
            return usable.creds
        c = resp["Credentials"]
        creds = Credentials(c["AccessKeyId"], c["SecretAccessKey"], c["SessionToken"])
        expires_at = c["Expiration"].timestamp()
        self._creds[key] = _CachedCreds(creds=creds, expires_at=expires_at)
        # invalidate any stale bearer (all regions) minted from older creds of this key
        self._bearers = {k: v for k, v in self._bearers.items() if k[0] != key}
        return creds
