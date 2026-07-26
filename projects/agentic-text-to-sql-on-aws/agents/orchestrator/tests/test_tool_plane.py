"""도구 평면(tool plane) 추상화 테스트 — direct ↔ gateway 전환.

AWS/SDK 를 실제로 호출하지 않도록 fake 클라이언트·토큰 fetch·시계를 주입한다.
- 모드별 require_mcp_arns 검증
- gateway 도구 suffix 분류(프리픽스 포함)
- Cognito 토큰 캐시(만료 전 재사용 / 만료 후 갱신)
- ToolClients direct/gateway 구성(clients 리스트, sql/semantic 분류)
"""

import pytest

from orchestrator.config import Settings
from orchestrator.mcp_client import (
    CognitoTokenCache,
    ToolClients,
    tool_display_name,
)


class FakeTool:
    """tool_name 속성만 가진 가짜 MCP 도구."""

    def __init__(self, name):
        self.tool_name = name


class FakeSpecTool:
    """tool_name 없이 tool_spec dict 만 노출하는 가짜 도구."""

    def __init__(self, name):
        self.tool_spec = {"name": name}


class FakeClient:
    def __init__(self, tools):
        self._tools = tools
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self, *args):
        self.stopped = True

    def list_tools_sync(self):
        return list(self._tools)


# ---------------------------------------------------------------------------
# config: 모드별 require 검증
# ---------------------------------------------------------------------------


def test_default_tool_plane_mode_is_direct():
    s = Settings.from_env({})
    assert s.tool_plane_mode == "direct"
    assert s.is_gateway_mode() is False


def test_gateway_mode_parsed_case_insensitive():
    s = Settings.from_env({"TOOL_PLANE_MODE": "GATEWAY"})
    assert s.tool_plane_mode == "gateway"
    assert s.is_gateway_mode() is True


def test_require_direct_mode_needs_arns():
    s = Settings.from_env({"SQL_MCP_ARN": "arn:sql"})
    with pytest.raises(ValueError, match="SEMANTIC_MCP_ARN"):
        s.require_mcp_arns()


def test_require_direct_mode_ignores_gateway_env():
    # direct 모드는 gateway env 가 없어도 ARN 만 있으면 통과.
    s = Settings.from_env({"SQL_MCP_ARN": "arn:sql", "SEMANTIC_MCP_ARN": "arn:sem"})
    s.require_mcp_arns()


def test_require_gateway_mode_needs_gateway_and_cognito():
    s = Settings.from_env({"TOOL_PLANE_MODE": "gateway", "GATEWAY_URL": "https://gw/mcp"})
    with pytest.raises(ValueError) as exc:
        s.require_mcp_arns()
    msg = str(exc.value)
    assert "COGNITO_CLIENT_ID" in msg
    assert "COGNITO_USER" in msg
    assert "COGNITO_PASSWORD_SECRET_ARN" in msg
    assert "COGNITO_USER_POOL_ID" in msg


def test_require_gateway_mode_needs_gateway_url():
    s = Settings.from_env(
        {
            "TOOL_PLANE_MODE": "gateway",
            "COGNITO_CLIENT_ID": "c",
            "COGNITO_USER": "u",
            "COGNITO_PASSWORD_SECRET_ARN": "arn:secret",
            "COGNITO_USER_POOL_ID": "pool",
        }
    )
    with pytest.raises(ValueError, match="GATEWAY_URL"):
        s.require_mcp_arns()


def test_require_gateway_mode_ok_when_all_present():
    s = _gateway_settings()
    s.require_mcp_arns()  # should not raise


def test_gateway_mode_does_not_require_arns():
    # gateway 모드에서는 ARN 이 비어 있어도 통과해야 한다.
    s = _gateway_settings()
    assert s.sql_mcp_arn == ""
    s.require_mcp_arns()


# ---------------------------------------------------------------------------
# suffix 분류 (Gateway target 프리픽스 포함)
# ---------------------------------------------------------------------------


