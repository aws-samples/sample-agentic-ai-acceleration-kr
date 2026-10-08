"""게이트웨이(AWS_IAM)를 SigV4로 호출하기 위한 httpx 클라이언트 팩토리.

harness(server/mcp_core/app_session.py)와 같은 패턴이다. runtime은 실행 역할의
자격증명으로 게이트웨이 MCP 엔드포인트에 서명한다 — bearer 토큰을 대체한다.
"""
from __future__ import annotations
from typing import Callable, Optional

AGENTCORE_SERVICE = "bedrock-agentcore"
AGENTCORE_HOST_SUFFIX = ".amazonaws.com"


def _is_gateway_url(url: str) -> bool:
    return f".gateway.{AGENTCORE_SERVICE}." in url and AGENTCORE_HOST_SUFFIX in url


def _signing_region(url: str) -> str:
    return url.split(f"{AGENTCORE_SERVICE}.", 1)[1].split(".", 1)[0]


def sigv4_httpx_client_factory(url: str) -> Optional[Callable]:
    if not _is_gateway_url(url):
        return None

    import boto3
    import httpx
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    region = _signing_region(url)
    credentials = boto3.Session().get_credentials()
    if credentials is None:
        raise RuntimeError(
            "AWS_IAM 게이트웨이는 SigV4가 필요하지만 AWS 자격증명을 찾을 수 없습니다."
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
