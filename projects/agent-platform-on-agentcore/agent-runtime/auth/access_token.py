"""게이트웨이(AWS_IAM) MCP 툴 로딩. 실행 역할 SigV4로 서명한다."""
import logging
from mcp.client.streamable_http import streamablehttp_client
from auth.sigv4 import sigv4_httpx_client_factory

logger = logging.getLogger(__name__)


def load_tools_from_mcp(gateway_endpoint, session_scope=None):
    """SigV4로 서명해 MCP 서버에서 툴을 로드한다.

    ``session_scope``는 이 게이트웨이엔 인터셉터가 없어 무시된다(stateless 툴).
    시그니처는 호출부 호환을 위해 남긴다.
    """
    from strands.tools.mcp import MCPClient

    factory = sigv4_httpx_client_factory(gateway_endpoint)
    # The endpoint may or may not already carry the `/mcp` path: the documented
    # contract is MCP_GATEWAY_URL *without* it, but AgentCore's own gateway_url
    # ends in `/mcp` and older terraform outputs passed it through, so both shapes
    # reach this code. Appending unconditionally produced `/mcp/mcp` → 400 → zero
    # tools loaded. Join idempotently, like infra/builtin_tools_gateway/mcp_client.py.
    base = gateway_endpoint.rstrip("/")
    client_kwargs = {"url": base if base.endswith("/mcp") else f"{base}/mcp"}
    if factory:
        client_kwargs["httpx_client_factory"] = factory

    mcp_client = MCPClient(lambda: streamablehttp_client(**client_kwargs))
    mcp_client.__enter__()
    # tools/list is paginated: the gateway returns one page plus a `nextCursor`,
    # which Strands surfaces as `PaginatedList.pagination_token`. A single
    # `list_tools_sync()` yields only the first page and silently drops the rest —
    # since the list is sorted, targets late in the alphabet (e.g. `platform-status`)
    # fall onto later pages and the agent never sees them. Follow the cursor to
    # exhaustion so every gateway tool is loaded.
    tools = []
    token = None
    while True:
        page = mcp_client.list_tools_sync(pagination_token=token)
        tools.extend(page)
        token = page.pagination_token
        if not token:
            break
    logger.info(f"Loaded {len(tools)} tools from MCP server")
    return tools, mcp_client
