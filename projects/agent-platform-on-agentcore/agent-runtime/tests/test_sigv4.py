from auth.sigv4 import sigv4_httpx_client_factory

GW = "https://abc123.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"


def _fake_creds(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAIOSFODNN7EXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


def test_non_gateway_url_returns_none(monkeypatch):
    _fake_creds(monkeypatch)
    assert sigv4_httpx_client_factory("http://localhost:8080/mcp") is None


def test_gateway_url_signs_request(monkeypatch):
    _fake_creds(monkeypatch)
    factory = sigv4_httpx_client_factory(GW)
    assert factory is not None
    client = factory()
    req = client.build_request("POST", GW, content=b'{"jsonrpc":"2.0"}')
    flow = client.auth.auth_flow(req)
    signed = next(flow)
    assert signed.headers["Authorization"].startswith("AWS4-HMAC-SHA256")
    assert "us-east-1" in signed.headers["Authorization"]
