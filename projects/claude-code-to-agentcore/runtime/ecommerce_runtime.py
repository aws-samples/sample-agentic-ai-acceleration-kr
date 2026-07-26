"""
ecommerce_runtime.py — AnyCompany 이커머스 분석 에이전트, AgentCore Runtime 버전.

Part 1의 로컬 `local_agent/agent.py` 와 **에이전트 로직은 동일**합니다. 바뀐 건
"도구를 어디서 가져오는가" 뿐입니다.

    로컬 (Claude Code)                     →  AgentCore Runtime (프로덕션)
    ──────────────────────────────────────────────────────────────────────
    stdio MCP: ecommerce_mcp.py            →  원격 MCP: AgentCore Gateway (HTTPS+JWT)
    stdio MCP: mcp-proxy → web search      →  원격 MCP: AgentCore Web Search Gateway
    .claude/skills/sales-report            →  컨테이너에 동봉된 동일 SKILL.md
    python agent.py "..."                  →  BedrockAgentCoreApp + @app.entrypoint

즉 Claude Code에서 쓰던 Skill/MCP가 그대로, 단지 "관리형 원격 도구"로 승격됩니다.

배포:
    python ecommerce_runtime.py deploy        # configure + deploy (컨테이너, CodeBuild)
    python ecommerce_runtime.py invoke "..."  # 배포된 런타임 호출
    python ecommerce_runtime.py               # 로컬 :8080 (컨테이너 진입점)

관측성 (AgentCore Observability 기준):
    트레이스·스팬·로그는 ADOT 자동 계측이 CloudWatch GenAI Observability로 전송(기본 제공).
    토큰·비용 등 LLM 수치는 observability.py가 호출마다 genai.* 메트릭으로 보완 기록.
    python ../observability/setup_anomaly_alarms.py  # 이상탐지 알람 구성 (선택)
    전체 가이드: ../observability/guide.md
"""
import json
import os
import shutil
import subprocess
import sys
import time

from bedrock_agentcore import BedrockAgentCoreApp
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

import observability

REGION = "us-east-1"
MODEL_ID = "us.anthropic.claude-sonnet-4-6"
AGENT_NAME = "anycompany_ecommerce"

os.environ["CLAUDE_CODE_USE_BEDROCK"] = "1"
os.environ.setdefault("AWS_REGION", REGION)

# Gateway 엔드포인트 + Web Search 엔드포인트는 환경변수로 주입 (deploy 시 .env 로 전달).
ECOMMERCE_GW_URL = os.environ.get("ECOMMERCE_GW_URL", "")
WEB_SEARCH_GW_URL = os.environ.get(
    "WEB_SEARCH_GW_URL",
    # 본인의 AgentCore Web Search 게이트웨이 URL로 교체하거나 WEB_SEARCH_GW_URL 환경변수로 주입하세요.
    "https://<YOUR-WEB-SEARCH-GATEWAY-ID>.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp")
# 이커머스 Gateway용 Cognito client_info (JWT 발급용). deploy 가 JSON 문자열로 주입.
# 참고: 실습 편의를 위해 환경변수로 전달합니다. 프로덕션에서는 client_secret을
# AWS Secrets Manager에 저장하고 런타임에서 조회하는 방식을 권장합니다.
ECOMMERCE_GW_CLIENT = os.environ.get("ECOMMERCE_GW_CLIENT", "")

SKILL = """\
이커머스 매출 리포트는 반드시 아래 4개 섹션으로 작성하세요:
1) 핵심 지표(총매출/총주문/AOV)  2) 카테고리별 실적(매출 내림차순+비중%)
3) 베스트셀러 TOP5  4) 인사이트&액션(관찰 2~3개 + 실행 권고).
숫자는 천 단위 콤마, 통화는 '원'. 추정 금지 — 반드시 도구로 조회한 실제 수치만 사용.
"""


def _gateway_token() -> str:
    """이커머스 Gateway 호출용 JWT를 Cognito에서 발급."""
    if not ECOMMERCE_GW_CLIENT:
        return ""
    from bedrock_agentcore_starter_toolkit.operations.gateway import GatewayClient
    gw = GatewayClient(region_name=REGION)
    return gw.get_access_token_for_cognito(json.loads(ECOMMERCE_GW_CLIENT))


