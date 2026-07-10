# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""AgentCore Gateway MCP client — raw MCP-over-httpx with SigV4 (AWS_IAM inbound).

The gateway-proxy calls Amazon Bedrock AgentCore Gateway's managed **WebSearch**
connector to power the server-side web-search loop (see services/web_search_loop.py).

Why raw httpx instead of the `mcp` SDK: AgentCore Gateway supports AWS_IAM inbound
auth (SigV4). Our pod has IRSA credentials, so we sign each MCP request with SigV4
(service ``bedrock-agentcore``) — no Cognito/JWT needed. SigV4 signs the *exact*
request body, which the MCP SDK does not let us control, so we speak the JSON-RPC /
Streamable-HTTP wire directly over the shared httpx client.

Protocol facts (verified against AWS docs + gonsoomoon-ml/web-search-mcp):
- Endpoint: POST ``https://<gw-id>.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp``
- Transport: Streamable-HTTP. Responses arrive as ``application/json`` OR ``text/event-stream``.
- Methods: ``initialize`` → ``notifications/initialized`` → ``tools/list`` / ``tools/call``.
- WebSearch tool is named ``<target_id>___WebSearch``; input ``{query(<=200), maxResults(1-25)}``;
  result content[].type=="text" carrying JSON ``{results:[{text,url,title,publishedDate}]}``.

The loop, not this client, decides what to do on failure (inject an error tool_result
so the model can continue) — so ``search`` raises typed errors rather than degrading.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import httpx
import structlog
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

logger = structlog.get_logger(__name__)

# MCP protocol version we advertise (AgentCore supports 2025-03-26 / 2025-06-18 / 2025-11-25).
_MCP_PROTOCOL_VERSION = "2025-03-26"
# Cache frozen IRSA creds briefly to avoid a blocking fetch on every search; SigV4
# still signs per-call with a fresh timestamp. Well under IRSA token lifetime.
_CRED_CACHE_TTL = 60.0
# The suffix AgentCore appends to the connector tool at listing time: "<target>___WebSearch".
_WEB_SEARCH_SUFFIX = "___WebSearch"


class AgentCoreMcpError(Exception):
    """Base for AgentCore MCP failures (the caller degrades gracefully)."""


class AgentCoreMcpTimeout(AgentCoreMcpError):
    """The MCP endpoint did not respond within the configured timeout."""


class AgentCoreMcpProtocolError(AgentCoreMcpError):
    """A JSON-RPC error, malformed response, or tool-not-found."""


@dataclass
class WebSearchResultItem:
    text: str
    url: str = ""
    title: str = ""
    published_date: Optional[str] = None


@dataclass
class WebSearchResponse:
    results: list[WebSearchResultItem]
    # The provider's raw text payload, fed back to the model VERBATIM so we never
    # lose fidelity (citations, ordering) through re-serialization.
    raw_text: str


@dataclass
class _CachedCreds:
    creds: Credentials
    expires_at: float


