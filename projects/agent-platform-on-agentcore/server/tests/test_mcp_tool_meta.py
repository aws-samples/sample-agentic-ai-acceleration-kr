"""툴 목록이 _meta를 통과시키는지.

스텁이 실제 응답보다 유능하면 테스트가 고무도장이 된다. 그래서 여기 스텁은 Strands
MCPAgentTool의 실제 형태를 그대로 흉내 낸다: tool_spec은 _meta를 버리고,
mcp_tool에만 원본이 남아 있다.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp_core.utils import MCPUtils  # noqa: E402
from models.mcp import MCPToolInfo  # noqa: E402


class StubMcpTool:
    """mcp.types.Tool 흉내. meta 속성의 alias가 _meta다."""

    def __init__(self, name, meta=None):
        self.name = name
        self.description = "설명"
        self.inputSchema = {"type": "object"}
        self.meta = meta


class StubAgentTool:
    """Strands MCPAgentTool 흉내.

    tool_spec이 _meta를 버리는 것이 실제 동작이다 — 이 스텁을 '고쳐서' 통과시키면
    안 된다.
    """

    def __init__(self, mcp_tool):
        self.mcp_tool = mcp_tool
        self.tool_name = mcp_tool.name

    @property
    def tool_spec(self):
        return {
            "name": self.mcp_tool.name,
            "description": self.mcp_tool.description,
            "inputSchema": {"json": self.mcp_tool.inputSchema},
        }


def test_extract_tool_info_returns_meta():
    meta = {"ui": {"resourceUri": "ui://weather/dashboard"}}
    tool = StubAgentTool(StubMcpTool("get_weather", meta=meta))

    name, description, input_schema, extracted = MCPUtils.extract_tool_info(tool)

    assert name == "get_weather"
    assert description == "설명"
    assert input_schema == {"type": "object"}
    assert extracted == meta


def test_extract_tool_info_meta_is_none_when_absent():
    tool = StubAgentTool(StubMcpTool("plain_tool"))
    _, _, _, extracted = MCPUtils.extract_tool_info(tool)
    assert extracted is None


def test_tool_info_model_carries_meta():
    info = MCPToolInfo(
        name="get_weather",
        description="설명",
        input_schema={"type": "object"},
        meta={"ui": {"resourceUri": "ui://weather/dashboard"}},
    )
    assert info.meta["ui"]["resourceUri"] == "ui://weather/dashboard"


def test_tool_info_meta_defaults_to_none():
    # _meta가 없는 기존 툴이 그대로 만들어져야 한다.
    info = MCPToolInfo(name="plain", description=None, input_schema=None)
    assert info.meta is None


def test_extract_tool_info_survives_a_raising_tool():
    """신뢰할 수 없는 서버의 잘못된 툴 하나가 목록 전체를 죽이면 안 된다.

    extract_tool_info의 넓은 except는 이걸 위해 있다. meta가 try 안에서
    초기화되면 그 except를 통과한 뒤 UnboundLocalError가 난다.
    """
    class Raising:
        @property
        def tool_name(self):
            raise RuntimeError("boom")

    name, description, input_schema, meta = MCPUtils.extract_tool_info(Raising())
    assert (name, description, input_schema, meta) == (None, None, None, None)
