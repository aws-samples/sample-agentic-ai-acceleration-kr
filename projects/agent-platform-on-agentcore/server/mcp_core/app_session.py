"""MCP Apps 전용 세션 유틸.

Strands MCPClient에는 read_resource가 없어서 ui:// 리소스를 가져올 수 없다. 그래서
resources/read 경로만 raw MCP SDK를 쓴다.
"""
import base64
import logging
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict

import anyio
from mcp.client.session import SUPPORTED_PROTOCOL_VERSIONS
from mcp.types import (
    ClientCapabilities,
    ClientNotification,
    ClientRequest,
    InitializeRequest,
    InitializeRequestParams,
    InitializeResult,
    InitializedNotification,
    LATEST_PROTOCOL_VERSION,
)

from mcp_core.ui_meta import APP_MIME_TYPE, ui_client_capabilities

logger = logging.getLogger(__name__)


@dataclass
class UiResourcePayload:
    """호스트에 넘길 ui:// 리소스 내용."""

    uri: str
    mime_type: str
    text: str
    ui_meta: Dict[str, Any] = field(default_factory=dict)


def build_client_capabilities() -> ClientCapabilities:
    """MCP Apps 지원을 선언하는 ClientCapabilities.

    설치된 MCP SDK의 ClientCapabilities에는 `extensions` 필드가 없다 (SEP-1724 이전).
    모델이 extra="allow"라서 추가 필드가 그대로 직렬화되므로 이렇게 통과시킨다.
    SDK가 extensions를 정식 지원하면 이 우회는 제거 대상이다.
    """
    return ClientCapabilities(**ui_client_capabilities())


def _content_text(content: Any) -> str:
    text = getattr(content, "text", None)
    if isinstance(text, str) and text:
        return text

    blob = getattr(content, "blob", None)
    if isinstance(blob, str) and blob:
        # base64/UTF-8 오류를 ValueError로 바꾼다. 이 함수의 다른 거부 경로가 모두
        # ValueError이고, 라우트가 그것만 400으로 잡는다 — 그냥 두면 악성 서버의
        # 잘못된 blob이 500이 된다.
        try:
            return base64.b64decode(blob, validate=True).decode("utf-8")
        except Exception as exc:
            raise ValueError(f"blob을 디코딩할 수 없습니다: {exc}") from exc

    raise ValueError("리소스에 text도 blob도 없습니다.")


def parse_resource_result(result: Any, expected_uri: str) -> UiResourcePayload:
    """resources/read 응답을 검증하고 UiResourcePayload로 만든다.

    검증을 여기서 하는 이유: 이 HTML은 신뢰할 수 없는 MCP 서버가 보낸 것이고 곧바로
    샌드박스 iframe에 주입된다. URI와 mimeType이 어긋나면 앱으로 취급하지 않는다.
    """
    contents = getattr(result, "contents", None) or []
    if not contents:
        raise ValueError("resources/read 응답의 contents가 비어 있습니다.")

    content = contents[0]

    uri = str(getattr(content, "uri", "") or "")
    if uri != expected_uri:
        raise ValueError(
            f"요청한 URI와 응답이 다릅니다: 요청={expected_uri!r} 응답={uri!r}"
        )

    mime_type = getattr(content, "mimeType", None)
    if mime_type != APP_MIME_TYPE:
        raise ValueError(
            f"지원하지 않는 mimeType입니다: {mime_type!r} (기대: {APP_MIME_TYPE!r})"
        )

    meta = getattr(content, "meta", None)
    if not isinstance(meta, dict):
        meta = getattr(content, "_meta", None)
    ui_meta = meta.get("ui") if isinstance(meta, dict) else None

    return UiResourcePayload(
        uri=uri,
        mime_type=mime_type,
        text=_content_text(content),
        ui_meta=ui_meta if isinstance(ui_meta, dict) else {},
    )


AGENTCORE_HOST_SUFFIX = ".amazonaws.com"
AGENTCORE_SERVICE = "bedrock-agentcore"


def _is_agentcore_runtime_url(url: str) -> bool:
    """AgentCore Runtime의 MCP 엔드포인트인가.

    형식: https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{arn}/invocations
    이 엔드포인트는 SigV4를 요구하므로 서명 없이는 403이 된다.
    """
    return AGENTCORE_SERVICE in url and "/runtimes/" in url and AGENTCORE_HOST_SUFFIX in url


def _is_agentcore_gateway_url(url: str) -> bool:
    """AWS_IAM 게이트웨이의 MCP 엔드포인트인가.

    형식: https://{id}.gateway.bedrock-agentcore.{region}.amazonaws.com/mcp

    런타임과 따로 판정하는 이유는 호스트 모양이 다르기 때문이다 — 런타임은 서비스가
    호스트 선두에 오지만 게이트웨이는 `{id}.gateway.` 뒤에 온다. CUSTOM_JWT
    게이트웨이도 같은 모양이라 여기서는 구분할 수 없다. 그쪽은 서명을 무시하고
    Authorization 헤더를 보므로, 서명을 붙여도 실패 이유가 달라지지 않는다.
    """
    return f".gateway.{AGENTCORE_SERVICE}." in url and AGENTCORE_HOST_SUFFIX in url


def _signing_region(url: str) -> str:
    """서명에 쓸 리전을 엔드포인트 호스트에서 뽑는다."""
    return url.split(f"{AGENTCORE_SERVICE}.", 1)[1].split(".", 1)[0]


