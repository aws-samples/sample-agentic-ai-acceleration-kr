"""Slack Web API over httpx, plus the one Socket Mode handshake the token check needs.

Not slack_sdk: its async client needs aiohttp, which nothing else here uses.
The surface is small — a handful of methods — so a thin wrapper is cheaper
than the dependency.

Tokens go into the Authorization header and nowhere else. No exception, log
line or return value built here carries one: `SlackApiError` holds the method
and Slack's error code only.
"""
import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Optional

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://slack.com/api/"

# Scopes the manifest asks for, checked against `x-oauth-scopes` on any Web API
# response. im:history is what delivers `message.im`; users:read covers
# bots.info and users.info; users:read.email puts the email on users.info.
REQUIRED_BOT_SCOPES = ("chat:write", "im:history", "users:read", "users:read.email")
REQUIRED_APP_SCOPES = ("connections:write",)

# Error codes that mean the token itself is dead or wrong, not that Slack is
# having a bad minute. A connection seeing one of these stops retrying.
AUTH_ERRORS = frozenset(
    {
        "invalid_auth",
        "not_authed",
        "token_revoked",
        "token_expired",
        "account_inactive",
        "not_allowed_token_type",
        "invalid_token",
        "missing_scope",
        "no_permission",
        "ekm_access_denied",
        "app_uninstalled",
    }
)

# How long to honour a 429's Retry-After before giving up on the call. Slack's
# tier limits are per minute, so a wait longer than this is better retried by
# whoever called than slept through here.
MAX_RETRY_AFTER_SECONDS = 30
MAX_RATE_LIMIT_RETRIES = 3


class SlackApiError(Exception):
    """A Web API call answered `ok: false` (or could not be made).

    `error` is Slack's code (`invalid_auth`, `channel_not_found`, …) or one of
    ours: `http_<status>`, `network_error`, `rate_limited`, `bad_response`.
    """

    def __init__(self, method: str, error: str):
        super().__init__(f"{method}: {error}")
        self.method = method
        self.error = error

    @property
    def is_auth_error(self) -> bool:
        return self.error in AUTH_ERRORS


@dataclass
class SlackResponse:
    data: Dict[str, Any]
    scopes: FrozenSet[str] = field(default_factory=frozenset)


def _scopes(headers: httpx.Headers) -> FrozenSet[str]:
    raw = headers.get("x-oauth-scopes") or ""
    return frozenset(s.strip() for s in raw.split(",") if s.strip())


def _retry_after(headers: httpx.Headers) -> float:
    try:
        return max(0.0, float(headers.get("retry-after") or 1))
    except ValueError:
        return 1.0


class SlackApi:
    def __init__(self, client: Optional[httpx.AsyncClient] = None, timeout: float = 15.0):
        self._client = client
        self._timeout = timeout
        # Tests replace this to skip the waits.
        self._sleep = asyncio.sleep

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=API_BASE, timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def call(self, method: str, token: str, **params: Any) -> SlackResponse:
        """POST one method, form-encoded. 429 waits Retry-After and tries again."""
        body = {k: v for k, v in params.items() if v is not None}
        for k, v in list(body.items()):
            if isinstance(v, bool):
                body[k] = "true" if v else "false"
            elif isinstance(v, (dict, list)):
                body[k] = json.dumps(v)
        attempts = 0
        while True:
            try:
                resp = await self._http().post(
                    method,
                    data=body,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.HTTPError as exc:
                # The exception text can include the request; never pass it on.
                logger.warning("Slack %s failed: %s", method, type(exc).__name__)
                raise SlackApiError(method, "network_error") from None
            if resp.status_code == 429:
                wait = _retry_after(resp.headers)
                attempts += 1
                if attempts > MAX_RATE_LIMIT_RETRIES or wait > MAX_RETRY_AFTER_SECONDS:
                    raise SlackApiError(method, "rate_limited")
                logger.info("Slack %s rate limited; retrying in %.0fs", method, wait)
                await self._sleep(wait)
                continue
            if resp.status_code >= 400:
                raise SlackApiError(method, f"http_{resp.status_code}")
            try:
                data = resp.json()
            except ValueError:
                raise SlackApiError(method, "bad_response") from None
            if not isinstance(data, dict):
                raise SlackApiError(method, "bad_response")
            if not data.get("ok"):
                raise SlackApiError(method, str(data.get("error") or "unknown_error"))
            return SlackResponse(data=data, scopes=_scopes(resp.headers))

    # ── methods ──────────────────────────────────────────────────────────────

    async def auth_test(self, token: str) -> SlackResponse:
        return await self.call("auth.test", token)

    async def bot_app_id(self, token: str, bot_id: str) -> str:
        resp = await self.call("bots.info", token, bot=bot_id)
        return str(((resp.data.get("bot") or {}).get("app_id")) or "")

    async def user_info(self, token: str, user_id: str) -> Dict[str, Any]:
        resp = await self.call("users.info", token, user=user_id)
        return resp.data.get("user") or {}

    async def open_connection(self, app_token: str) -> SlackResponse:
        """apps.connections.open: a single-use Socket Mode WebSocket URL."""
        return await self.call("apps.connections.open", app_token)

    async def post_message(
        self,
        token: str,
        channel: str,
        *,
        thread_ts: Optional[str] = None,
        text: Optional[str] = None,
        markdown_text: Optional[str] = None,
    ) -> str:
        resp = await self.call(
            "chat.postMessage",
            token,
            channel=channel,
            thread_ts=thread_ts,
            text=text,
            markdown_text=markdown_text,
            unfurl_links=False,
            unfurl_media=False,
        )
        return str(resp.data.get("ts") or "")

    async def update_message(
        self,
        token: str,
        channel: str,
        ts: str,
        *,
        text: Optional[str] = None,
        markdown_text: Optional[str] = None,
    ) -> None:
        await self.call(
            "chat.update", token, channel=channel, ts=ts, text=text, markdown_text=markdown_text
        )

    async def revoke(self, token: str) -> None:
        await self.call("auth.revoke", token)


async def socket_app_id(url: str, timeout: float = 10.0) -> str:
    """Open a Socket Mode connection, read `hello`, close. Returns its app id.

    The only documented way to learn which app an App-Level Token (`xapp-`)
    belongs to: the token string's format is not documented, so it is not
    parsed. The connection is closed the moment `hello` arrives, so it never
    receives (and never fails to acknowledge) an event.
    """
    import websockets

    async with asyncio.timeout(timeout):
        async with websockets.connect(url, open_timeout=timeout, close_timeout=2) as ws:
            while True:
                raw = await ws.recv()
                try:
                    message = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if message.get("type") == "hello":
                    info = message.get("connection_info") or {}
                    return str(info.get("app_id") or "")
