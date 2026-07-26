"""app.py 오케스트레이션 흐름 테스트 (clarification 캐시/재개/만료).

SDK 를 실제로 호출하지 않도록 fake runner/session 과 monkeypatch 를 사용한다.
async 제너레이터는 asyncio.run 으로 드레인한다(pytest-asyncio 미의존).
"""

import asyncio

from orchestrator import app as app_module
from orchestrator.app import (
    RunnerSession,
    _clarification_expired_events,
    _drive,
    _resume_orchestration,
)
from orchestrator.ids import SequentialIdFactory
from orchestrator.request import ParsedRequest
from orchestrator.session_cache import SessionCache
from orchestrator.stream_translator import StreamTranslator


def _drain(agen):
    async def _run():
        out = []
        async for item in agen:
            out.append(item)
        return out

    return asyncio.run(_run())


class FakeRunner:
    """지정한 strands 이벤트 시퀀스를 stream_async 로 방출하는 가짜 runner."""

    def __init__(self, events):
        self._events = events

    async def stream_async(self, task):
        self.task = task
        for e in self._events:
            yield e


class FakeClient:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


def _req(session_id="s1", clarification_response=None):
    return ParsedRequest(
        question="질문",
        thread_id=session_id,
        run_id="r1",
        session_id=session_id,
        actor_id="anonymous",
        clarification_response=clarification_response,
    )


def _clarification_event():
    return {
        "type": "multiagent_node_interrupt",
        "node_id": "intent",
        "interrupts": [
            {"id": "i-1", "name": "clarification", "reason": {"question": "기간은?", "fields": []}}
        ],
    }


def test_clarification_expired_events_shape():
    out = list(_clarification_expired_events(SequentialIdFactory("r1")))
    types = [e["type"] for e in out]
    assert types == ["TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END"]
    assert "다시" in out[1]["delta"]


def test_drive_keeps_session_on_interrupt(monkeypatch):
    cache = SessionCache()
    monkeypatch.setattr(app_module, "SESSION_CACHE", cache)
    client = FakeClient()
    session = RunnerSession(
        runner=FakeRunner([_clarification_event()]), clients=[client], session_manager=None
    )
    req = _req()
    translator = StreamTranslator(SequentialIdFactory("r1"))
    out = _drain(_drive(session, req, translator, task="질문"))

    # CUSTOM 방출 + 세션이 캐시에 살아있고 클라이언트 미종료
    assert any(e["type"] == "CUSTOM" for e in out)
    assert cache.get("s1") is session
    assert client.stopped is False


def test_drive_closes_session_on_normal_completion(monkeypatch):
    cache = SessionCache()
    monkeypatch.setattr(app_module, "SESSION_CACHE", cache)
    client = FakeClient()
    session = RunnerSession(
        runner=FakeRunner([{"data": "정상 응답"}]), clients=[client], session_manager=None
    )
    req = _req()
    translator = StreamTranslator(SequentialIdFactory("r1"))
    out = _drain(_drive(session, req, translator, task="질문"))

    assert any(e["type"] == "TEXT_MESSAGE_CONTENT" for e in out)
    assert "s1" not in cache
    assert client.stopped is True


def test_drive_evicts_and_closes_lru(monkeypatch):
    cache = SessionCache(max_size=1)
    monkeypatch.setattr(app_module, "SESSION_CACHE", cache)
    old_client = FakeClient()
    old_session = RunnerSession(runner=None, clients=[old_client], session_manager=None)
    cache.put("old", old_session)

    new_client = FakeClient()
    new_session = RunnerSession(
        runner=FakeRunner([_clarification_event()]), clients=[new_client], session_manager=None
    )
    translator = StreamTranslator(SequentialIdFactory("r1"))
    _drain(_drive(new_session, _req("new"), translator, task="q"))

    # 상한 초과로 old 축출 + 자원 정리, new 는 유지
    assert "old" not in cache
    assert old_client.stopped is True
    assert cache.get("new") is new_session


def test_resume_cache_miss_yields_expired(monkeypatch):
    cache = SessionCache()
    monkeypatch.setattr(app_module, "SESSION_CACHE", cache)
    req = _req(clarification_response={"interruptId": "i-1", "values": {"period": "이번달"}})
    out = _drain(_resume_orchestration(req, app_module.SETTINGS))

    types = [e["type"] for e in out]
    # CLARIFICATION_EXPIRED: 안내 TEXT_MESSAGE + RUN_FINISHED (RUN_ERROR 아님)
    assert "TEXT_MESSAGE_CONTENT" in types
    assert types[-1] == "RUN_FINISHED"
    assert not any(e["type"] == "RUN_ERROR" for e in out)


def test_resume_cache_hit_drives_with_resume_task(monkeypatch):
    cache = SessionCache()
    monkeypatch.setattr(app_module, "SESSION_CACHE", cache)
    runner = FakeRunner([{"data": "재개 후 결과"}])
    client = FakeClient()
    session = RunnerSession(runner=runner, clients=[client], session_manager=None)
    cache.put("s1", session)

    req = _req(clarification_response={"interruptId": "i-1", "values": {"period": "이번달"}})
    out = _drain(_resume_orchestration(req, app_module.SETTINGS))

    # resume task 가 interruptResponse 형태로 runner 에 전달됨
    assert runner.task == [
        {"interruptResponse": {"interruptId": "i-1", "response": {"period": "이번달"}}}
    ]
    assert any(e["type"] == "TEXT_MESSAGE_CONTENT" for e in out)
    assert out[-1]["type"] == "RUN_FINISHED"
    # 정상 완료 → 세션 정리
    assert "s1" not in cache
    assert client.stopped is True