def test_tool_display_name_from_tool_name():
    assert tool_display_name(FakeTool("run_sql")) == "run_sql"


def test_tool_display_name_from_spec_fallback():
    assert tool_display_name(FakeSpecTool("search_schema")) == "search_schema"


def test_tool_display_name_missing_returns_empty():
    assert tool_display_name(object()) == ""


def test_gateway_tools_classified_by_suffix_with_prefix():
    tools = [
        FakeTool("SqlTarget___run_sql"),
        FakeTool("SemanticTarget___search_schema"),
        FakeTool("request_clarification"),
    ]
    client = FakeClient(tools)
    tc = ToolClients(sql_client=client, semantic_client=client, gateway=True)
    sql = [tool_display_name(t) for t in tc.sql_tools()]
    sem = [tool_display_name(t) for t in tc.semantic_tools()]
    assert sql == ["SqlTarget___run_sql"]
    assert sem == ["SemanticTarget___search_schema"]


def test_gateway_tools_classified_without_prefix():
    tools = [FakeTool("run_sql"), FakeTool("search_schema")]
    client = FakeClient(tools)
    tc = ToolClients(sql_client=client, semantic_client=client, gateway=True)
    assert [tool_display_name(t) for t in tc.sql_tools()] == ["run_sql"]
    assert [tool_display_name(t) for t in tc.semantic_tools()] == ["search_schema"]


def test_direct_tools_return_full_client_lists():
    sql_client = FakeClient([FakeTool("run_sql"), FakeTool("extra")])
    sem_client = FakeClient([FakeTool("search_schema")])
    tc = ToolClients(sql_client=sql_client, semantic_client=sem_client, gateway=False)
    # direct 모드는 분류 없이 각 클라이언트 전체를 반환.
    assert len(tc.sql_tools()) == 2
    assert len(tc.semantic_tools()) == 1


# ---------------------------------------------------------------------------
# ToolClients 구성: clients 리스트 / start / close
# ---------------------------------------------------------------------------


def test_gateway_clients_is_single_instance():
    client = FakeClient([])
    tc = ToolClients(sql_client=client, semantic_client=client, gateway=True)
    assert tc.clients == [client]
    assert tc.gateway is True


def test_direct_clients_is_two_instances():
    sql_client = FakeClient([])
    sem_client = FakeClient([])
    tc = ToolClients(sql_client=sql_client, semantic_client=sem_client, gateway=False)
    assert tc.clients == [sql_client, sem_client]
    assert tc.gateway is False


def test_start_and_close_all_clients():
    sql_client = FakeClient([])
    sem_client = FakeClient([])
    tc = ToolClients(sql_client=sql_client, semantic_client=sem_client, gateway=False)
    tc.start()
    assert sql_client.started and sem_client.started
    tc.close()
    assert sql_client.stopped and sem_client.stopped


def test_create_tool_clients_direct(monkeypatch):
    import orchestrator.mcp_client as mc

    made = []

    def fake_create(arn, region):
        c = FakeClient([])
        made.append((arn, region))
        return c

    monkeypatch.setattr(mc, "create_mcp_client", fake_create)
    s = Settings.from_env({"SQL_MCP_ARN": "arn:sql", "SEMANTIC_MCP_ARN": "arn:sem"})
    tc = mc.create_tool_clients(s)
    assert tc.gateway is False
    assert len(tc.clients) == 2
    assert made == [("arn:sql", "us-west-2"), ("arn:sem", "us-west-2")]


def test_create_tool_clients_gateway(monkeypatch):
    import orchestrator.mcp_client as mc

    captured = {}

    def fake_token(settings, cache=None):
        captured["token_called"] = True
        return "tok-123"

    def fake_gateway_client(url, token):
        captured["url"] = url
        captured["token"] = token
        return FakeClient([])

    monkeypatch.setattr(mc, "fetch_cognito_token", fake_token)
    monkeypatch.setattr(mc, "create_gateway_mcp_client", fake_gateway_client)
    s = _gateway_settings()
    tc = mc.create_tool_clients(s)
    assert tc.gateway is True
    assert len(tc.clients) == 1  # sql/semantic 동일 인스턴스
    assert captured["url"] == "https://gw.example/mcp"
    assert captured["token"] == "tok-123"


