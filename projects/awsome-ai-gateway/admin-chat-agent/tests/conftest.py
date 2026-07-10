# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""pytest fixtures — admin-chat-agent 테스트 공통.

`src/agent/main.py` 는 import 시점에 strands / bedrock_agentcore / boto3 를
끌어오고 sub-agent(LLM client)를 인스턴스화한다. 순수 함수(_reconcile_numbers,
_extract_chart_specs 등)만 테스트하려고 이 무거운 의존성을 깔 필요는 없다.

`agent_pure_fns` fixture 는 main.py 소스에서 순수 함수/상수 정의만 AST 로
추출해 격리된 네임스페이스에 exec 한다 — 실제 소스를 그대로 실행하므로
사본 drift 위험이 없다(복붙 테스트의 함정 회피).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_AGENT_MAIN = (
    Path(__file__).resolve().parent.parent / "src" / "agent" / "main.py"
)

# 무거운 import 없이 격리 실행 가능한 순수 정의들.
_PURE_NAMES = {
    "_TIME_UNIT",
    "_NUM_RE",
    "_collect_numbers",
    "_reconcile_numbers",
    "_parse_agent_json",
    "_valid_chart_spec",
    "_extract_chart_specs",
}


def _load_pure_namespace() -> dict:
    import json
    import re

    src = _AGENT_MAIN.read_text(encoding="utf-8")
    tree = ast.parse(src)
    chunks: list[str] = []
    for node in tree.body:
        name = None
        if isinstance(node, ast.FunctionDef):
            name = node.name
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    name = t.id
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name = node.target.id
        if name in _PURE_NAMES:
            seg = ast.get_source_segment(src, node)
            if seg:
                chunks.append(seg)
    ns: dict = {"re": re, "json": json}
    exec("\n\n".join(chunks), ns)  # noqa: S102 — 신뢰된 1st-party 소스
    return ns


@pytest.fixture(scope="session")
def agent_pure_fns() -> dict:
    """main.py 의 순수 함수/상수를 담은 네임스페이스 dict."""
    return _load_pure_namespace()