def _sigv4_client_factory(url: str):
    """AgentCore 엔드포인트면 SigV4로 서명하는 httpx 클라이언트 팩토리를 준다.

    정적 헤더로는 안 되는 이유: SigV4는 요청 본문과 경로까지 서명에 포함하므로
    요청마다 다시 계산해야 한다. 그래서 httpx auth 훅으로 붙인다.
    """
    if not (_is_agentcore_runtime_url(url) or _is_agentcore_gateway_url(url)):
        return None

    import boto3
    import httpx
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    region = _signing_region(url)
    credentials = boto3.Session().get_credentials()
    if credentials is None:
        raise RuntimeError(
            "AgentCore MCP 엔드포인트는 SigV4가 필요하지만 AWS 자격증명을 찾을 수 없습니다."
        )

    class _SigV4(httpx.Auth):
        requires_request_body = True

        def auth_flow(self, request):
            aws_request = AWSRequest(
                method=request.method,
                url=str(request.url),
                data=request.content or b"",
                headers={
                    k: v
                    for k, v in request.headers.items()
                    # 이 세 개는 서명 대상이 아니거나 전송 계층이 다시 채운다.
                    if k.lower() not in ("authorization", "connection", "host")
                },
            )
            SigV4Auth(
                credentials.get_frozen_credentials(), AGENTCORE_SERVICE, region
            ).add_auth(aws_request)
            for key, value in aws_request.headers.items():
                request.headers[key] = value
            yield request

    def factory(headers=None, timeout=None, auth=None):
        return httpx.AsyncClient(
            headers=headers,
            auth=_SigV4(),
            timeout=timeout or httpx.Timeout(120.0),
            follow_redirects=True,
        )

    return factory


@asynccontextmanager
async def _async_session(url, streamablehttp_client, ClientSession):
    """Open an async MCP session with UI capabilities enabled."""
    from mcp.types import Implementation

    factory = _sigv4_client_factory(url)
    client_kwargs = {"httpx_client_factory": factory} if factory else {}

    async with streamablehttp_client(url, **client_kwargs) as (read, write, _):
        async with ClientSession(read, write) as session:
            # Hand-roll the initialization because SDK's ClientSession.initialize() hardcodes
            # capabilities internally and does not accept ClientCapabilities as a parameter.
            # SEP-1724 predates SDK support for extensions, so we must inject them via the
            # initialize request params directly.
            req = InitializeRequest(
                params=InitializeRequestParams(
                    protocolVersion=LATEST_PROTOCOL_VERSION,
                    capabilities=build_client_capabilities(),
                    clientInfo=Implementation(name="bap", version="1.0.0"),
                )
            )
            result = await session.send_request(
                ClientRequest(root=req),
                InitializeResult,
            )
            if result.protocolVersion not in SUPPORTED_PROTOCOL_VERSIONS:
                raise RuntimeError(f"Unsupported protocol version from the server: {result.protocolVersion}")
            await session.send_notification(ClientNotification(InitializedNotification()))
            yield session


@contextmanager
def open_session(url: str):
    """Streamable-HTTP MCP session. Initializes with MCP Apps capability enabled.

    Strands MCPClient는 read_resource가 없어서 이 저수준 API를 쓴다.
    """
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    class _Sync:
        def __init__(self, portal, session):
            self._portal = portal
            self._session = session

        def read_resource(self, uri):
            return self._portal.call(self._session.read_resource, uri)

        def list_tools(self):
            return self._portal.call(self._session.list_tools)

        def call_tool(self, name, arguments):
            return self._portal.call(self._session.call_tool, name, arguments)

    with anyio.from_thread.start_blocking_portal() as portal:
        with portal.wrap_async_context_manager(
            _async_session(url, streamablehttp_client, ClientSession)
        ) as session:
            yield _Sync(portal, session)


def list_tools(url: str, timeout: float | None = None, *, _client_factory=None, _session_cls=None):
    """List an MCP server's tools, optionally time-bounded so a stuck endpoint
    fails fast instead of hanging.

    Why the bound matters: app discovery (`McpAppsRelay._walk_app_tools`) opens a
    session to *every* MCP record, and it runs inside the live SSE turn. A record
    whose stored endpoint is malformed — a gateway URL missing its `/mcp` path was
    the real case — answers with a non-JSON-RPC body; the SDK client then waits for
    a valid message that never arrives and blocks forever. The `try/except` that is
    meant to skip a bad record never fires, because a hang is not an exception, so
    the whole turn stalls (the browser shows infinite loading). `anyio.fail_after`
    turns that hang into a `TimeoutError` the caller catches and skips.

    Unlike `open_session`, the whole session lifecycle — connect, initialize,
    list — is inside one coroutine, so the bound can span the initialize round-trip
    where the hang actually happens, not just the list call. The `_client_factory`
    / `_session_cls` seams exist for tests; production passes neither.
    """
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    client = _client_factory or streamablehttp_client
    session_cls = _session_cls or ClientSession

    async def _run():
        cm = _async_session(url, client, session_cls)
        if timeout is None:
            async with cm as session:
                return (await session.list_tools()).tools
        with anyio.fail_after(timeout):
            async with cm as session:
                return (await session.list_tools()).tools

    with anyio.from_thread.start_blocking_portal() as portal:
        return portal.call(_run)
