"""AgentCore Runtime 에 호스팅된 MCP 서버 접속 (SigV4).

M1 은 Gateway 없이 Runtime MCP 서버에 직접 연결한다(streamable-HTTP `/mcp`).
엔드포인트 형식:
  https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{ARN}/invocations?qualifier=DEFAULT
  (ARN 은 URL 인코딩)

SigV4 서명은 공식 `mcp-proxy-for-aws` 의 `aws_iam_streamablehttp_client` 로 처리한다
(서명 서비스명: `bedrock-agentcore`). Strands `MCPClient` 와 결합한다.

URL 조립 함수는 순수 로직이라 단위 테스트로 커버하고, 실제 클라이언트 생성은 통합 시점에만 수행한다.
"""

from __future__ import annotations

from urllib.parse import quote

MCP_SERVICE_NAME = "bedrock-agentcore"


def build_runtime_mcp_url(runtime_arn: str, region: str, qualifier: str = "DEFAULT") -> str:
    """Runtime ARN 으로 MCP invocations 엔드포인트 URL 을 조립."""
    if not runtime_arn:
        raise ValueError("runtime_arn 이 비어 있습니다.")
    escaped = quote(runtime_arn, safe="")
    return (
        f"https://{MCP_SERVICE_NAME}.{region}.amazonaws.com"
        f"/runtimes/{escaped}/invocations?qualifier={qualifier}"
    )


def create_mcp_client(runtime_arn: str, region: str):
    """Runtime MCP 서버에 대한 Strands MCPClient 를 생성.

    실제 SDK 의존성(strands, mcp_proxy_for_aws)은 함수 내부에서 지연 임포트해
    순수 로직 테스트(단위)가 이 의존성 없이도 돌아가게 한다.
    """
    from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client
    from strands.tools.mcp import MCPClient

    url = build_runtime_mcp_url(runtime_arn, region)

    def _transport():
        return aws_iam_streamablehttp_client(
            endpoint=url,
            aws_region=region,
            aws_service=MCP_SERVICE_NAME,
        )

    return MCPClient(_transport)
