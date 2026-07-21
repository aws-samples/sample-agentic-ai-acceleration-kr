"""
verify_gateway.py — 배포한 ecommerce Gateway를 MCP로 직접 호출해 검증.

Cognito에서 JWT 액세스 토큰을 받아 Gateway(MCP/HTTPS)에 붙고,
tools/list 로 도구 3개가 보이는지 + query_sales 를 실제 호출해
Lambda→SQLite 경로가 동작하는지 확인합니다.

    python verify_gateway.py
"""
import asyncio
import json
import os

from bedrock_agentcore_starter_toolkit.operations.gateway import GatewayClient
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

HERE = os.path.dirname(os.path.abspath(__file__))


async def main():
    info = json.load(open(os.path.join(HERE, "gateway.json")))
    gw = GatewayClient(region_name="us-east-1")
    token = gw.get_access_token_for_cognito(info["client_info"])
    headers = {"Authorization": f"Bearer {token}"}

    async with streamablehttp_client(info["gateway_url"], headers=headers) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            print("도구 목록:", [t.name for t in tools.tools])

            print("\n[query_sales] 2026 상반기 전체 매출")
            res = await s.call_tool("ecommerce___query_sales",
                                    {"start_date": "2026-01-01", "end_date": "2026-06-30"})
            print(res.content[0].text)

            print("\n[top_products] 상위 3개")
            res = await s.call_tool("ecommerce___top_products", {"limit": 3})
            print(res.content[0].text)


if __name__ == "__main__":
    asyncio.run(main())
