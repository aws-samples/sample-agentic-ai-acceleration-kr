"""앱 전용 툴이 에이전트 목록에서 빠지는지.

규격 MUST: visibility에 "model"이 없으면 호스트는 그 툴을 에이전트의 툴 목록에
넣어서는 안 된다. 넣으면 UI 안에서만 눌러야 하는 동작을 모델이 임의로 호출한다.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.mcp_manager import filter_model_visible  # noqa: E402


class StubMcpTool:
    def __init__(self, name, meta=None):
        self.name = name
        self.meta = meta


class StubAgentTool:
    def __init__(self, name, meta=None):
        self.mcp_tool = StubMcpTool(name, meta)
        self.tool_name = name


def test_app_only_tool_is_removed():
    tools = [
        StubAgentTool("visible"),
        StubAgentTool("app_only", meta={"ui": {"visibility": ["app"]}}),
    ]
    assert [t.tool_name for t in filter_model_visible(tools)] == ["visible"]


def test_tool_without_meta_is_kept():
    # 기본값이 ["model", "app"]이므로 기존 툴은 전부 남아야 한다.
    tools = [StubAgentTool("legacy_a"), StubAgentTool("legacy_b")]
    assert len(filter_model_visible(tools)) == 2


def test_explicit_both_is_kept():
    tools = [StubAgentTool("both", meta={"ui": {"visibility": ["model", "app"]}})]
    assert len(filter_model_visible(tools)) == 1


def test_malformed_visibility_keeps_tool():
    # 잘못된 형태 때문에 툴이 조용히 사라지면 안 된다.
    tools = [StubAgentTool("odd", meta={"ui": {"visibility": "app"}})]
    assert len(filter_model_visible(tools)) == 1


def test_empty_list_is_safe():
    assert filter_model_visible([]) == []


def test_a_raising_tool_is_kept_and_does_not_break_the_list():
    """들여다볼 수 없는 툴 하나가 목록 전체를 죽이면 안 된다.

    lazily-hydrated 프록시나 검증 프로퍼티를 가진 객체가 접근 시 예외를 던질 수 있다.
    그때 필터가 터지면 에이전트는 나머지 정상 툴까지 전부 잃는다.
    """
    class Raising:
        tool_name = "raising"

        @property
        def mcp_tool(self):
            raise RuntimeError("boom")

    good = StubAgentTool("good")
    result = filter_model_visible([Raising(), good])

    assert len(result) == 2
    assert good in result