def build_options() -> ClaudeAgentOptions:
    mcp_servers = {}
    allowed = ["Skill"]

    # 1) 이커머스 도구 — 로컬 stdio MCP 대신 AgentCore Gateway(원격 MCP).
    if ECOMMERCE_GW_URL and ECOMMERCE_GW_CLIENT:
        token = _gateway_token()
        mcp_servers["ecommerce"] = {
            "type": "http", "url": ECOMMERCE_GW_URL,
            "headers": {"Authorization": f"Bearer {token}"}}
        allowed += ["mcp__ecommerce__ecommerce___query_sales",
                    "mcp__ecommerce__ecommerce___top_products",
                    "mcp__ecommerce__ecommerce___run_sql"]

    # 2) 웹 검색 — AgentCore Web Search Gateway(SigV4/IAM). mcp-proxy로 서명.
    mcp_servers["web-search"] = {
        "type": "stdio", "command": "uvx",
        "args": ["mcp-proxy-for-aws@1.6.1", WEB_SEARCH_GW_URL,
                 "--service", "bedrock-agentcore", "--region", REGION]}
    allowed += ["mcp__web-search__WebSearch___WebSearch"]

    return ClaudeAgentOptions(
        model=MODEL_ID,
        system_prompt=("당신은 AnyCompany의 데이터 분석가입니다. 사내 이커머스 데이터는 "
                       "ecommerce 도구로 조회하고, 외부 시장 트렌드는 web-search 도구를 "
                       "사용하세요.\n\n[매출 리포트 작성 스킬]\n" + SKILL),
        mcp_servers=mcp_servers,
        allowed_tools=allowed,
        # allowed_tools 로 read-only 도구만 허용하므로 확인 프롬프트 없이 실행해도 안전.
        permission_mode="bypassPermissions",
        setting_sources=[],
        load_timeout_ms=120000,
        cli_path=shutil.which("claude"),
    )


app = BedrockAgentCoreApp()


@app.entrypoint
async def invoke(payload: dict):
    # 관측성: 스트리밍하는 동안 응답 텍스트/도구 호출을 모으고, 스트림 마지막의
    # ResultMessage(토큰·비용·레이턴시)를 받아 CloudWatch genai.* 메트릭으로 기록합니다.
    # 트레이스·스팬은 ADOT 자동 계측이 GenAI Observability로 전송 (observability.py 참고).
    prompt = payload["prompt"]
    t0 = time.monotonic()
    chunks: list[str] = []
    tools_used: list[str] = []
    result: ResultMessage | None = None
    error: str | None = None
    try:
        async with ClaudeSDKClient(options=build_options()) as client:
            await client.query(prompt)
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            chunks.append(block.text)
                            yield block.text
                        elif isinstance(block, ToolUseBlock):
                            tools_used.append(block.name)
                elif isinstance(msg, ResultMessage):
                    result = msg
                    if msg.is_error and not error:
                        error = f"result.subtype={msg.subtype}"
    except Exception as e:
        error = repr(e)
        raise
    finally:
        observability.observe_invocation(
            prompt=prompt,
            response="".join(chunks),
            model=MODEL_ID,
            latency_ms=(result.duration_ms if result
                        else int((time.monotonic() - t0) * 1000)),
            usage=(result.usage if result else None) or {},
            cost_usd=(result.total_cost_usd if result else None) or 0.0,
            session_id=result.session_id if result else None,
            num_turns=result.num_turns if result else None,
            tools_used=tools_used,
            error=error,
        )


# ───────────────────────── Configure + Deploy ───────────────────────────────
REQUIREMENTS = """\
bedrock-agentcore>=1.1.4
claude-agent-sdk>=0.1.19
bedrock-agentcore-starter-toolkit>=0.1.0
"""

