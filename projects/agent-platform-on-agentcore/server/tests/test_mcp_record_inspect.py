"""레지스트리 레코드로 MCP 서버를 검사하는 라우트.

이 라우트가 /api/mcp/tools 와 따로 있는 이유는 신뢰 기준점이다. 그쪽은 요청 본문의
server_config 로 stdio 명령을 실행하므로 관리자 전용이어야 한다. 이쪽은 record_id 만
받고 엔드포인트를 레지스트리에서 해석한다 — 이 파일이 지키는 불변식이다.
"""
import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.auth as auth  # noqa: E402
import routes.mcp as mcp_routes  # noqa: E402

ADMIN = auth.AuthUser(sub="u-admin", username="alice", groups=["admin"])


class StubTool:
    """MCP SDK Tool 의 모양. inputSchema 는 규격 철자(camelCase)다."""

    def __init__(self, name, description=None, inputSchema=None, meta=None):
        self.name = name
        self.description = description
        self.inputSchema = inputSchema
        self.meta = meta


class StubRelay:
    def __init__(self):
        self.calls = []
        self.tools = [
            StubTool(
                "get_status",
                description="상태를 준다",
                inputSchema={"type": "object", "properties": {"verbose": {"type": "boolean"}}},
                meta={"ui": {"resourceUri": "ui://x/view"}},
            ),
        ]
        self.fail = None

    def resolve_endpoint(self, record_id):
        if record_id == "no-endpoint":
            raise ValueError(f"MCP record '{record_id}' has no endpoint URL")
        return f"https://mcp.example/{record_id}/mcp"

    def list_tools(self, record_id):
        url = self.resolve_endpoint(record_id)
        if self.fail:
            raise self.fail
        from services.mcp_apps_service import McpAppsRelay

        # 실제 변환 함수를 쓴다 — 스텁이 변환까지 흉내 내면 inputSchema 철자 버그를
        # 잡지 못하는 고무도장이 된다.
        return url, [McpAppsRelay._tool_info(t) for t in self.tools]

    def call_tool(self, record_id, tool_name, arguments):
        self.resolve_endpoint(record_id)
        if self.fail:
            raise self.fail
        self.calls.append((record_id, tool_name, arguments))
        return {"content": [{"type": "text", "text": "ok"}]}


@pytest.fixture
def client(monkeypatch):
    relay = StubRelay()
    monkeypatch.setattr(mcp_routes, "shared_relay", lambda: relay)

    app = FastAPI()
    app.include_router(mcp_routes.router)
    app.dependency_overrides[auth.require_admin] = lambda: ADMIN

    test_client = TestClient(app)
    test_client.relay = relay
    return test_client


def test_list_tools_returns_schema_and_ui_meta(client):
    """inputSchema(규격 철자)와 _meta.ui 가 둘 다 살아 나와야 한다.

    meta 가 유실되면 앱이 달린 툴을 인스펙터에서 구분할 수 없고, input_schema 가
    유실되면 인자 폼이 만들어지지 않는다.
    """
    resp = client.get("/api/mcp/records/r1/tools")

    assert resp.status_code == 200
    body = resp.json()
    assert body["record_id"] == "r1"
    assert body["endpoint"] == "https://mcp.example/r1/mcp"
    tool = body["tools"][0]
    assert tool["name"] == "get_status"
    assert tool["description"] == "상태를 준다"
    assert tool["input_schema"]["properties"] == {"verbose": {"type": "boolean"}}
    assert tool["meta"]["ui"]["resourceUri"] == "ui://x/view"


def test_snake_case_input_schema_is_accepted():
    """일부 서버는 input_schema 로 보낸다. 둘 다 읽어야 폼이 빈 채로 뜨지 않는다."""
    from services.mcp_apps_service import McpAppsRelay

    class SnakeTool:
        name = "t"
        description = None
        input_schema = {"type": "object", "properties": {"q": {"type": "string"}}}
        meta = None

    info = McpAppsRelay._tool_info(SnakeTool())
    assert info["input_schema"]["properties"] == {"q": {"type": "string"}}


