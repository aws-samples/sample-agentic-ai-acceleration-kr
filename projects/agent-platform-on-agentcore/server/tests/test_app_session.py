"""resources/read 결과 해석과 capability 선언.

네트워크 없이 검증한다: 세션 수립은 통합 테스트 영역이고, 여기서 잡아야 하는 버그는
응답 파싱과 capability 직렬화다.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp_core.app_session import (  # noqa: E402
    build_client_capabilities,
    parse_resource_result,
)
from mcp_core.ui_meta import APP_MIME_TYPE, UI_EXTENSION_ID  # noqa: E402


class StubContent:
    def __init__(self, uri, mimeType, text, meta=None):
        self.uri = uri
        self.mimeType = mimeType
        self.text = text
        self.blob = None
        self.meta = meta


class StubResult:
    def __init__(self, contents):
        self.contents = contents


def test_capabilities_serialize_extensions_by_alias():
    """설치된 SDK는 SEP-1724 이전이라 extensions 필드를 모른다.

    extra="allow" 덕분에 통과하는 우회이므로, 직렬화 결과를 고정해 둔다.
    """
    caps = build_client_capabilities()
    dumped = caps.model_dump(by_alias=True, exclude_none=True)
    assert dumped["extensions"][UI_EXTENSION_ID]["mimeTypes"] == [APP_MIME_TYPE]


def test_parse_extracts_text_and_ui_meta():
    ui_meta = {"csp": {"connectDomains": ["https://api.example.com"]}, "prefersBorder": True}
    result = StubResult([
        StubContent("ui://x/view", APP_MIME_TYPE, "<!DOCTYPE html><html></html>",
                    meta={"ui": ui_meta}),
    ])

    payload = parse_resource_result(result, "ui://x/view")

    assert payload.uri == "ui://x/view"
    assert payload.mime_type == APP_MIME_TYPE
    assert payload.text == "<!DOCTYPE html><html></html>"
    assert payload.ui_meta == ui_meta


def test_parse_rejects_wrong_mime_type():
    """ui:// 인데 mcp-app 프로필이 아니면 앱으로 취급하지 않는다."""
    result = StubResult([StubContent("ui://x/view", "text/html", "<html></html>")])
    with pytest.raises(ValueError, match="mimeType"):
        parse_resource_result(result, "ui://x/view")


def test_parse_rejects_uri_mismatch():
    """요청한 URI와 다른 리소스를 서버가 돌려주면 거부한다."""
    result = StubResult([StubContent("ui://other/view", APP_MIME_TYPE, "<html></html>")])
    with pytest.raises(ValueError, match="URI"):
        parse_resource_result(result, "ui://x/view")


def test_parse_rejects_empty_contents():
    with pytest.raises(ValueError, match="비어"):
        parse_resource_result(StubResult([]), "ui://x/view")


def test_parse_decodes_blob_when_text_absent():
    """규격은 text 또는 blob(base64) 둘 중 하나를 허용한다."""
    import base64

    content = StubContent("ui://x/view", APP_MIME_TYPE, None)
    content.blob = base64.b64encode(b"<html>blob</html>").decode()
    payload = parse_resource_result(StubResult([content]), "ui://x/view")
    assert payload.text == "<html>blob</html>"


def test_parse_missing_ui_meta_yields_empty_dict():
    """_meta.ui가 없으면 호스트가 제한적 CSP 기본값을 쓴다."""
    result = StubResult([StubContent("ui://x/view", APP_MIME_TYPE, "<html></html>")])
    assert parse_resource_result(result, "ui://x/view").ui_meta == {}


def test_parse_rejects_malformed_base64_blob():
    """잘못된 blob은 ValueError여야 한다 — 라우트가 400으로 잡는 유일한 타입이다."""
    content = StubContent("ui://x/view", APP_MIME_TYPE, None)
    content.blob = "!!!notbase64"
    with pytest.raises(ValueError):
        parse_resource_result(StubResult([content]), "ui://x/view")


def test_parse_rejects_non_utf8_blob():
    """UTF-8이 아닌 blob은 ValueError여야 한다."""
    import base64
    content = StubContent("ui://x/view", APP_MIME_TYPE, None)
    content.blob = base64.b64encode(b"\xff\xfe\x00binary").decode()
    with pytest.raises(ValueError):
        parse_resource_result(StubResult([content]), "ui://x/view")


def test_supported_versions_come_from_the_sdk():
    """버전 목록은 SDK가 소유한다.

    직접 나열하면 SDK가 버전을 추가할 때마다 조용히 어긋나고, 최신이 아닌 서버를
    전부 거부한다 — 실제로 2025-06-18을 쓰는 서버가 연결에 실패했다.
    """
    from mcp.client.session import SUPPORTED_PROTOCOL_VERSIONS as sdk_versions

    from mcp_core.app_session import SUPPORTED_PROTOCOL_VERSIONS as ours

    assert list(ours) == list(sdk_versions)


