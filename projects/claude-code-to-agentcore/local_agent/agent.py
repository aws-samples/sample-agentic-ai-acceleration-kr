"""
agent.py — AnyCompany 이커머스 분석 에이전트 (로컬 / Claude Code 환경 재현).

Claude Code에서 다음을 붙여 데이터 분석을 하던 환경을, Claude Agent SDK로
그대로 코드화한 것입니다.

  • Skill  : local_agent/.claude/skills/sales-report/SKILL.md  (매출 리포트 작성법)
  • MCP    : ecommerce      (사내 이커머스 DB 조회 — query_sales/top_products/run_sql)
  • MCP    : web-search     (AgentCore Web Search 게이트웨이 — 외부 시장 트렌드)

setting_sources=["project"] 로 두면 SDK가 ./.claude/settings.json 과 ./.mcp.json,
그리고 ./.claude/skills/* 를 Claude Code와 똑같이 읽어들입니다. 즉 이 한 파일은
"Claude Code에서 하던 그 세션"을 비대화형으로 실행하는 래퍼입니다.

실행:
    export CLAUDE_CODE_USE_BEDROCK=1 AWS_REGION=us-east-1
    python local_agent/agent.py "지난 6개월 매출 리포트를 작성해줘"
"""
import asyncio
import os
import sys

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    TextBlock,
)

os.environ.setdefault("CLAUDE_CODE_USE_BEDROCK", "1")
os.environ.setdefault("AWS_REGION", "us-east-1")
MODEL_ID = "us.anthropic.claude-sonnet-4-6"
PROJECT = os.path.dirname(os.path.abspath(__file__))


def build_options() -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        model=MODEL_ID,
        cwd=PROJECT,
        # Claude Code의 프로젝트 설정을 그대로 로드: .claude/settings.json, .mcp.json, .claude/skills/*
        setting_sources=["project"],
        system_prompt=(
            "당신은 AnyCompany의 데이터 분석가입니다. 사내 이커머스 데이터는 'ecommerce' "
            "MCP 도구로 조회하고, 외부 시장 트렌드가 필요하면 web-search 도구를 사용하세요. "
            "매출 리포트 요청에는 sales-report 스킬의 형식을 따르세요."
        ),
        allowed_tools=[
            "mcp__ecommerce__query_sales",
            "mcp__ecommerce__top_products",
            "mcp__ecommerce__run_sql",
            "mcp__agentcore-web-search__WebSearch___WebSearch",
            "Skill",
        ],
        # allowed_tools 로 read-only 도구만 허용하므로 확인 프롬프트 없이 실행해도 안전.
        permission_mode="bypassPermissions",
    )


async def run(prompt: str):
    async with ClaudeSDKClient(options=build_options()) as client:
        await client.query(prompt)
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        print(block.text, end="", flush=True)
    print()


if __name__ == "__main__":
    prompt = " ".join(sys.argv[1:]) or "지난 6개월 매출 리포트를 작성해줘."
    asyncio.run(run(prompt))