def test_underscore_meta_is_read():
    """MCP SDK 버전에 따라 _meta 로만 노출된다 — 그것도 봐야 앱이 유실되지 않는다."""
    from services.mcp_apps_service import McpAppsRelay

    class MetaTool:
        name = "t"
        description = None
        inputSchema = None
        meta = None
        _meta = {"ui": {"resourceUri": "ui://a/b"}}

    assert McpAppsRelay._tool_info(MetaTool())["meta"]["ui"]["resourceUri"] == "ui://a/b"


def test_record_without_endpoint_is_a_client_error(client):
    """엔드포인트가 없는 레코드는 400 이다. 502 로 내면 AWS 장애처럼 보인다."""
    resp = client.get("/api/mcp/records/no-endpoint/tools")

    assert resp.status_code == 400
    assert "no endpoint URL" in resp.json()["detail"]


def test_session_failure_is_unwrapped_to_the_real_cause(client):
    """anyio TaskGroup 이 감싼 원인을 502 본문에 남긴다.

    그러지 않으면 "unhandled errors in a TaskGroup" 만 남아 SigV4 누락과 서버 다운을
    구분할 수 없다.
    """
    client.relay.fail = BaseExceptionGroup(
        "unhandled errors in a TaskGroup", [RuntimeError("Connection reset by peer")]
    )

    resp = client.get("/api/mcp/records/r1/tools")

    assert resp.status_code == 502
    assert resp.json()["detail"] == "RuntimeError: Connection reset by peer"


def test_jwt_gateway_401_says_why(client):
    """SigV4로는 CUSTOM_JWT 게이트웨이를 검사할 수 없다는 것을 본문에 남긴다.

    그러지 않으면 "401 Unauthorized" 만 남아 배포가 깨진 것처럼 보인다 — 실측
    게이트웨이(bap-gateway)에서 실제로 이 응답을 받았다.
    """
    client.relay.fail = BaseExceptionGroup(
        "g", [RuntimeError("Client error '401 Unauthorized' for url ...")]
    )

    detail = client.get("/api/mcp/records/r1/tools").json()["detail"]

    assert "401" in detail
    assert "CUSTOM_JWT" in detail


def test_ordinary_failures_are_not_annotated(client):
    """401/403이 아니면 그대로 둔다 — 관계없는 오류에 인증 얘기를 붙이면 오히려 헷갈린다."""
    client.relay.fail = RuntimeError("connection refused")

    assert (
        client.get("/api/mcp/records/r1/tools").json()["detail"]
        == "RuntimeError: connection refused"
    )


def test_gateway_arn_composes_an_endpoint():
    """URL 없이 gatewayArn 만 있는 레코드도 검사할 수 있어야 한다.

    자동 등록은 remotes[].url 을 넣지만 손으로 등록한 게이트웨이 레코드에는 ARN 만
    있다. UI 가 이미 "ARN으로 조합됩니다" 라고 말하던 그 조합이다.
    """
    from services.mcp_apps_service import gateway_mcp_url

    assert (
        gateway_mcp_url(
            "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/bap-gateway-gwexample02"
        )
        == "https://bap-gateway-gwexample02.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
    )
    # 게이트웨이가 아닌 ARN 은 조합하지 않는다.
    assert gateway_mcp_url("arn:aws:bedrock-agentcore:us-east-1:1:runtime/x") is None
    assert gateway_mcp_url("not-an-arn") is None


def test_endpointless_gateway_record_resolves_through_its_arn():
    """resolve_endpoint 가 URL → ARN 순으로 떨어진다."""
    from models.registry import RegistryRecordDetail
    from services.mcp_apps_service import McpAppsRelay

    arn = "arn:aws:bedrock-agentcore:us-east-1:1:gateway/gw-abc123"

    class OneRecord:
        def __init__(self, content):
            self._content = content

        def get_record(self, record_id):
            return RegistryRecordDetail(
                record_id=record_id, name=record_id, descriptor_content=self._content
            )

    # URL 이 있으면 그것이 이긴다 (harness 와 같은 규칙을 유지한다).
    both = McpAppsRelay(
        registry=OneRecord(
            {
                "server": {
                    "gatewayArn": arn,
                    "remotes": [{"url": "https://explicit.example/mcp"}],
                }
            }
        )
    )
    assert both.resolve_endpoint("r1") == "https://explicit.example/mcp"

    only_arn = McpAppsRelay(registry=OneRecord({"server": {"gatewayArn": arn}}))
    assert only_arn.resolve_endpoint("r1") == (
        "https://gw-abc123.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
    )

    # 둘 다 없으면 여전히 400 이 되는 ValueError 다.
    neither = McpAppsRelay(registry=OneRecord({"server": {"name": "x"}}))
    with pytest.raises(ValueError):
        neither.resolve_endpoint("r1")


