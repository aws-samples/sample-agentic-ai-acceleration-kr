import pytest

GW = "https://abc.gateway.bedrock-agentcore.us-east-1.amazonaws.com"


def test_load_tools_passes_sigv4_factory_not_bearer(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAIOSFODNN7EXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")

    captured_kwargs = {}

    # streamablehttp_client을 mock하여 호출 인자만 캡처한다.
    # MCPClient의 초기화를 피하기 위해 MCPClient 자체를 mock한다.
    def mock_mcpclient_init(self, client_factory):
        captured_kwargs["client_factory"] = client_factory
        raise RuntimeError("stop after init")

    import auth.access_token as at
    from strands.tools.mcp import MCPClient

    monkeypatch.setattr(MCPClient, "__init__", mock_mcpclient_init)

    with pytest.raises(RuntimeError):
        at.load_tools_from_mcp(f"{GW}/mcp")

    # factory가 전달되고 bearer 헤더가 없는 인자로 호출되는지 검증
    assert "client_factory" in captured_kwargs
    factory = captured_kwargs["client_factory"]
    assert callable(factory)

    # factory의 반환값(streamablehttp_client 호출 인자)에 Authorization 헤더가 없는지 검증
    captured_http = {}

    def fake_streamablehttp_client(**kwargs):
        captured_http.update(kwargs)
        from unittest.mock import MagicMock
        return MagicMock()

    monkeypatch.setattr(at, "streamablehttp_client", fake_streamablehttp_client)
    factory()
    assert "headers" not in captured_http or "Authorization" not in (captured_http.get("headers") or {})


def test_load_tools_follows_pagination_cursor(monkeypatch):
    """게이트웨이의 tools/list는 페이지네이션된다 — 커서를 끝까지 따라가야 한다.

    첫 페이지만 읽으면 뒤 페이지의 툴이 조용히 사라진다(정렬 순서상 알파벳 뒤쪽
    타깃이 밀려난다). 실측으로 platform-status 툴 2개가 2페이지에 있어 기본
    에이전트가 못 봤다. 커서를 무시하는 회귀를 이 테스트가 잡는다.
    """
    from strands.types.collections import PaginatedList

    import auth.access_token as at
    from strands.tools.mcp import MCPClient

    # 2페이지짜리 응답: page1은 nextCursor를 실어 보내고, page2가 마지막이다.
    pages = {
        None: PaginatedList(["a", "b"], token="cursor-2"),
        "cursor-2": PaginatedList(["c"], token=None),
    }
    calls = []

    monkeypatch.setattr(MCPClient, "__init__", lambda self, factory: None)
    monkeypatch.setattr(MCPClient, "__enter__", lambda self: self)

    def fake_list_tools_sync(self, pagination_token=None):
        calls.append(pagination_token)
        return pages[pagination_token]

    monkeypatch.setattr(MCPClient, "list_tools_sync", fake_list_tools_sync)
    monkeypatch.setattr(at, "sigv4_httpx_client_factory", lambda ep: None)

    tools, _ = at.load_tools_from_mcp(f"{GW}/mcp")

    assert tools == ["a", "b", "c"]  # 두 페이지가 모두 합쳐진다
    assert calls == [None, "cursor-2"]  # 커서를 따라 두 번 호출한다