# ---------------------------------------------------------------------------
# Cognito 토큰 캐시 (시계 주입)
# ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_token_cache_reuses_before_expiry():
    clock = FakeClock(1000.0)
    cache = CognitoTokenCache(clock=clock)
    calls = []

    def fetch():
        calls.append(1)
        return ("tok-A", 3600)

    assert cache.get_or_fetch(fetch, skew=300) == "tok-A"
    # 만료 5분 전 이내 시각으로 이동 — 재사용해야 함.
    clock.t = 1000.0 + 3600 - 301
    assert cache.get_or_fetch(fetch, skew=300) == "tok-A"
    assert len(calls) == 1


def test_token_cache_refreshes_after_skew_window():
    clock = FakeClock(1000.0)
    cache = CognitoTokenCache(clock=clock)
    tokens = iter(["tok-A", "tok-B"])

    def fetch():
        return (next(tokens), 3600)

    assert cache.get_or_fetch(fetch, skew=300) == "tok-A"
    # 만료 5분 전 경계 진입 — 갱신해야 함.
    clock.t = 1000.0 + 3600 - 299
    assert cache.get_or_fetch(fetch, skew=300) == "tok-B"


def test_token_cache_refreshes_after_expiry():
    clock = FakeClock(1000.0)
    cache = CognitoTokenCache(clock=clock)
    tokens = iter(["tok-A", "tok-B"])

    def fetch():
        return (next(tokens), 3600)

    assert cache.get_or_fetch(fetch, skew=300) == "tok-A"
    clock.t = 1000.0 + 4000  # 완전 만료 후
    assert cache.get_or_fetch(fetch, skew=300) == "tok-B"


def test_fetch_cognito_token_uses_cache_and_reads_secret(monkeypatch):
    """fetch_cognito_token 이 시크릿을 읽고 initiate_auth 로 토큰을 얻어 캐시하는지."""
    import orchestrator.mcp_client as mc

    class FakeSecrets:
        def get_secret_value(self, SecretId):
            assert SecretId == "arn:secret"
            return {"SecretString": "pw"}

    class FakeIdp:
        def __init__(self):
            self.calls = 0

        def initiate_auth(self, ClientId, AuthFlow, AuthParameters):
            self.calls += 1
            assert AuthFlow == "USER_PASSWORD_AUTH"
            assert AuthParameters == {"USERNAME": "u", "PASSWORD": "pw"}
            return {"AuthenticationResult": {"AccessToken": "tok-X", "ExpiresIn": 3600}}

    idp = FakeIdp()

    class FakeBoto:
        def client(self, name, region_name=None):
            return FakeSecrets() if name == "secretsmanager" else idp

    import sys

    monkeypatch.setitem(sys.modules, "boto3", FakeBoto())
    clock = FakeClock(500.0)
    cache = mc.CognitoTokenCache(clock=clock)
    s = _gateway_settings()

    assert mc.fetch_cognito_token(s, cache=cache) == "tok-X"
    # 만료 전 재호출 → initiate_auth 재호출 없음(캐시 재사용).
    assert mc.fetch_cognito_token(s, cache=cache) == "tok-X"
    assert idp.calls == 1


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _gateway_settings():
    return Settings.from_env(
        {
            "TOOL_PLANE_MODE": "gateway",
            "GATEWAY_URL": "https://gw.example/mcp",
            "COGNITO_CLIENT_ID": "c",
            "COGNITO_USER": "u",
            "COGNITO_PASSWORD_SECRET_ARN": "arn:secret",
            "COGNITO_USER_POOL_ID": "pool",
        }
    )