def test_gateway_url_missing_mcp_path_is_normalized():
    """자동 등록은 AWS 가 보고한 gatewayUrl 을 그대로 저장하는데, 어떤 게이트웨이는
    그 값에 `/mcp` 경로가 빠져 있다 (실측: bap-gateway). bare 호스트는 JSON-RPC 가
    아닌 AWS 에러 envelope 를 돌려주고, MCP SDK 는 그것을 파싱하지 못하면 예외가 아니라
    hang 하므로 — 앱 탐색이 라이브 SSE 턴 안에서 돌기에 — 턴 전체가 멈춘다. 게이트웨이
    엔드포인트 형식은 ARN 에서 결정적(`gateway_mcp_url`)이므로 canonical `/mcp` 경로로
    정규화한다. 'URL 이 이긴다' 규칙은 유지하되 경로만 고친다.
    """
    from models.registry import RegistryRecordDetail
    from services.mcp_apps_service import McpAppsRelay

    class OneRecord:
        def __init__(self, content):
            self._content = content

        def get_record(self, record_id):
            return RegistryRecordDetail(
                record_id=record_id, name=record_id, descriptor_content=self._content
            )

    host = "https://gw-abc123.gateway.bedrock-agentcore.us-east-1.amazonaws.com"
    bare = McpAppsRelay(
        registry=OneRecord(
            {
                "server": {
                    "gatewayArn": "arn:aws:bedrock-agentcore:us-east-1:1:gateway/gw-abc123",
                    "remotes": [{"url": host}],
                }
            }
        )
    )
    assert bare.resolve_endpoint("r1") == host + "/mcp"

    # 게이트웨이가 아닌 명시 URL 은 손대지 않는다 — /mcp 를 강제하지 않는다.
    other = McpAppsRelay(
        registry=OneRecord({"server": {"remotes": [{"url": "https://mcp.example.com/sse"}]}})
    )
    assert other.resolve_endpoint("r1") == "https://mcp.example.com/sse"


def test_call_passes_only_record_id_and_arguments(client):
    resp = client.post(
        "/api/mcp/records/tools/call",
        json={"record_id": "r1", "tool_name": "get_status", "arguments": {"verbose": True}},
    )

    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert client.relay.calls == [("r1", "get_status", {"verbose": True})]


def test_call_request_has_no_server_config_field():
    """server_config 를 받으면 임의 명령 실행이 다시 열린다. 모델 수준에서 막는다."""
    from models.mcp import MCPRecordToolCallRequest

    assert "server_config" not in MCPRecordToolCallRequest.model_fields

    # extra 필드는 pydantic 기본값(ignore)으로 조용히 버려진다 — 무시되는 것을 고정한다.
    req = MCPRecordToolCallRequest.model_validate(
        {
            "record_id": "r1",
            "tool_name": "t",
            "server_config": {"command": "rm", "args": ["-rf", "/"]},
        }
    )
    assert not hasattr(req, "server_config")


def test_call_failure_is_a_result_not_a_page_error(client):
    """실패한 툴 호출은 200 + success=False 다 (/tools/call 과 같은 규약).

    502 로 내면 UI 가 결과 카드 대신 페이지 오류를 띄운다.
    """
    client.relay.fail = RuntimeError("tool blew up")

    resp = client.post(
        "/api/mcp/records/tools/call",
        json={"record_id": "r1", "tool_name": "get_status"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is False
    assert "tool blew up" in body["error"]


def test_routes_require_admin():
    """의존성 오버라이드 없이는 통과해선 안 된다."""
    app = FastAPI()
    app.include_router(mcp_routes.router)

    with TestClient(app) as bare:
        assert bare.get("/api/mcp/records/r1/tools").status_code in (401, 403)
        assert (
            bare.post(
                "/api/mcp/records/tools/call",
                json={"record_id": "r1", "tool_name": "t"},
            ).status_code
            in (401, 403)
        )