class AgentCoreMcpClient:
    """MCP client for AgentCore Gateway's managed WebSearch connector.

    Framework-agnostic: raw JSON-RPC over an injected httpx.AsyncClient, SigV4-signed
    with IRSA creds. ``ensure_initialized`` runs ``initialize`` + ``tools/list`` once
    (lock-guarded) and caches the resolved tool name + input schema so the router can
    inject a client-facing web_search tool definition without a per-request round-trip.
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        gateway_url: str,
        *,
        region: str = "us-east-1",
        target_id: str = "",
        timeout: float = 30.0,
        session_provider: Callable[[], Credentials] | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._http = http_client
        self._url = gateway_url.rstrip("/") if gateway_url else ""
        self._region = region
        self._target_id = target_id
        self._timeout = timeout
        self._session_provider = session_provider or self._default_creds
        self._now = now

        self._lock = asyncio.Lock()
        self._initialized = False
        self._tool_name: Optional[str] = None
        self._tool_input_schema: Optional[dict] = None
        self._mcp_session_id: Optional[str] = None
        self._cached_creds: Optional[_CachedCreds] = None
        self._rpc_id = 0

    # ── credentials / signing ────────────────────────────────────────────────
    @staticmethod
    def _default_creds() -> Credentials:
        """Pod IRSA credentials (frozen). Mirrors MantleCredentialBroker._in_account_creds."""
        import boto3

        raw = boto3.Session().get_credentials()
        if raw is None:
            raise AgentCoreMcpError(
                "No AWS credentials for AgentCore MCP (ensure IRSA is configured on the pod)."
            )
        frozen = raw.get_frozen_credentials()
        return Credentials(frozen.access_key, frozen.secret_key, frozen.token)

    async def _frozen_creds(self) -> Credentials:
        now = self._now()
        cached = self._cached_creds
        if cached and cached.expires_at > now:
            return cached.creds
        # get_frozen_credentials / boto3 session is blocking → run off the event loop.
        creds = await asyncio.get_running_loop().run_in_executor(None, self._session_provider)
        self._cached_creds = _CachedCreds(creds=creds, expires_at=now + _CRED_CACHE_TTL)
        return creds

    def _sign_headers(self, body: bytes, creds: Credentials, extra: dict[str, str]) -> dict[str, str]:
        """SigV4-sign a POST to the MCP endpoint (service bedrock-agentcore)."""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        headers.update(extra)
        req = AWSRequest(method="POST", url=self._url, data=body, headers=headers)
        SigV4Auth(creds, "bedrock-agentcore", self._region).add_auth(req)
        return dict(req.headers)

    # ── JSON-RPC transport ─────────────────────────────────────────────────────
    def _next_id(self) -> int:
        self._rpc_id += 1
        return self._rpc_id

    async def _post_jsonrpc(
        self, method: str, params: Optional[dict], *, is_notification: bool = False
    ) -> Optional[dict]:
        """POST one JSON-RPC message. Returns the parsed ``result`` (None for notifications).

        Handles both ``application/json`` and ``text/event-stream`` responses.
        """
        if not self._url:
            raise AgentCoreMcpError("AgentCore gateway URL is not configured.")

        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not is_notification:
            payload["id"] = self._next_id()
        body = json.dumps(payload).encode()

        extra: dict[str, str] = {}
        if self._mcp_session_id:
            extra["Mcp-Session-Id"] = self._mcp_session_id
        creds = await self._frozen_creds()
        headers = self._sign_headers(body, creds, extra)

        try:
            resp = await self._http.post(
                self._url, content=body, headers=headers, timeout=self._timeout
            )
        except httpx.TimeoutException as e:
            raise AgentCoreMcpTimeout(f"AgentCore MCP timeout on {method}") from e
        except httpx.HTTPError as e:
            raise AgentCoreMcpError(f"AgentCore MCP transport error on {method}: {e}") from e

        # Capture (or refresh) the session id the gateway may hand back on initialize.
        sid = resp.headers.get("Mcp-Session-Id") or resp.headers.get("mcp-session-id")
        if sid:
            self._mcp_session_id = sid

        if is_notification:
            return None

        if resp.status_code >= 400:
            raise AgentCoreMcpProtocolError(
                f"AgentCore MCP HTTP {resp.status_code} on {method}: {resp.text[:300]}"
            )

        message = self._parse_response(resp, method)
        if "error" in message:
            raise AgentCoreMcpProtocolError(
                f"AgentCore MCP JSON-RPC error on {method}: {json.dumps(message['error'])[:300]}"
            )
        return message.get("result", {})

    def _parse_response(self, resp: httpx.Response, method: str) -> dict:
        """Return the JSON-RPC object from a JSON or SSE (text/event-stream) response."""
        ctype = resp.headers.get("content-type", "")
        text = resp.text
        if "text/event-stream" in ctype:
            # Take the LAST `data:` JSON line that carries a JSON-RPC response.
            last: Optional[dict] = None
            for line in text.splitlines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and ("result" in obj or "error" in obj):
                    last = obj
            if last is None:
                raise AgentCoreMcpProtocolError(
                    f"AgentCore MCP: no JSON-RPC data in SSE response to {method}"
                )
            return last
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as e:
            raise AgentCoreMcpProtocolError(
                f"AgentCore MCP: non-JSON response to {method}: {text[:200]}"
            ) from e
        if not isinstance(obj, dict):
            raise AgentCoreMcpProtocolError(f"AgentCore MCP: unexpected response shape to {method}")
        return obj

    # ── public API ──────────────────────────────────────────────────────────────
    async def ensure_initialized(self) -> str:
        """Run initialize + tools/list once; cache the WebSearch tool name + schema.

        Returns the resolved tool name. Safe to call on every request (cached after
        first success). Re-discovers after a protocol error (see ``search``).
        """
        if self._initialized and self._tool_name:
            return self._tool_name
        async with self._lock:
            if self._initialized and self._tool_name:
                return self._tool_name

            await self._post_jsonrpc(
                "initialize",
                {
                    "protocolVersion": _MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "llm-gateway", "version": "1.0"},
                },
            )
            # Per MCP: notify the server the handshake is complete before listing.
            try:
                await self._post_jsonrpc("notifications/initialized", {}, is_notification=True)
            except AgentCoreMcpError:
                # Stateless gateways may reject/ignore this; not fatal.
                logger.debug("agentcore_mcp.initialized_notify_skipped")

            result = await self._post_jsonrpc("tools/list", {})
            tools = (result or {}).get("tools", []) if isinstance(result, dict) else []
            name = self._resolve_web_search_tool(tools)
            if not name:
                raise AgentCoreMcpProtocolError(
                    f"AgentCore MCP: no WebSearch tool found in tools/list "
                    f"(saw: {[t.get('name') for t in tools if isinstance(t, dict)]})"
                )
            self._tool_name = name
            self._tool_input_schema = next(
                (t.get("inputSchema") for t in tools if t.get("name") == name), None
            )
            self._initialized = True
            logger.info("agentcore_mcp.initialized", tool=name)
            return name

    def _resolve_web_search_tool(self, tools: list) -> Optional[str]:
        """Prefer exact ``<target_id>___WebSearch``; else any name ending in ___WebSearch;
        else any name containing 'websearch' (case-insensitive)."""
        names = [t.get("name", "") for t in tools if isinstance(t, dict)]
        if self._target_id:
            exact = f"{self._target_id}{_WEB_SEARCH_SUFFIX}"
            if exact in names:
                return exact
        suffix = next((n for n in names if n.endswith(_WEB_SEARCH_SUFFIX)), None)
        if suffix:
            return suffix
        return next((n for n in names if "websearch" in n.lower()), None)

    def tool_input_schema(self) -> Optional[dict]:
        """Cached MCP input schema for the WebSearch tool (after ensure_initialized)."""
        return self._tool_input_schema

    async def search(self, query: str, max_results: int = 10) -> WebSearchResponse:
        """Run one WebSearch tools/call. Clamps inputs to the connector's limits.

        Raises AgentCoreMcpError subclasses on failure (caller injects an error
        tool_result). Empty results are NOT an error.
        """
        tool_name = await self.ensure_initialized()
        q = (query or "").strip()[:200]  # connector limit: query <= 200 chars
        if not q:
            raise AgentCoreMcpProtocolError("AgentCore MCP: empty query")
        n = max(1, min(int(max_results or 10), 25))  # connector limit: 1..25

        try:
            result = await self._post_jsonrpc(
                "tools/call", {"name": tool_name, "arguments": {"query": q, "maxResults": n}}
            )
        except AgentCoreMcpProtocolError:
            # Tool set may have changed under us → invalidate and let the next call re-discover.
            self._initialized = False
            self._tool_name = None
            raise

        # MCP tools/call can return a successful JSON-RPC envelope but signal a TOOL-level
        # failure via result.isError:true — treat that as a failed search, not a hit (F-8).
        if isinstance(result, dict) and result.get("isError"):
            content = result.get("content", [])
            detail = ""
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text":
                    detail = (b.get("text") or "")[:200]
                    break
            raise AgentCoreMcpProtocolError(f"AgentCore MCP tools/call isError: {detail}")

        return self._parse_search_result(result or {})

    def _parse_search_result(self, result: dict) -> WebSearchResponse:
        """Extract results[] from the MCP tools/call result (content[].type=='text' JSON)."""
        content = result.get("content", []) if isinstance(result, dict) else []
        text_payload = ""
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text_payload = block.get("text", "") or ""
                if text_payload:
                    break
        if not text_payload:
            return WebSearchResponse(results=[], raw_text="{\"results\": []}")

        items: list[WebSearchResultItem] = []
        try:
            data = json.loads(text_payload)
        except json.JSONDecodeError:
            # Not JSON — hand the raw text back to the model as-is.
            return WebSearchResponse(results=[], raw_text=text_payload)

        raw_list = data.get("results", data) if isinstance(data, dict) else data
        if isinstance(raw_list, list):
            for r in raw_list:
                if not isinstance(r, dict):
                    continue
                items.append(
                    WebSearchResultItem(
                        text=r.get("text") or r.get("snippet") or "",
                        url=r.get("url", "") or "",
                        title=r.get("title", "") or "",
                        published_date=r.get("publishedDate") or r.get("published_date"),
                    )
                )
        return WebSearchResponse(results=items, raw_text=text_payload)
