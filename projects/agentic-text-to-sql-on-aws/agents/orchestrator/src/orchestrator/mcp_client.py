"""AgentCore Runtime / Gateway MCP 서버 접속.

두 가지 도구 평면(tool plane) 모드를 지원한다(config.Settings.tool_plane_mode):

- **direct** (M1 기본): Gateway 없이 Runtime MCP 서버에 직접 연결(streamable-HTTP).
  엔드포인트: https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{ARN}/invocations?qualifier=DEFAULT
  (ARN URL 인코딩). SigV4 서명은 공식 `mcp-proxy-for-aws` 의 `aws_iam_streamablehttp_client`
  로 처리한다(서명 서비스명: `bedrock-agentcore`). sql/semantic 각각 클라이언트 1개.
- **gateway** (M3): 단일 Gateway MCP 엔드포인트가 모든 도구를 집약한다. Cognito M2M
  (USER_PASSWORD_AUTH)으로 AccessToken 을 받아 `Authorization: Bearer` 헤더로 전달한다.
  클라이언트 1개에서 도구를 받아 이름 suffix 로 sql/semantic 을 분류한다.

  ⚠️ gateway 모드 인증은 orchestrator 의 **서비스 계정 위임**(M3 범위)이다. 사용자별 JWT
  전파(On-Behalf-Of)는 M4+ 로 미룬다.

순수 로직(URL 조립·도구 분류·토큰 캐시)은 단위 테스트로 커버하고, 실제 SDK/AWS 호출은
통합 시점에만 수행한다. SDK/boto3 의존성은 함수 내부에서 지연 임포트한다.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote

MCP_SERVICE_NAME = "bedrock-agentcore"

# gateway 모드 Cognito 토큰을 만료 몇 초 전에 미리 갱신할지(초).
TOKEN_REFRESH_SKEW_SECONDS = 300


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


def create_gateway_mcp_client(gateway_url: str, bearer_token: str):
    """Gateway MCP 엔드포인트에 대한 Strands MCPClient 를 생성(Bearer 인증).

    Cognito M2M AccessToken 을 `Authorization: Bearer <token>` 헤더로 전달하며,
    표준 MCP streamable-http 트랜스포트를 사용한다. SigV4 direct 경로와 달리
    프록시 서명이 필요 없다. SDK 의존성은 지연 임포트한다.
    """
    if not gateway_url:
        raise ValueError("gateway_url 이 비어 있습니다.")
    from mcp.client.streamable_http import streamablehttp_client
    from strands.tools.mcp import MCPClient

    headers = {"Authorization": f"Bearer {bearer_token}"}

    def _transport():
        return streamablehttp_client(gateway_url, headers=headers)

    return MCPClient(_transport)


class CognitoTokenCache:
    """Cognito AccessToken 을 만료 전까지 재사용하는 단순 캐시.

    만료 `skew` 초 전에 미리 갱신한다. 시계(`clock`)를 주입할 수 있어 단위 테스트에서
    AWS 호출 없이 만료·갱신 경계를 검증한다.
    """

    def __init__(self, clock: Any = time.time) -> None:
        self._clock = clock
        self._token: str | None = None
        self._expires_at: float = 0.0

    def get_or_fetch(
        self,
        fetch: Any,
        skew: float = TOKEN_REFRESH_SKEW_SECONDS,
    ) -> str:
        """캐시된 토큰이 유효하면 반환, 아니면 `fetch()` 로 갱신한다.

        fetch 는 `(access_token, expires_in_seconds)` 튜플을 반환해야 한다.
        """
        now = self._clock()
        if self._token is not None and now < self._expires_at - skew:
            return self._token
        token, expires_in = fetch()
        self._token = token
        self._expires_at = now + float(expires_in)
        return token


# 모듈 레벨 토큰 캐시(같은 microVM 내 재사용). 테스트는 자체 인스턴스를 쓴다.
_TOKEN_CACHE = CognitoTokenCache()


def fetch_cognito_token(settings: Any, cache: CognitoTokenCache | None = None) -> str:
    """Cognito M2M(USER_PASSWORD_AUTH)로 AccessToken 을 획득(캐시 경유).

    비밀번호는 Secrets Manager(`cognito_password_secret_arn`)에서 읽어 LLM/로그에
    노출하지 않는다. boto3 의존성은 지연 임포트한다. 만료 전이면 캐시된 토큰을 재사용한다.
    """
    cache = cache or _TOKEN_CACHE

    def _fetch() -> tuple[str, float]:
        import boto3

        secrets = boto3.client("secretsmanager", region_name=settings.region)
        password = secrets.get_secret_value(SecretId=settings.cognito_password_secret_arn)[
            "SecretString"
        ]

        idp = boto3.client("cognito-idp", region_name=settings.region)
        resp = idp.initiate_auth(
            ClientId=settings.cognito_client_id,
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={
                "USERNAME": settings.cognito_user,
                "PASSWORD": password,
            },
        )
        result = resp["AuthenticationResult"]
        return result["AccessToken"], float(result.get("ExpiresIn", 3600))

    return cache.get_or_fetch(_fetch)


def tool_display_name(tool: Any) -> str:
    """MCP 도구 객체에서 도구명을 방어적으로 추출한다.

    Gateway target 프리픽스가 붙어 `<TargetName>___run_sql` 형태일 수 있다.
    """
    name = getattr(tool, "tool_name", None)
    if name:
        return str(name)
    spec = getattr(tool, "tool_spec", None)
    if isinstance(spec, dict) and spec.get("name"):
        return str(spec["name"])
    return ""


def _filter_by_suffix(tools: list[Any], suffix: str) -> list[Any]:
    """도구명이 `suffix` 로 끝나는 도구만 반환(Gateway 프리픽스 무시)."""
    return [t for t in tools if tool_display_name(t).endswith(suffix)]


class ToolClients:
    """도구 평면 모드를 추상화한 MCP 클라이언트 묶음.

    - direct: sql/semantic 각각 클라이언트 1개. 각 클라이언트 도구 전체를 그대로 반환.
    - gateway: 단일 클라이언트가 모든 도구를 집약. `run_sql`/`search_schema` suffix 로 분류.

    app.py 의 RunnerSession 이 소비할 수 있도록 `clients`(중복 없는 실제 인스턴스 리스트)를
    제공한다. gateway 모드에서 sql/semantic 은 동일 인스턴스이므로 `clients` 는 1개다.
    """

    def __init__(self, sql_client: Any, semantic_client: Any, gateway: bool) -> None:
        self._sql_client = sql_client
        self._semantic_client = semantic_client
        self._gateway = gateway

    @property
    def gateway(self) -> bool:
        return self._gateway

    @property
    def clients(self) -> list[Any]:
        """정리/시작 대상 실제 클라이언트 인스턴스(중복 제거)."""
        if self._gateway or self._sql_client is self._semantic_client:
            return [self._sql_client]
        return [self._sql_client, self._semantic_client]

    def start(self) -> None:
        for client in self.clients:
            client.start()

    def close(self) -> None:
        for client in self.clients:
            stop = getattr(client, "stop", None)
            if stop is None:
                continue
            try:
                stop(None, None, None)
            except TypeError:
                stop()

    def sql_tools(self) -> list[Any]:
        tools = self._sql_client.list_tools_sync()
        return _filter_by_suffix(tools, "run_sql") if self._gateway else tools

    def semantic_tools(self) -> list[Any]:
        tools = self._semantic_client.list_tools_sync()
        return _filter_by_suffix(tools, "search_schema") if self._gateway else tools


def create_tool_clients(settings: Any) -> ToolClients:
    """설정에 따라 direct 2-클라이언트 또는 gateway 1-클라이언트 묶음을 생성한다.

    require_mcp_arns() 로 필수 env 가 검증된 뒤 호출되는 것을 전제로 한다.
    """
    if settings.is_gateway_mode():
        token = fetch_cognito_token(settings)
        client = create_gateway_mcp_client(settings.gateway_url, token)
        return ToolClients(sql_client=client, semantic_client=client, gateway=True)

    sql_client = create_mcp_client(settings.sql_mcp_arn, settings.region)
    semantic_client = create_mcp_client(settings.semantic_mcp_arn, settings.region)
    return ToolClients(sql_client=sql_client, semantic_client=semantic_client, gateway=False)