def test_agentcore_runtime_url_is_detected():
    """AgentCore Runtime의 MCP 엔드포인트는 SigV4가 필요하다.

    판정이 틀리면 서명 없이 붙어 403이 되거나(누락), 일반 MCP 서버에 불필요한
    AWS 서명을 붙인다(오검출).
    """
    from mcp_core.app_session import _is_agentcore_runtime_url

    arn = "arn%3Aaws%3Abedrock-agentcore%3Aus-east-1%3A1%3Aruntime%2Fx-y"
    assert _is_agentcore_runtime_url(
        f"https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/{arn}/invocations?qualifier=DEFAULT"
    )
    # 일반 MCP 서버는 서명하지 않는다.
    assert not _is_agentcore_runtime_url("http://127.0.0.1:3099/mcp")
    assert not _is_agentcore_runtime_url("https://mcp.example.com/mcp")
    # 게이트웨이는 runtimes 경로가 아니다.
    assert not _is_agentcore_runtime_url(
        "https://x.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
    )


def test_agentcore_gateway_url_is_detected():
    """AWS_IAM 게이트웨이도 SigV4 를 요구한다.

    런타임과 판정을 따로 두는 이유는 호스트 모양이 다르기 때문이다 — 런타임은 서비스가
    호스트 선두에 오지만 게이트웨이는 `{id}.gateway.` 뒤에 온다. 게이트웨이 레코드를
    인스펙터로 열면 서명 없이는 403 이 된다.
    """
    from mcp_core.app_session import _is_agentcore_gateway_url

    assert _is_agentcore_gateway_url(
        "https://builtin-tools-abc123.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
    )
    assert not _is_agentcore_gateway_url("https://mcp.example.com/mcp")
    # 런타임 엔드포인트는 게이트웨이 판정에 걸리지 않는다 (각자 자기 모양만 본다).
    assert not _is_agentcore_gateway_url(
        "https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/x/invocations"
    )


def test_signing_region_comes_from_both_host_shapes():
    """서명 리전을 잘못 뽑으면 SignatureDoesNotMatch 가 된다."""
    from mcp_core.app_session import _signing_region

    assert (
        _signing_region(
            "https://x.gateway.bedrock-agentcore.ap-northeast-2.amazonaws.com/mcp"
        )
        == "ap-northeast-2"
    )
    assert (
        _signing_region(
            "https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/x/invocations"
        )
        == "us-west-2"
    )


def test_non_agentcore_url_gets_no_signing_factory():
    """일반 서버에는 httpx 팩토리를 주지 않는다 (기본 경로 유지)."""
    from mcp_core.app_session import _sigv4_client_factory

    assert _sigv4_client_factory("http://127.0.0.1:3099/mcp") is None


def test_list_tools_times_out_instead_of_hanging():
    """엔드포인트가 응답을 안 끝내면 세션은 hang이 아니라 TimeoutError여야 한다.

    실제로 겪은 것: 게이트웨이 레코드의 저장된 URL에 `/mcp` 경로가 빠져 있으면 bare
    호스트가 JSON-RPC가 아닌 AWS 에러 envelope를 돌려주고, SDK 클라이언트는 유효한
    메시지가 오길 기다리며 영원히 블록한다. 이 세션 개설은 라이브 SSE 턴 안의 앱 탐색
    (`_walk_app_tools`)에서 일어나므로, 거기서 멈추면 턴 전체가 무한로딩이 된다 — 나쁜
    레코드를 스킵하려던 try/except는 hang이 예외가 아니라서 발동하지 못한다. 세션을
    시간으로 묶어 hang을 호출자가 스킵할 수 있는 TimeoutError로 바꾼다.
    """
    from contextlib import asynccontextmanager

    import anyio

    from mcp_core.app_session import list_tools

    @asynccontextmanager
    async def hanging_client(url, **kwargs):
        yield (None, None, None)

    class HangingSession:
        def __init__(self, read, write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def send_request(self, *args, **kwargs):
            # The initialize round-trip the real SDK does; here it never returns,
            # mirroring the SDK waiting on a body it could not parse.
            await anyio.sleep_forever()

        async def send_notification(self, *args, **kwargs):
            pass

        async def list_tools(self):
            await anyio.sleep_forever()

    with pytest.raises(TimeoutError):
        list_tools(
            "https://broken.gateway.example/",  # note: no /mcp path
            timeout=0.2,
            _client_factory=hanging_client,
            _session_cls=HangingSession,
        )
