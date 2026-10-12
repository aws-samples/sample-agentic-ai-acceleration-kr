"""_meta.ui 해석 규칙.

이 파일이 있는 이유: MCPToolInfo가 _meta를 조용히 버려서 MCP Apps가 아예 동작하지
않았다. 유실은 예외를 던지지 않으므로 테스트로만 잡힌다.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp_core.ui_meta import (  # noqa: E402
    APP_MIME_TYPE,
    UI_EXTENSION_ID,
    is_app_callable,
    is_model_visible,
    resource_uri_of,
    ui_client_capabilities,
    visibility_of,
)


def test_extension_id_and_mime_are_the_exact_spec_strings():
    assert UI_EXTENSION_ID == "io.modelcontextprotocol/ui"
    assert APP_MIME_TYPE == "text/html;profile=mcp-app"


def test_client_capabilities_declare_mime_types():
    caps = ui_client_capabilities()
    assert caps["extensions"][UI_EXTENSION_ID]["mimeTypes"] == [APP_MIME_TYPE]


def test_resource_uri_read_from_nested_ui_key():
    meta = {"ui": {"resourceUri": "ui://weather/dashboard"}}
    assert resource_uri_of(meta) == "ui://weather/dashboard"


def test_deprecated_flat_key_still_read():
    # 규격이 deprecated로 표시했지만 GA 전까지는 서버가 보낼 수 있다.
    meta = {"ui/resourceUri": "ui://legacy/view"}
    assert resource_uri_of(meta) == "ui://legacy/view"


def test_nested_key_wins_over_deprecated_flat_key():
    meta = {"ui": {"resourceUri": "ui://new"}, "ui/resourceUri": "ui://old"}
    assert resource_uri_of(meta) == "ui://new"


def test_no_meta_means_no_resource():
    assert resource_uri_of(None) is None
    assert resource_uri_of({}) is None


def test_non_ui_scheme_is_rejected():
    # ui:// 가 아니면 앱이 아니다. 임의 URL을 호스트가 가져오게 하면 안 된다.
    assert resource_uri_of({"ui": {"resourceUri": "https://evil.example/x"}}) is None


def test_visibility_defaults_to_both():
    # 규격 기본값. 기존 툴이 그대로 동작해야 한다.
    assert visibility_of(None) == ["model", "app"]
    assert visibility_of({}) == ["model", "app"]
    assert visibility_of({"ui": {}}) == ["model", "app"]


def test_app_only_tool_hidden_from_model():
    meta = {"ui": {"visibility": ["app"]}}
    assert is_app_callable(meta) is True
    assert is_model_visible(meta) is False


def test_model_only_tool_not_callable_by_app():
    meta = {"ui": {"visibility": ["model"]}}
    assert is_app_callable(meta) is False
    assert is_model_visible(meta) is True


def test_malformed_visibility_falls_back_to_default():
    # 잘못된 타입이 왔을 때 조용히 전부 차단되면 기존 툴이 사라진다.
    assert visibility_of({"ui": {"visibility": "app"}}) == ["model", "app"]
    assert visibility_of({"ui": {"visibility": []}}) == ["model", "app"]
