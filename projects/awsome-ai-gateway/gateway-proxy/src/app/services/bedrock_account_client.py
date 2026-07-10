# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""Cross-account Bedrock-runtime client broker (claude-code → 374 native).

Bedrock NATIVE(boto3 invoke_model)는 Mantle 과 달리 cross-account 를 미지원했다
(startup 에 in-account 859 클라이언트 고정). 이 브로커는 대상 계정 role 을 STS AssumeRole
하여 그 계정의 bedrock-runtime 클라이언트를 빌드·캐시한다. MantleCredentialBroker 의
assume+캐시 패턴(mantle_credentials.py)을 미러하되, bearer 대신 boto3 클라이언트를 vend.

- 캐시 키 = (role_arn, region, external_id). region-bound 클라이언트 cross-serve 방지 +
  external_id 회전 시 stale 클라이언트 재사용 방지.
- static creds 는 botocore 가 자동 갱신 안 하므로, creds 만료 임박 시 **클라이언트 재빌드**.
- assume/build 는 blocking → run_in_executor. asyncio.Lock 으로 동시성 보호.
- 실패는 호출자(BedrockAdapter._get_client)가 in-account 로 투명 폴백하도록 raise.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

import boto3
import structlog

logger = structlog.get_logger(__name__)

_ASSUME_DURATION = 3600          # 1h STS 세션 (Mantle 브로커와 동일)
_CLIENT_REFRESH_SKEW = 300       # creds 하드만료 이 초 전에 클라이언트 재빌드


@dataclass
class _CachedClient:
    client: Any
    expires_at: float            # epoch sec (STS Expiration)


class BedrockAccountClientProvider:
    """대상 계정 role 을 assume 하여 bedrock-runtime 클라이언트를 vend/캐시."""

    def __init__(self, sts_client, boto_config, now: Callable[[], float] = time.time) -> None:
        self._sts = sts_client
        self._boto_config = boto_config
        self._now = now
        self._clients: dict[tuple[str, str, str], _CachedClient] = {}
        self._lock = asyncio.Lock()

    async def get_client(self, role_arn: str, region: str, external_id: Optional[str] = None):
        # 캐시 키에 external_id 포함: external_id 회전/수정 시 stale 클라이언트 재사용을 막는다
        # (external_id 는 assume 자격의 일부라 값이 바뀌면 다른 세션으로 취급해야 함).
        key = (role_arn, region, external_id or "")
        async with self._lock:
            now = self._now()
            cached = self._clients.get(key)
            if cached and cached.expires_at - _CLIENT_REFRESH_SKEW > now:
                return cached.client

            creds, expires_at = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._assume(role_arn, external_id)
            )
            client = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._build_client(region, creds)
            )
            self._clients[key] = _CachedClient(client=client, expires_at=expires_at)
            logger.info("bedrock_xacct_client_built", role_arn=role_arn, region=region)
            return client

    def _assume(self, role_arn: str, external_id: Optional[str]) -> tuple[dict, float]:
        kwargs: dict[str, Any] = {
            "RoleArn": role_arn,
            "RoleSessionName": "gw-bedrock-xacct",
            "DurationSeconds": _ASSUME_DURATION,
        }
        if external_id:
            kwargs["ExternalId"] = external_id
        resp = self._sts.assume_role(**kwargs)
        c = resp["Credentials"]
        return (
            {
                "aws_access_key_id": c["AccessKeyId"],
                "aws_secret_access_key": c["SecretAccessKey"],
                "aws_session_token": c["SessionToken"],
            },
            c["Expiration"].timestamp(),
        )

    def _build_client(self, region: str, creds: dict):
        return boto3.client(
            "bedrock-runtime",
            region_name=region,
            config=self._boto_config,
            **creds,
        )
