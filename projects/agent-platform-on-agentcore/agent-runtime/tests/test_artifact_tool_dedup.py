"""게이트웨이가 노출하는 artifact 툴을 런타임 목록에서 걷어낸다.

artifact 툴은 이 런타임에서 로컬로 서빙되고(`agents/default.py`) `main.py` 가
**정확한 이름**으로 가로채 패널 이벤트를 만든다. 공유 게이트웨이도 harness 를 위해
같은 툴을 노출하므로, 런타임은 게이트웨이 접두사가 붙은 두 번째 사본
(`<target>___create_artifact`)을 함께 받는다. 그 사본은 접두사 때문에 `main.py`
가 못 알아채므로, 모델이 그쪽을 고르면 패널이 열리지 않는 죽은 툴이 된다. 로컬
사본만 남긴다.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.mcp_manager import drop_artifact_tools  # noqa: E402


class StubTool:
    def __init__(self, name):
        self.tool_name = name


def names(tools):
    return [t.tool_name for t in tools]


def test_gateway_prefixed_artifact_tools_are_dropped():
    tools = [
        StubTool("platform-tools___create_artifact"),
        StubTool("platform-tools___update_artifact"),
        StubTool("platform-tools___web_search"),
        StubTool("platform-tools___current_time"),
    ]
    assert names(drop_artifact_tools(tools)) == [
        "platform-tools___web_search",
        "platform-tools___current_time",
    ]


def test_bare_artifact_names_are_dropped_too():
    # 접두사 없이 오더라도(로컬 등록이 게이트웨이로 새어 들어온 경우) 걷어낸다.
    tools = [StubTool("create_artifact"), StubTool("update_artifact"), StubTool("calculate")]
    assert names(drop_artifact_tools(tools)) == ["calculate"]


def test_non_artifact_tools_are_kept():
    tools = [StubTool("web_search"), StubTool("fetch_url")]
    assert names(drop_artifact_tools(tools)) == ["web_search", "fetch_url"]


def test_empty_list_is_safe():
    assert drop_artifact_tools([]) == []


def test_an_unreadable_tool_is_kept():
    """이름을 못 읽는 툴 하나가 목록 전체를 잃게 만들면 안 된다."""
    class Raising:
        @property
        def tool_name(self):
            raise RuntimeError("boom")

    good = StubTool("web_search")
    result = drop_artifact_tools([Raising(), good])
    assert good in result and len(result) == 2
