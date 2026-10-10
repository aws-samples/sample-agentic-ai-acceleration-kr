"""대화 중간 도구 추가와 thinking 표시(updates)를 Bedrock 으로 넘긴다 (2026-10-09).

계정 기능 플래그가 켜진 Claude Code 2.1.29x 는 beta 를 더 보낸다. 그중 둘이 게이트웨이에서
대화마다 400 을 한 번 냈다(Claude Code 가 그 기능을 빼고 다시 보내 장애는 아님).

- ``thinking-display-updates-2026-08-18``: ``thinking.display: "updates"`` 를 연다. beta 를
  버리면 ``thinking.adaptive.display: Input should be 'summarized', 'omitted'`` 400
  (실제 배포에서 관찰).
- ``inline-tools-2026-09-15``: system 메시지의 ``tool_addition`` / ``tool_removal`` 블록을
  연다. beta 를 버리면 ``Input tag 'tool_addition' ... does not match`` 400.

``inline-tools`` 는 설정만으로는 부족하다. Claude Code 는 advisor 도구를 ``tools`` 에 넣고
system 메시지에서 ``tool_addition`` 으로 가리킨다. advisor 를 ``tools`` 에서만 지우면 beta 를
넘겨도 ``tool_addition/tool_removal references unknown tool 'advisor'`` 400 이므로, 가리키는
블록은 tool_filter 가 함께 지운다(tests/unit/test_tool_filter.py).

계약:
- 두 beta 는 기본 설정으로 넘어간다(짝 필드 없음). 그 beta 를 보내지 않는 클라이언트의
  본문은 이전과 같다;
- 본문을 만든 뒤 Bedrock 으로 가는 요청에 advisor 를 가리키는 것이 남지 않는다;
- 웹 검색 루프는 system 메시지의 ``tool_addition`` 을 건드리지 않는다.

English: forward mid-conversation tool changes and thinking "updates" display to Bedrock
(2026-10-09). Claude Code 2.1.29x with account feature flags sends more betas; two of them
cost one 400 per conversation behind the gateway (Claude Code retried without the feature).
``inline-tools`` also needs the tool filter: Claude Code lists the advisor tool in ``tools`` and
points at it with a ``tool_addition`` block in a system message, so removing the advisor from
``tools`` alone still 400s ("references unknown tool 'advisor'"); tool_filter removes the blocks
that point at it (tests/unit/test_tool_filter.py). Contract: both betas forward by default (no
paired field) and clients that do not send them get the old body; the body sent to Bedrock has
nothing that points at the advisor; the web-search loop leaves the system message's
``tool_addition`` alone.
"""

from __future__ import annotations

import copy
import json
import time

import pytest

from app.services import upstream_compat
from app.services.upstream_compat import (
    apply_forwarded_betas,
    client_betas,
    forward_beta_map,
)

DTU = "dangerous-tool-use-2026-09-03"
PTC = "per-turn-control-2026-07-01"
IT = "inline-tools-2026-09-15"
TD = "thinking-display-updates-2026-08-18"

#: 2.1.295 가 계정 플래그가 켜진 환경에서 보낸 13개(2026-10-09 캡처) 뒤에, 2026-10-08 배포
#: 게이트웨이 로그(beta_dropped)에 나온 나머지 이름을 붙였다(이 넷의 순서는 가정).
#: English: the 13 betas 2.1.295 sent with account flags on (captured), followed by the other
#: names seen in a 2026-10-08 deployment gateway log (order of those four assumed).
FLAGGED_HEADER = (
    "claude-code-20250219,interleaved-thinking-2025-05-14,"
    "thinking-token-count-2026-05-13,context-management-2025-06-27,"
    "prompt-caching-scope-2026-01-05,mid-conversation-system-2026-04-07,"
    "per-turn-control-2026-07-01,mid-conversation-tool-changes-2026-07-01,"
    "inline-tools-2026-09-15,advisor-tool-2026-03-01,effort-2025-11-24,"
    "dangerous-tool-use-2026-09-03,afk-mode-2026-01-31,"
    "redact-thinking-2026-02-12,structured-outputs-2025-12-15,"
    "thinking-display-updates-2026-08-18,fallback-credit-2026-06-01"
)
#: 빈 설정(CLAUDE_CONFIG_DIR)의 2.1.295 — 2.1.289 와 같은 11개.
#: English: 2.1.295 with an empty config dir — the same 11 as 2.1.289.
CLEAN_HEADER = (
    "claude-code-20250219,interleaved-thinking-2025-05-14,"
    "thinking-token-count-2026-05-13,context-management-2025-06-27,"
    "prompt-caching-scope-2026-01-05,mid-conversation-system-2026-04-07,"
    "per-turn-control-2026-07-01,mid-conversation-tool-changes-2026-07-01,"
    "effort-2025-11-24,dangerous-tool-use-2026-09-03,afk-mode-2026-01-31"
)

