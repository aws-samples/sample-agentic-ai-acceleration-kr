# Copyright 2026 © Amazon.com and Affiliates.
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import structlog
from botocore.credentials import Credentials

from app.schemas.routing import RoutingProfileSchema

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
    tokens (30 min) to avoid per-request STS calls and token minting.
    """

    def __init__(self, sts_client, now: Callable[[], float] = time.time) -> None:
        self._sts = sts_client
        self._now = now
        self._creds: dict[str, _CachedCreds] = {}
        # Bearer cache is keyed by (cred_key, region): a bearer is region-bound
        # (SigV4 binds to the regional endpoint), so two profiles sharing a role
        # but differing region must not cross-serve tokens.
        self._bearers: dict[tuple[str, str], _CachedBearer] = {}
        self._lock = asyncio.Lock()

    def _in_account_creds(self) -> Credentials:
        """Pod's own IRSA credentials (no AssumeRole) for in-account Mantle (374)."""
        import boto3
        raw = boto3.Session().get_credentials()
        if raw is None:
            raise RuntimeError(
                "No AWS credentials available for in-account Mantle path. "
                "Ensure IRSA is configured on the pod."
            )
        frozen = raw.get_frozen_credentials()
        return Credentials(frozen.access_key, frozen.secret_key, frozen.token)

    async def bearer_token(self, profile: RoutingProfileSchema) -> str:
        if BedrockTokenGenerator is None:
            raise RuntimeError("aws-bedrock-token-generator not installed")

        cred_key = profile.account_role_arn or "in-account"
        bearer_key = (cred_key, profile.region)
        async with self._lock:
            now = self._now()
            cached_bearer = self._bearers.get(bearer_key)
            if cached_bearer and cached_bearer.expires_at > now:
                return cached_bearer.token

            if profile.account_role_arn:
                creds = await self._get_creds(profile.account_role_arn, profile.external_id, now)
                cred_expiry = self._creds[profile.account_role_arn].expires_at
            else:
                creds = await asyncio.get_running_loop().run_in_executor(None, self._in_account_creds)
                cred_expiry = now + _ASSUME_DURATION

            token = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: BedrockTokenGenerator().get_token(creds, profile.region),
            )
            # Never let the bearer outlive the underlying creds it was minted from.
            bearer_expiry = min(now + _BEARER_TTL, cred_expiry - _BEARER_CRED_SKEW)
            self._bearers[bearer_key] = _CachedBearer(token=token, expires_at=bearer_expiry)
            return token

    async def _get_creds(
        self, role_arn: str, external_id: Optional[str], now: float
    ) -> Credentials:
        cached = self._creds.get(role_arn)
        if cached and cached.expires_at - _CRED_REFRESH_SKEW > now:
            return cached.creds

        kwargs = {
            "RoleArn": role_arn,
            "RoleSessionName": "gw-mantle",
            "DurationSeconds": _ASSUME_DURATION,
        }
        if external_id:
            kwargs["ExternalId"] = external_id

        resp = await asyncio.get_running_loop().run_in_executor(
            None, lambda: self._sts.assume_role(**kwargs)
        )
        c = resp["Credentials"]
        creds = Credentials(c["AccessKeyId"], c["SecretAccessKey"], c["SessionToken"])
        expires_at = c["Expiration"].timestamp()
        self._creds[role_arn] = _CachedCreds(creds=creds, expires_at=expires_at)
        # invalidate any stale bearer (all regions) minted from older creds
        self._bearers = {k: v for k, v in self._bearers.items() if k[0] != role_arn}
        return creds
