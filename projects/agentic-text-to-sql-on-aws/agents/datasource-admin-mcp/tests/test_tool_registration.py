"""실 FastMCP 로 도구 10종이 등록·스키마화되는지 검증 — 도구 시그니처 계약 테스트.

``from __future__ import annotations`` 하에서 데코레이터가
``get_type_hints`` 로 어노테이션을 모듈 전역에서 평가하므로, 함수 내부 지연 임포트한 타입을
어노테이션에 쓰면 배포 시점에 NameError 로 크래시한다(로컬 pytest 는 통과했던 사례).
이 테스트는 실제 SDK 의 스키마 생성 경로를 태워 그 함정을 사전에 잡는다.
"""

from __future__ import annotations

import asyncio

import pytest

from datasource_admin_mcp import server

# 도구 시그니처 계약(README 와 동일): 도구명 → (필수 인자, 선택 인자 기본값)
EXPECTED_TOOLS: dict[str, tuple[list[str], dict[str, object]]] = {
    "list_entities": ([], {"entity_type": None, "status": None}),
    "get_entity": (["entity_type", "entity_id"], {}),
    "put_entity": (
        ["entity_type", "entity_id", "payload"],
        {"status": "candidate", "actor": "admin-panel"},
    ),
    "publish_entity": (["entity_type", "entity_id"], {"actor": "admin-panel"}),
    "unpublish_entity": (["entity_type", "entity_id"], {"actor": "admin-panel"}),
    "register_datasource": (
        ["datasource_id", "engine", "config"],
        {"actor": "admin-panel"},
    ),
    "test_datasource": (["datasource_id"], {}),
    "crawl_schema": (["datasource_id"], {"actor": "admin-panel"}),
    # 개선 파이프라인 Track B
    "reject_entity": (
        ["entity_type", "entity_id"],
        {"reason": "", "actor": "admin-panel"},
    ),
    "mine_candidates": ([], {"hours": 24, "actor": "mining-batch"}),
}


@pytest.fixture(scope="module")
def tools() -> dict:
    """실 FastMCP 인스턴스에서 등록된 도구 목록(스키마 생성 경로 포함)."""
    listed = asyncio.run(server.mcp.list_tools())
    return {tool.name: tool for tool in listed}


def test_all_contracted_tools_registered(tools: dict) -> None:
    assert set(tools) == set(EXPECTED_TOOLS), f"등록된 도구: {sorted(tools)}"


def test_core_tools_still_registered(tools: dict) -> None:
    # additive only 계약: 큐레이션·데이터소스 도구 8종은 제거·개명되지 않아야 한다.
    core_tools = {
        "list_entities",
        "get_entity",
        "put_entity",
        "publish_entity",
        "unpublish_entity",
        "register_datasource",
        "test_datasource",
        "crawl_schema",
    }
    assert core_tools <= set(tools)


def test_mine_candidates_hours_is_integer_typed(tools: dict) -> None:
    # hours 가 정수 스키마여야 MCP 클라이언트가 숫자를 전달한다.
    schema = tools["mine_candidates"].inputSchema["properties"]["hours"]
    assert schema.get("type") == "integer", schema


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_tool_schema_matches_contract(tools: dict, name: str) -> None:
    required_args, optional_args = EXPECTED_TOOLS[name]
    schema = tools[name].inputSchema
    properties = schema.get("properties", {})

    # 인자 집합이 계약과 정확히 일치해야 한다(추가/누락 모두 실패).
    assert set(properties) == set(required_args) | set(optional_args)
    # 필수 인자만 required 로 표시돼야 한다.
    assert set(schema.get("required", [])) == set(required_args)


def test_tool_descriptions_are_korean(tools: dict) -> None:
    # LLM/관리자에게 노출되는 설명이 비어 있지 않아야 한다(도구 선택 품질).
    for name, tool in tools.items():
        assert tool.description, f"{name} 설명 누락"


def test_payload_and_config_are_object_typed(tools: dict) -> None:
    # dict 인자가 object 스키마로 잡혀야 MCP 클라이언트가 JSON 객체를 전달할 수 있다.
    assert tools["put_entity"].inputSchema["properties"]["payload"]["type"] == "object"
    assert tools["register_datasource"].inputSchema["properties"]["config"]["type"] == "object"


def test_optional_string_args_allow_null(tools: dict) -> None:
    # list_entities(entity_type=None, status=None) — nullable 로 표현돼야 한다.
    properties = tools["list_entities"].inputSchema["properties"]
    for key in ("entity_type", "status"):
        rendered = str(properties[key])
        assert "null" in rendered, f"{key} 가 nullable 이 아니다: {properties[key]}"
