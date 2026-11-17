# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""OIDC JWT verifier with JWKS auto-fetch + caching.

Generic OIDC client — works with any OIDC-compliant IDP (Keycloak, Cognito,
Identity Center OIDC application, Okta, Azure AD, ...). Configuration via
admin-api Settings (OIDC_ISSUER_URL, OIDC_AUDIENCE, claim names).

Design notes
------------
- JWKS 는 issuer 의 ``.well-known/openid-configuration`` 에서 jwks_uri 를 발견한 뒤
  fetch (TTL: ``OIDC_JWKS_CACHE_TTL_SECONDS``).
- ``kid`` 매칭으로 정확한 키 선택 → key rotation 안전.
- jose 의 ``jwt.decode`` 가 sig + exp + nbf + iat + aud + iss 를 모두 검증.
- ``RS256`` 만 허용 (alg=none, HS256 차단).
- JWKS fetch 직렬화는 ``asyncio.Lock`` — blocking lock 을 ``await`` 구간에 걸면
  단일 스레드 event loop 가 영구 정지한다 (regression #51 참조).

This class is **read-only** w.r.t. database. User/team provisioning happens in
``app.services.oidc_service``.
"""
from __future__ import annotations

import asyncio
import time

import httpx
import structlog
from jose import JWTError, jwk, jwt

logger = structlog.get_logger()


class OIDCVerifyError(Exception):
    """JWT 검증 실패. 401 로 변환되어야 함."""


class OIDCConfigError(Exception):
    """IDP discovery 실패 / JWKS unreachable. 503 으로 변환."""


class OIDCVerifier:
    """OIDC JWT verifier — issuer discovery + JWKS cache + signature/claim 검증.

    멀티 worker 환경에서 worker 별 1개 인스턴스 (httpx.AsyncClient 재사용).
    동시 JWKS fetch 는 ``asyncio.Lock`` 으로 1회로 합쳐진다 — 인스턴스는 소유한
    event loop 안에서만 사용해야 하며, 다른 스레드에서 공유하면 안 된다.
    """

    _ALLOWED_ALGS = ("RS256", "RS384", "RS512")

    # 모르는 kid 로 강제 갱신하는 최소 간격(초). 검증 전 단계라 누구나 kid 를 지어낼 수 있다 —
    # 간격이 없으면 요청 하나가 IdP 로의 fetch 하나가 된다.
    _MIN_FORCED_REFRESH_INTERVAL_SECONDS = 30.0
    # TTL 이 지난 뒤 갱신에 실패했을 때, 옛 키로 버티면서 다시 시도하기까지의 간격(초).
    _STALE_RETRY_INTERVAL_SECONDS = 30.0
    # 옛 키로 버티는 상한(초) — 마지막으로 **성공한** fetch 기준. IdP 가 그사이 키를 폐기했을 수
    # 있으므로 무한정 믿지 않는다. 이 시간이 지나도 IdP 가 안 닿으면 검증을 멈춘다(503).
    _MAX_STALE_SECONDS = 24 * 60 * 60.0

    def __init__(
        self,
        issuer_url: str,
        audience: str,
        jwks_cache_ttl_seconds: int = 3600,
        http_timeout_seconds: float = 5.0,
        discovery_url_override: str | None = None,
        required_client_id: str | None = None,
    ) -> None:
        """
        Parameters
        ----------
        issuer_url:
            토큰의 ``iss`` claim 비교 대상. **이 URL 자체에서는 fetch 하지 않을 수 있음**.
        discovery_url_override:
            dev 환경 (docker / 다른 hostname) 처럼 issuer URL 을 직접 fetch 못 하는 경우.
            ``.well-known/openid-configuration`` 가 있는 base URL 을 명시적으로 지정.
            None 이면 ``issuer_url`` 로 fetch.
        required_client_id:
            채우면 **이 app client 에게 발급된 토큰만** 통과한다. ``aud`` 가 있으면 그 값과,
            없으면(Cognito access_token) ``client_id`` claim 과 대조한다. 둘 다 없으면 거부.
            ``audience`` 와 달리 claim 이 없다고 넘어가지 않는다 — jose 는 토큰에 ``aud`` 가
            없으면 audience 를 줘도 검사를 건너뛴다.

        토큰의 ``iss`` claim 검증은 항상 ``issuer_url`` 과 정확 일치를 요구한다.
        JWKS fetch 만 별도 URL 사용 가능.
        """
        if not issuer_url:
            raise ValueError("issuer_url required")
        # audience 는 optional. Cognito access_token 은 표준 `aud` claim 이 없고
        # `client_id` claim 만 가지므로 비워두면 audience 검증 skip.
        # Keycloak / Okta / Azure AD 처럼 aud 가 명시되는 IDP 는 채워야 안전.
        # trailing slash 정규화 — issuer claim 비교는 정확 일치
        self._issuer_url = issuer_url.rstrip("/")
        self._discovery_base = (discovery_url_override or issuer_url).rstrip("/")
        self._audience = audience or None
        self._required_client_id = required_client_id or None
        self._jwks_ttl = jwks_cache_ttl_seconds
        self._http_timeout = http_timeout_seconds

        self._jwks_uri: str | None = None
        self._jwks_keys: dict[str, dict] = {}  # kid -> JWK dict
        # _jwks_fetched_at 은 "다음에 언제 다시 받을까" 의 기준이라 실패 시 재시도 간격만큼
        # 당겨 쓴다. _jwks_loaded_at 은 지금 들고 있는 키를 **실제로 받은** 시각이다.
        self._jwks_fetched_at: float = 0.0
        self._jwks_loaded_at: float = 0.0
        # asyncio.Lock (NOT threading.Lock): _ensure_jwks_async 가 lock 을 잡은 채
        # await 한다. blocking lock 이면 두 번째 요청이 OS 스레드를 park 시켜
        # event loop 자체가 멈추고, JWKS 응답을 읽을 수도 없어 복구 불가.
        self._lock = asyncio.Lock()

    @property
    def issuer_url(self) -> str:
        """정규화된 issuer. 호출부가 "이 토큰이 이 verifier 의 것인가" 를 가를 때 쓴다."""
        return self._issuer_url

    # ------------------------------------------------------------------
    # Discovery + JWKS
    # ------------------------------------------------------------------

    async def _discover_jwks_uri_async(self, client: httpx.AsyncClient) -> str:
        """OIDC discovery → jwks_uri.

        ``discovery_url_override`` 가 있으면 그걸로 fetch.
        토큰 iss 검증을 위해 응답의 ``issuer`` 가 configured ``issuer_url`` 와 일치해야 함.
        """
        url = f"{self._discovery_base}/.well-known/openid-configuration"
        try:
            r = await client.get(url, timeout=self._http_timeout)
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise OIDCConfigError(f"OIDC discovery failed: {e}") from e

        jwks_uri = data.get("jwks_uri")
        if not jwks_uri:
            raise OIDCConfigError("OIDC discovery response missing jwks_uri")

        disc_issuer = (data.get("issuer") or "").rstrip("/")
        if disc_issuer != self._issuer_url:
            raise OIDCConfigError(
                f"OIDC issuer mismatch: configured={self._issuer_url} discovered={disc_issuer}"
            )

        # JWKS URL 도 discovery base 기준으로 rewrite — discovery 가 다른 호스트로 갔으면
        # jwks_uri 도 같은 호스트로 가야 admin-api 가 접근 가능.
        if self._discovery_base != self._issuer_url:
            from urllib.parse import urlparse, urlunparse
            jwks_parsed = urlparse(jwks_uri)
            disc_parsed = urlparse(self._discovery_base)
            jwks_uri = urlunparse((
                disc_parsed.scheme, disc_parsed.netloc,
                jwks_parsed.path, jwks_parsed.params,
                jwks_parsed.query, jwks_parsed.fragment,
            ))
        return jwks_uri

    async def _fetch_jwks_async(self, client: httpx.AsyncClient) -> dict[str, dict]:
        if self._jwks_uri is None:
            self._jwks_uri = await self._discover_jwks_uri_async(client)

        try:
            r = await client.get(self._jwks_uri, timeout=self._http_timeout)
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise OIDCConfigError(f"JWKS fetch failed: {e}") from e

        keys = data.get("keys") or []
        # alg 가 sig 전용인 키만 (Keycloak 은 enc 키도 같이 노출)
        result: dict[str, dict] = {}
        for k in keys:
            kid = k.get("kid")
            use = k.get("use", "sig")
            if not kid or use != "sig":
                continue
            result[kid] = k
        if not result:
            raise OIDCConfigError("JWKS contains no signing keys (use=sig)")
        return result

    def _jwks_is_fresh(self, force: bool) -> bool:
        if not self._jwks_keys:
            return False
        age = time.monotonic() - self._jwks_fetched_at
        if force:
            # 방금 받은 키라면 다시 받아도 같은 답이다 — 지어낸 kid 가 fetch 를 유발하지 못하게 한다.
            return age < self._MIN_FORCED_REFRESH_INTERVAL_SECONDS
        return age < self._jwks_ttl

    async def _ensure_jwks_async(self, client: httpx.AsyncClient, force: bool = False) -> None:
        if self._jwks_is_fresh(force):
            return
        async with self._lock:
            # 이중 체크 — lock 진입 사이 다른 호출이 갱신했을 수 있음
            if self._jwks_is_fresh(force):
                return
            try:
                keys = await self._fetch_jwks_async(client)
            except OIDCConfigError as e:
                # TTL 만료로 인한 정기 갱신이 실패했고 옛 키가 있으면 그 키로 계속 검증한다.
                # 키 회전은 드물고, IdP 가 잠깐 안 닿는다고 유효한 세션을 전부 503 으로
                # 떨어뜨릴 이유가 없다. 모르는 kid 로 온 강제 갱신(force)은 그대로 올린다 —
                # 그때는 옛 키로 답할 수 없다. 옛 키가 상한보다 오래됐을 때도 올린다.
                too_stale = (time.monotonic() - self._jwks_loaded_at) > self._MAX_STALE_SECONDS
                if force or not self._jwks_keys or too_stale:
                    raise
                self._jwks_fetched_at = (
                    time.monotonic() - self._jwks_ttl + self._STALE_RETRY_INTERVAL_SECONDS
                )
                logger.warning(
                    "oidc_verifier.jwks_refresh_failed_using_stale_keys",
                    issuer=self._issuer_url,
                    key_count=len(self._jwks_keys),
                    error=str(e),
                )
                return
            self._jwks_keys = keys
            self._jwks_fetched_at = self._jwks_loaded_at = time.monotonic()
            logger.info(
                "oidc_verifier.jwks_loaded",
                issuer=self._issuer_url,
                key_count=len(keys),
            )

    # ------------------------------------------------------------------
    # Verify
    # ------------------------------------------------------------------

    async def verify_async(self, token: str, http_client: httpx.AsyncClient) -> dict:
        """검증 성공 시 JWT payload (claims) 반환. 실패 시 OIDCVerifyError."""
        if not token or token.count(".") != 2:
            raise OIDCVerifyError("malformed token")

        # 1. unverified header → kid
        try:
            header = jwt.get_unverified_header(token)
        except JWTError as e:
            raise OIDCVerifyError(f"invalid header: {e}") from e

        kid = header.get("kid")
        alg = header.get("alg")
        if alg not in self._ALLOWED_ALGS:
            raise OIDCVerifyError(f"algorithm not allowed: {alg}")

        # 2. JWKS 확보 (필요 시 fetch)
        try:
            await self._ensure_jwks_async(http_client)
        except OIDCConfigError:
            raise

        # 3. kid 로 키 매칭. 없으면 force-refresh (rotation 시나리오).
        key_dict = self._jwks_keys.get(kid) if kid else None
        if key_dict is None:
            await self._ensure_jwks_async(http_client, force=True)
            key_dict = self._jwks_keys.get(kid) if kid else None
        if key_dict is None:
            raise OIDCVerifyError(f"unknown kid: {kid}")

        # 4. 서명 + 표준 claim 검증 (jose 가 exp/nbf/iat/aud/iss 모두 검증)
        try:
            public_key = jwk.construct(key_dict, algorithm=alg)
            decode_kwargs = {
                "algorithms": [alg],
                "issuer": self._issuer_url,
                "options": {
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_nbf": True,
                    "verify_iat": True,
                    "verify_aud": self._audience is not None,
                    "verify_iss": True,
                    "require_exp": True,
                    "require_iat": True,
                    # id_token 의 at_hash claim 은 access_token 과 binding 검증용.
                    # gateway-cli 가 id_token 만 보내므로 access_token 없이 비활성.
                    # at_hash 자체는 token-binding 보안인데, 우리는 issuer/audience/sig 로 충분.
                    "verify_at_hash": False,
                },
            }
            if self._audience is not None:
                decode_kwargs["audience"] = self._audience
            payload = jwt.decode(
                token,
                public_key.to_pem().decode("utf-8"),
                **decode_kwargs,
            )
        except JWTError as e:
            raise OIDCVerifyError(str(e)) from e

        # 5. sub 필수 (auto-provisioning 식별자)
        if not payload.get("sub"):
            raise OIDCVerifyError("missing 'sub' claim")

        # 6. 발급 대상 client 대조 (required_client_id 를 준 verifier 만)
        if self._required_client_id is not None and not self._issued_to_required_client(payload):
            raise OIDCVerifyError("token was not issued to the expected client")

        return payload

    def _issued_to_required_client(self, payload: dict) -> bool:
        """``aud`` 가 있으면 그것으로, 없으면 ``client_id`` 로 발급 대상을 판정한다.

        id_token 은 ``aud`` 에 app client id 를 싣는다. Cognito access_token 은 ``aud`` 가
        없고 ``client_id`` 에 싣는다. ``aud`` 가 있는데 맞지 않으면 ``client_id`` 로 넘어가지
        않는다 — 두 claim 중 하나만 맞아도 통과시키면 대조가 느슨해진다.
        """
        aud = payload.get("aud")
        if aud is not None:
            audiences = aud if isinstance(aud, list) else [aud]
            return self._required_client_id in audiences
        return payload.get("client_id") == self._required_client_id
