"""게이트웨이(AWS_IAM) MCP 툴 로딩. 실행 역할 SigV4로 서명한다."""
import logging
import os
import time
from mcp.client.streamable_http import streamablehttp_client
from auth.sigv4 import sigv4_httpx_client_factory

logger = logging.getLogger(__name__)


SESSION_SCOPE_HEADER = "X-Agent-Session-Scope"


def load_tools_from_mcp(gateway_endpoint, session_scope=None):
    """SigV4로 서명해 MCP 서버에서 툴을 로드한다.

    ``session_scope``(서버가 thread_id 에서 만든 runtimeSessionId)는 모든 MCP 요청에
    ``X-Agent-Session-Scope`` 헤더로 실린다. 내장 툴 게이트웨이의 인터셉터는 이 값을
    검증된 호출자 신원 *아래에* 붙여 샌드박스 라벨을 만들므로, 실행 역할 하나로 모든
    사용자를 부르는 runtime 이라도 대화마다 다른 Code Interpreter·Browser 세션에 닿는다.
    헤더가 없으면 역할 하나에 샌드박스 하나가 되어 다른 사용자의 파일이 보였다
    (도쿄 스택 실측, 2026-10-21). 인터셉터가 없는 게이트웨이는 헤더를 무시한다.
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
    if session_scope:
        client_kwargs["headers"] = {SESSION_SCOPE_HEADER: str(session_scope)}

    mcp_client = MCPClient(lambda: streamablehttp_client(**client_kwargs))
    mcp_client.__enter__()
    # tools/list is paginated: the gateway returns one page plus a `nextCursor`,
    # which Strands surfaces as `PaginatedList.pagination_token`. A single
    # `list_tools_sync()` yields only the first page and silently drops the rest —
    # since the list is sorted, targets late in the alphabet (e.g. `platform-status`)
    # fall onto later pages and the agent never sees them. Follow the cursor to
    # exhaustion so every gateway tool is loaded.
    #
    # Each page is retried on its own, and a page that keeps failing ends the list
    # with what came before it instead of losing every tool. The gateway asks each
    # target for its page; the `registry` target's endpoint answers 429 under load
    # ("returned HTTP 429 to the initialize handshake"), and that one error used to
    # fail the whole load — 442 of 650 sessions ran with no gateway tools at 300
    # concurrent new chats (bst load test, 2026-10-18). Targets are listed by name,
    # so a late failing target costs only its own tools and those after it.
    attempts = max(1, int(os.getenv("MCP_TOOLS_PAGE_ATTEMPTS", "3")))
    tools = []
    token = None
    while True:
        page = None
        for attempt in range(1, attempts + 1):
            try:
                page = mcp_client.list_tools_sync(pagination_token=token)
                break
            except Exception as e:
                if attempt == attempts:
                    if not tools:
                        raise
                    logger.warning(
                        f"tools/list page {'after ' + str(len(tools)) + ' tools'} failed {attempts} times; "
                        f"continuing with the {len(tools)} tools loaded so far: {e}"
                    )
                else:
                    logger.warning(f"tools/list page failed (attempt {attempt}/{attempts}), retrying: {e}")
                    time.sleep(0.5 * attempt)
        if page is None:
            break
        tools.extend(page)
        token = page.pagination_token
        if not token:
            break
    logger.info(f"Loaded {len(tools)} tools from MCP server")
    return tools, mcp_client
