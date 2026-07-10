# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""invoke() heartbeat 스트리밍 머지 단위 테스트.

핵심 계약(§49 레버 C):
  1. sub-agent blocking(침묵) 구간에 type:heartbeat 프레임이 흐른다(공백 제거).
  2. 첫 text 델타 이후엔 heartbeat 가 멈춘다(중복/순서 보호).
  3. 기존 이벤트(thinking/tool_call/tool_result/reasoning/text/done) 순서·계약 보존.
  4. pump 예외가 consumer 로 전파된다(삼키지 않음).

fake orchestrator.stream_async 로 agent 레벨만 검증(LLM 호출 0). heartbeat
간격은 monkeypatch 로 짧게(0.02s) 줄여 침묵을 빠르게 재현.
"""

from __future__ import annotations

import asyncio

import pytest

from agent import main as m


async def _drain(payload: dict) -> list[dict]:
    return [ev async for ev in m.invoke(payload)]


@pytest.mark.asyncio
async def test_heartbeat_fills_silence(monkeypatch):
    """sub-agent blocking 구간(침묵)에 heartbeat 프레임이 발행된다."""
    monkeypatch.setattr(m, "_HEARTBEAT_FIRST", 0.02)
    monkeypatch.setattr(m, "_HEARTBEAT_INTERVAL", 0.02)

    async def fake_stream(content):
        # orchestrator 가 sql specialist 를 부른다고 알림(current_tool_use).
        yield {"current_tool_use": {"toolUseId": "t1", "name": "ask_sql_specialist", "input": {}}}
        # 침묵: sub-agent 가 blocking 으로 도는 구간(0.1s) — heartbeat 가 메워야 함.
        await asyncio.sleep(0.1)
        # 그제서야 답변 텍스트 시작.
        yield {"data": "비용은 "}
        yield {"data": "$84.4"}

    class _FakeOrch:
        stream_async = staticmethod(fake_stream)

    monkeypatch.setattr(m, "orchestrator", _FakeOrch)

    events = await _drain({"content": "6월 비용", "session_id": "s1"})
    types = [e["type"] for e in events]

    # 1) 침묵 구간에 heartbeat 가 ≥1개 흘렀다.
    hbs = [e for e in events if e["type"] == "heartbeat"]
    assert len(hbs) >= 1, f"heartbeat 없음: {types}"
    # 2) heartbeat 는 in-flight tool(ask_sql_specialist)로 라벨링됐다.
    assert any(e["phase"] == "sql" for e in hbs), hbs
    assert all("elapsed_ms" in e for e in hbs)
    # 3) 기존 계약 보존: thinking 먼저, tool_call, text, done.
    assert types[0] == "thinking"
    assert "tool_call" in types
    assert "text" in types
    assert types[-1] == "done"


@pytest.mark.asyncio
async def test_heartbeat_stops_after_first_text(monkeypatch):
    """첫 text 델타 이후엔 heartbeat 가 멈춘다(중복/순서 보호)."""
    monkeypatch.setattr(m, "_HEARTBEAT_FIRST", 0.02)
    monkeypatch.setattr(m, "_HEARTBEAT_INTERVAL", 0.02)

    async def fake_stream(content):
        yield {"data": "답변 시작 "}  # 즉시 텍스트 — heartbeat 가 거의 안 떠야
        await asyncio.sleep(0.1)       # 텍스트 시작 후 침묵
        yield {"data": "계속"}

    class _FakeOrch:
        stream_async = staticmethod(fake_stream)

    monkeypatch.setattr(m, "orchestrator", _FakeOrch)

    events = await _drain({"content": "q", "session_id": "s2"})
    # 첫 text 이후 도착한 heartbeat 는 버려져야 한다 → text 뒤엔 heartbeat 없음.
    seen_text = False
    for e in events:
        if e["type"] == "text":
            seen_text = True
        if seen_text:
            assert e["type"] != "heartbeat", f"text 이후 heartbeat 누출: {events}"


@pytest.mark.asyncio
async def test_validator_skip_gate_warns(monkeypatch):
    """§58 결함⑨: SQL 은 돌았는데 validator 미호출이면 WARN 발행(게이트 가시화)."""
    monkeypatch.setattr(m, "_HEARTBEAT_FIRST", 0.02)
    monkeypatch.setattr(m, "_HEARTBEAT_INTERVAL", 0.02)

    async def fake_stream(content):
        # SQL specialist 가 결과를 stash 한 것처럼 — validator 는 부르지 않음.
        bucket = m._tool_results.get()
        if bucket is not None:
            bucket.append({"tool": "ask_sql_specialist", "result": {"sql": "SELECT 1", "rows": []}})
        yield {"data": "비용은 $84.4 입니다"}

    class _FakeOrch:
        stream_async = staticmethod(fake_stream)

    monkeypatch.setattr(m, "orchestrator", _FakeOrch)

    events = await _drain({"content": "6월 비용", "session_id": "s-skip"})
    validators = [e for e in events if e["type"] == "validator"]
    assert any(
        v.get("result", {}).get("verdict") == "WARN"
        and "검증" in v.get("result", {}).get("reason", "")
        for v in validators
    ), f"validator-skip WARN 없음: {validators}"


@pytest.mark.asyncio
async def test_validator_present_no_skip_warn(monkeypatch):
    """validator 가 호출됐으면 skip 게이트 WARN 안 뜬다."""
    monkeypatch.setattr(m, "_HEARTBEAT_FIRST", 0.02)
    monkeypatch.setattr(m, "_HEARTBEAT_INTERVAL", 0.02)

    async def fake_stream(content):
        bucket = m._tool_results.get()
        if bucket is not None:
            bucket.append({"tool": "ask_sql_specialist", "result": {"sql": "SELECT 1", "rows": []}})
            bucket.append({"tool": "ask_validator", "result": {"verdict": "PASS", "reason": "ok"}})
        yield {"data": "검증 완료된 답변"}

    class _FakeOrch:
        stream_async = staticmethod(fake_stream)

    monkeypatch.setattr(m, "orchestrator", _FakeOrch)

    events = await _drain({"content": "q", "session_id": "s-ok"})
    skip_warns = [
        e for e in events
        if e["type"] == "validator" and "검증을 거치지 않" in e.get("result", {}).get("reason", "")
    ]
    assert not skip_warns, f"validator 있는데 skip WARN 누출: {skip_warns}"


@pytest.mark.asyncio
async def test_pump_exception_propagates(monkeypatch):
    """stream_async 예외가 consumer 로 전파된다(조용히 삼키지 않음)."""
    monkeypatch.setattr(m, "_HEARTBEAT_FIRST", 0.02)
    monkeypatch.setattr(m, "_HEARTBEAT_INTERVAL", 0.02)

    async def fake_stream(content):
        yield {"current_tool_use": {"toolUseId": "t1", "name": "ask_sql_specialist", "input": {}}}
        raise RuntimeError("bedrock boom")

    class _FakeOrch:
        stream_async = staticmethod(fake_stream)

    monkeypatch.setattr(m, "orchestrator", _FakeOrch)

    with pytest.raises(RuntimeError, match="bedrock boom"):
        await _drain({"content": "q", "session_id": "s3"})