ADVISOR = {"type": "advisor_20260301", "name": "advisor", "model": "claude-fable-5-1"}
READ = {"name": "Read", "description": "read a file", "input_schema": {"type": "object"}}
CC = {"type": "ephemeral"}
SAFEGUARDS = [{"type": "dangerous_tool_use",
               "classifier_context": {"v": 1, "permission_mode": "auto"}}]


def _add(name: str, *, cache: bool = False, kind: str = "tool_addition") -> dict:
    blk = {"type": kind, "tool": {"type": "tool_reference", "name": name}}
    if cache:
        blk["cache_control"] = dict(CC)
    return blk


def _text(t: str = "reminder", *, cache: bool = False) -> dict:
    blk = {"type": "text", "text": t}
    if cache:
        blk["cache_control"] = dict(CC)
    return blk


def _fmap():
    from app.config import Settings

    return forward_beta_map(Settings().bedrock_forward_betas)


@pytest.fixture(autouse=True)
def _fresh_log_once(monkeypatch):
    monkeypatch.setattr(upstream_compat, "_logged_dropped_betas", set())


# ── 설정 / settings ──────────────────────────────────────────────────────────
def test_default_forwards_inline_tools_and_thinking_display_updates_without_fields():
    fmap = _fmap()
    assert fmap[IT] is None and fmap[TD] is None


def test_flagged_client_gets_four_betas_in_header_order():
    out = {"anthropic_version": "bedrock-2023-05-31"}
    kept = apply_forwarded_betas(out, {"safeguards": SAFEGUARDS},
                                 client_betas([FLAGGED_HEADER]), _fmap())
    assert kept == [PTC, IT, DTU, TD]
    assert out["anthropic_beta"] == [PTC, IT, DTU, TD]


def test_clean_client_still_gets_the_same_two_betas():
    out = {"anthropic_version": "bedrock-2023-05-31"}
    kept = apply_forwarded_betas(out, {"safeguards": SAFEGUARDS},
                                 client_betas([CLEAN_HEADER]), _fmap())
    assert kept == [PTC, DTU]


# ── 라우트 / route: /v1/messages → Bedrock body ──────────────────────────────
async def test_route_sends_no_advisor_reference_and_forwards_inline_tools(monkeypatch):
    from tests.regression.test_high_bedrock_beta_forwarding import _sent_to_bedrock

    messages = [{"role": "user", "content": "hi"},
                {"role": "system", "output_config": {"effort": "medium"},
                 "content": [_text(), _add("advisor", cache=True)]}]
    sent = await _sent_to_bedrock(
        monkeypatch, header=FLAGGED_HEADER,
        extra={"tools": [READ, ADVISOR], "messages": messages,
               "safeguards": SAFEGUARDS})
    assert "advisor" not in json.dumps(sent)
    assert sent["anthropic_beta"] == [PTC, IT, DTU, TD]
    assert sent["messages"][1]["content"] == [_text(cache=True)]
    assert sent["safeguards"] == SAFEGUARDS


# ── 웹 검색 루프 / web-search loop ───────────────────────────────────────────
async def test_web_search_loop_keeps_the_system_tool_addition():
    from app.services import web_search_loop as wsl
    from tests.regression.test_high_websearch_safeguard_results import (
        BASH,
        CLIENT,
        SEARCH,
        _turn,
    )
    from tests.unit.test_web_search_loop import FakeMcp, FakeRequest, _aiter

    turns = iter([_turn(tool=SEARCH), _turn(text="done", tool=CLIENT)])
    sent: list[dict] = []

    async def invoke_stream(turn_body):
        sent.append(copy.deepcopy(turn_body))
        return 200, _aiter(next(turns)), {}, None

    async def on_usage(_u):
        return None

    base = {"messages": [{"role": "user", "content": "hi"},
                         {"role": "system", "content": [_text(), _add("Bash")]}],
            "tools": [BASH]}
    async for _ in wsl._anthropic_stream(
            invoke_stream=invoke_stream, base_body=base, mcp_client=FakeMcp(),
            request=FakeRequest(), on_usage=on_usage, max_iterations=5,
            deadline=time.monotonic() + 60, default_max_results=10):
        pass
    assert len(sent) == 2
    for turn_body in sent:
        system = turn_body["messages"][1]
        assert system["role"] == "system"
        assert [b["type"] for b in system["content"]] == ["text", "tool_addition"]
        assert system["content"][1]["tool"] == {"type": "tool_reference", "name": "Bash"}