DOCKERFILE = """\
FROM node:20-bookworm-slim AS node
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim
WORKDIR /app
ENV UV_SYSTEM_PYTHON=1 UV_COMPILE_BYTECODE=1 PYTHONUNBUFFERED=1 DOCKER_CONTAINER=1 \\
    AWS_REGION=us-east-1 AWS_DEFAULT_REGION=us-east-1 \\
    HOME=/tmp CLAUDE_CONFIG_DIR=/tmp/.claude XDG_CONFIG_HOME=/tmp/.config XDG_CACHE_HOME=/tmp/.cache \\
    CLAUDE_CODE_USE_BEDROCK=1 ANTHROPIC_MODEL=us.anthropic.claude-sonnet-4-6 \\
    CLAUDE_CODE_STREAM_CLOSE_TIMEOUT=180000 DISABLE_AUTOUPDATER=1 DISABLE_TELEMETRY=1 \\
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
# Node.js는 공식 이미지에서 바이너리만 복사 (curl | bash 설치 스크립트 사용 안 함)
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \\
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \\
    && npm install -g @anthropic-ai/claude-code@2.1.159 \\
    && claude --version
COPY requirements.txt requirements.txt
RUN uv pip install -r requirements.txt
RUN uv pip install aws-opentelemetry-distro==0.12.2
RUN useradd -m -u 1000 bedrock_agentcore
USER bedrock_agentcore
EXPOSE 8080
COPY . .
CMD ["opentelemetry-instrument", "python", "-m", "ecommerce_runtime"]
"""


def _agentcore():
    cand = os.path.join(os.path.dirname(sys.executable), "agentcore")
    return cand if os.path.exists(cand) else "agentcore"


def deploy():
    here = os.path.dirname(os.path.abspath(__file__))
    gw_info = json.load(open(os.path.join(here, "..", "gateway", "gateway.json")))
    build = os.path.join(here, ".build", AGENT_NAME)
    os.makedirs(build, exist_ok=True)
    entry = os.path.basename(__file__)
    shutil.copy(os.path.abspath(__file__), os.path.join(build, entry))
    shutil.copy(os.path.join(here, "observability.py"), os.path.join(build, "observability.py"))
    with open(os.path.join(build, "requirements.txt"), "w") as f:
        f.write(REQUIREMENTS)
    for stale in (".bedrock_agentcore.yaml",):
        try:
            os.remove(os.path.join(build, stale))
        except FileNotFoundError:
            pass

    env = {**os.environ, "AGENTCORE_SUPPRESS_RECOMMENDATION": "1", "AWS_REGION": REGION}
    ac = _agentcore()
    print(">> configure (container)")
    subprocess.run([ac, "configure", "-e", entry, "-n", AGENT_NAME, "-rf", "requirements.txt",
                    "-dt", "container", "--disable-memory", "-ni"], cwd=build, env=env, check=True)
    dockerfile = os.path.join(build, ".bedrock_agentcore", AGENT_NAME, "Dockerfile")
    print(f">> overwrite Dockerfile -> {dockerfile}")
    with open(dockerfile, "w") as f:
        f.write(DOCKERFILE)
    # Gateway 좌표를 런타임 환경변수로 주입.
    envs = [
        "--env", f"ECOMMERCE_GW_URL={gw_info['gateway_url']}",
        "--env", f"ECOMMERCE_GW_CLIENT={json.dumps(gw_info['client_info'])}",
        "--env", f"WEB_SEARCH_GW_URL={WEB_SEARCH_GW_URL}",
    ]
    print(">> deploy (CodeBuild ARM64)")
    subprocess.run([ac, "deploy", "--auto-update-on-conflict", *envs],
                   cwd=build, env=env, check=True)
    print(f"\ndeployed runtime '{AGENT_NAME}'")


def invoke_deployed(prompt: str):
    import uuid
    import boto3
    from botocore.config import Config

    ctl = boto3.client("bedrock-agentcore-control", region_name=REGION)
    arn = next(r["agentRuntimeArn"] for r in ctl.list_agent_runtimes()["agentRuntimes"]
               if r["agentRuntimeName"] == AGENT_NAME)
    rt = boto3.client("bedrock-agentcore", region_name=REGION,
                      config=Config(read_timeout=300, retries={"max_attempts": 0}))
    resp = rt.invoke_agent_runtime(agentRuntimeArn=arn, runtimeSessionId=uuid.uuid4().hex * 2,
                                   payload=json.dumps({"prompt": prompt}).encode(),
                                   contentType="application/json", accept="text/event-stream")
    print("HTTP", resp["ResponseMetadata"]["HTTPStatusCode"])
    print(resp["response"].read().decode())


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "deploy":
        deploy()
    elif len(sys.argv) > 1 and sys.argv[1] == "invoke":
        invoke_deployed(" ".join(sys.argv[2:]) or "2026 상반기 매출 리포트를 작성해줘.")
    else:
        app.run()
